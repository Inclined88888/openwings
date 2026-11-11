"""Unit tests for the Wings page renderer decisions (overlay/app/wings/render.py).
Run with ./tests/unit/run.sh (inside the built image, network off)."""
from __future__ import annotations

import asyncio
import importlib
import sys

import pytest

sys.path.insert(0, "/app")


@pytest.fixture
def render(monkeypatch):
    for name in ("WINGS_RENDERER", "WINGS_COMPOSE_PROFILES", "WINGS_BROWSER_ALLOWED_DOMAINS",
                 "WINGS_BROWSER_ALLOW_PRIVATE"):
        monkeypatch.delenv(name, raising=False)
    import wings.render as r
    return importlib.reload(r)


def test_enabled_follows_the_browser_profile(render, monkeypatch):
    assert not render.enabled()
    monkeypatch.setenv("WINGS_COMPOSE_PROFILES", "runner, browser")
    assert render.enabled()
    monkeypatch.setenv("WINGS_RENDERER", "0")
    assert not render.enabled()


def test_only_renders_when_a_browser_can_help(render):
    assert render.worth_rendering({"error": ""}, "")
    assert render.worth_rendering({"error": "Empty response body"}, "")
    assert render.worth_rendering({"error": "Page access challenge: article content was not retrieved."}, "")
    assert render.worth_rendering({"error": "HTTP 403: Forbidden"}, "")
    assert render.worth_rendering({"js_rendered": True}, "Loading...")
    # Real failures and pages that already have their text are left alone.
    assert not render.worth_rendering({"error": "HTTP 404: Not Found"}, "")
    assert not render.worth_rendering({"error": "NetworkError: name not resolved"}, "")
    assert not render.worth_rendering({"error": "TooLarge: 50 MB"}, "")
    assert not render.worth_rendering({"js_rendered": True}, "x" * 2000)
    assert not render.worth_rendering({"js_rendered": False}, "short page")


def test_refuses_bad_urls_before_touching_the_browser(render, monkeypatch):
    monkeypatch.setenv("WINGS_RENDERER", "1")

    async def no_browser():
        raise AssertionError("must not connect")

    monkeypatch.setattr(render, "_connect", no_browser)
    for url in ("file:///etc/passwd", "javascript:alert(1)", "http://user:pw@example.com/", "http://127.0.0.1:7000/",
                "http://169.254.169.254/latest/meta-data/"):
        result = asyncio.run(render.render(url))
        assert not result["success"] and result["error"].startswith("not opened"), (url, result)


def test_disabled_renderer_never_runs(render):
    result = asyncio.run(render.render("https://example.com/"))
    assert result == {"success": False, "url": "https://example.com/", "title": "", "content": "",
                      "error": "the page renderer is not enabled"}

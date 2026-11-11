"""OpenWings page renderer: read pages that need JavaScript, for web_fetch and
deep research (patch 0015).

Upstream's web_fetch gives up on pages that build their text with
JavaScript, and its deep research browser fallback calls private_browser
"read", which upstream retired (it always answers page_unavailable). So
single page apps, many news and shop sites and most dashboards came back
empty in research.

OpenWings renders those pages in the `renderer` container: the same locked down
browser image as `browser`, but with no profile volume, so it holds no
logins, cookies or downloads, and a hostile page there can never reach the
person's signed in browser. Each read uses a fresh browser context that is
closed straight after. The agent browser's URL rules (browser_policy) and
the container's egress proxy apply, and private or local addresses are
always refused here, whatever WINGS_BROWSER_ALLOW_PRIVATE says, exactly as
upstream's fetcher refuses them (WINGS_RENDERER_ALLOW_PRIVATE=1 exists for
test installs only). A read has a hard time limit, and a page
that is still a bot check ("Just a moment...") when read is reported as
such, never as the page's content.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import socket

from wings import browser_policy as policy

logger = logging.getLogger("wings.render")

_MAX_CHARS = 300_000
_MIN_WAIT_S = 5.0     # shortest navigation limit a caller can ask for
_HARD_EXTRA_S = 30.0  # time for the page's text after navigation, then give up
_slots: asyncio.Semaphore | None = None
_lock: asyncio.Lock | None = None
_state: dict = {"pw": None, "browser": None}


def enabled() -> bool:
    flag = os.environ.get("WINGS_RENDERER", "").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    if flag in ("1", "true", "yes", "on"):
        return True
    profiles = os.environ.get("WINGS_COMPOSE_PROFILES", "")
    return "browser" in {p.strip().lower() for p in profiles.split(",")}


def worth_rendering(result: dict, text: str) -> bool:
    """Would a real browser read more than the plain fetch did?"""
    error = str(result.get("error") or "")
    if text.strip():
        # A JavaScript app whose server HTML is only a shell.
        return bool(result.get("js_rendered")) and len(text.strip()) < 800
    if not error or error.startswith(("Empty response body", "Page access challenge", "HTTP 403")):
        return True
    return False


def _endpoint() -> tuple[str, int]:
    raw = os.environ.get("WINGS_RENDERER_CDP", "renderer:9223").strip()
    host, _, port = raw.rpartition(":")
    if not host:
        host, port = raw, "9223"
    return host, int(port or 9223)


def _sync_primitives():
    global _slots, _lock
    if _slots is None:
        _slots = asyncio.Semaphore(max(1, int(os.environ.get("WINGS_RENDER_CONCURRENCY", "2") or 2)))
    if _lock is None:
        _lock = asyncio.Lock()


async def _connect():
    _sync_primitives()
    async with _lock:
        browser = _state["browser"]
        try:
            if browser is not None and browser.is_connected():
                return browser
        except Exception:
            pass
        from wings import keyholder
        if not keyholder.RENDERER_TOKEN:
            raise RuntimeError("WINGS_RENDERER_TOKEN is not set; run ./scripts/setup.sh")
        host, port = _endpoint()
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except OSError:
            raise RuntimeError("the renderer container is not running") from None
        ip = infos[0][4][0]
        endpoint = f"http://[{ip}]:{port}" if ":" in ip else f"http://{ip}:{port}"
        if _state["pw"] is None:
            from playwright.async_api import async_playwright
            _state["pw"] = await async_playwright().start()
        browser = await _state["pw"].chromium.connect_over_cdp(
            endpoint, timeout=15000, headers={"X-Wings-Browser-Token": keyholder.RENDERER_TOKEN})
        _state["browser"] = browser
        return browser


def _fail(url: str, error: str) -> dict:
    return {"success": False, "url": url, "title": "", "content": "", "error": error}


# Upstream's interstitial test (services/search/content.py): a short page with
# one of these titles and corroborating text is a bot check, not evidence.
_CHALLENGE_TITLES = {"client challenge", "just a moment", "security verification", "verify you are human"}
_CHALLENGE_TEXT = re.compile(
    r"required part of this site|verify (?:that )?you are human|checking your browser|"
    r"enable javascript|security verification|performing security", re.I)


def is_challenge(title: str, text: str) -> bool:
    name = str(title or "").strip().lower().rstrip(".!")
    body = str(text or "")
    return name in _CHALLENGE_TITLES and len(body) < 2000 and bool(_CHALLENGE_TEXT.search(body))


def _private_ok() -> bool:
    # Separate from the agent browser's setting, and off unless a test
    # install whose test pages sit on a private network turns it on.
    return os.environ.get("WINGS_RENDERER_ALLOW_PRIVATE", "").strip().lower() in ("1", "true", "yes")


async def _check(url: str) -> tuple[bool, str, str]:
    # Public addresses only, and DNS off the event loop.
    ok, reason, normalized = await asyncio.to_thread(policy.check_url, url, private_ok=_private_ok())
    if not ok and "WINGS_BROWSER_ALLOW_PRIVATE" in reason:
        # That switch is the agent browser's; it never opens this to the renderer.
        reason = reason.split(" Set WINGS_BROWSER_ALLOW_PRIVATE", 1)[0] + " The page renderer never reads private or local addresses."
    return ok, reason, normalized


async def render(url: str, *, timeout_s: float = 30.0) -> dict:
    """{"success", "url", "title", "content", "error"} for one page, read
    after its scripts ran. Never raises, and returns within about
    timeout_s + 35 seconds whatever the page does."""
    if not enabled():
        return _fail(url, "the page renderer is not enabled")
    ok, reason, url = await _check(str(url or ""))
    if not ok:
        return _fail(url, f"not opened: {reason}")
    _sync_primitives()
    limit = min(max(timeout_s, _MIN_WAIT_S), 60)
    try:
        await asyncio.wait_for(_slots.acquire(), timeout=limit)
    except asyncio.TimeoutError:
        return _fail(url, "the page renderer is busy; try again shortly")
    try:
        return await asyncio.wait_for(_read(url, int(limit * 1000)), timeout=limit + _HARD_EXTRA_S)
    except asyncio.TimeoutError:
        return _fail(url, "render failed: the page did not finish loading in time")
    finally:
        _slots.release()


async def _read(url: str, timeout_ms: int) -> dict:
    try:
        browser = await _connect()
        context = await browser.new_context(accept_downloads=False, service_workers="block",
                                            locale=os.environ.get("WINGS_BROWSER_LANG", "en-US") or "en-US",
                                            viewport={"width": 1280, "height": 900})
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("renderer unavailable: %s", str(exc)[:200])
        return _fail(url, f"renderer unavailable: {str(exc)[:160]}")
    try:
        page = await context.new_page()
        response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        if response is not None and response.status >= 400:
            return _fail(url, f"HTTP {response.status}")
        try:
            await page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            pass  # pages that poll forever still have their text by now
        title, text = await _page_text(page)
        if is_challenge(title, text):
            # Some checks finish on their own and move to the real page.
            await asyncio.sleep(6)
            title, text = await _page_text(page)
            if is_challenge(title, text):
                return _fail(url, "Page access challenge: the site showed a bot check instead of the page; "
                                  "do not treat it as the page's content")
        final = page.url
        allowed, why, _ = await _check(final)
        if not allowed:
            return _fail(url, f"the page moved to a refused address: {why}")
        if len(text) < 40:
            return _fail(url, "no readable text even after the page's scripts ran")
        return {"success": True, "url": final, "title": title, "content": text, "error": ""}
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if isinstance(exc, TimeoutError):
            return _fail(url, "render failed: the page stopped responding")
        message = str(exc).splitlines()[0][:200] if str(exc) else exc.__class__.__name__
        return _fail(url, f"render failed: {message}")
    finally:
        # Closing the context also disposes a page whose script never yields;
        # shielded so a timeout of the read still closes it.
        try:
            await asyncio.wait_for(asyncio.shield(context.close()), timeout=10)
        except BaseException:
            pass


async def _page_text(page) -> tuple[str, str]:
    from wings.browser import _TEXT_JS
    try:
        text = await asyncio.wait_for(page.main_frame.evaluate(_TEXT_JS), 20)
    except Exception:
        # A page whose script never yields fails here too (bounded).
        text = await asyncio.wait_for(
            page.main_frame.evaluate("() => document.body ? document.body.innerText : ''"), 10)
    title = (await asyncio.wait_for(page.title(), 10) or "")[:300]
    text = re.sub(r"[ \t]+\n", "\n", str(text or ""))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()[:_MAX_CHARS]
    return title, text

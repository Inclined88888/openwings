"""Unit tests for the Wings code runner: the app-side client
(overlay/app/wings/runner.py) and the runner server's path handling
(docker/runner/runner_server.py, loaded from the mounted tests folder when
available). No containers needed."""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import os
import sys

import pytest

sys.path.insert(0, "/app")


@pytest.fixture
def runner(monkeypatch, tmp_path):
    monkeypatch.setenv("WINGS_RUNNER_ROOT", str(tmp_path))
    monkeypatch.setenv("WINGS_RUNNER", "1")
    import wings.runner as r
    return importlib.reload(r)


def test_enabled_follows_profiles(monkeypatch):
    import wings.runner as r
    monkeypatch.delenv("WINGS_RUNNER", raising=False)
    monkeypatch.setenv("WINGS_COMPOSE_PROFILES", "browser,runner")
    assert r.enabled()
    monkeypatch.setenv("WINGS_COMPOSE_PROFILES", "browser")
    assert not r.enabled()


def test_relative_cwd_stays_inside_the_shared_root(runner, tmp_path):
    (tmp_path / "default" / "sub").mkdir(parents=True)
    assert runner.relative_cwd(str(tmp_path)) == ""
    assert runner.relative_cwd(str(tmp_path / "default")) == "default"
    assert runner.relative_cwd(str(tmp_path / "default" / "sub")) == os.path.join("default", "sub")
    assert runner.relative_cwd("/app/data") is None
    assert runner.relative_cwd(str(tmp_path) + "-other") is None
    link = tmp_path / "escape"
    link.symlink_to("/etc")
    assert runner.relative_cwd(str(link)) is None


def test_first_image_ignores_symlinks_and_big_files(runner, tmp_path):
    (tmp_path / "chart.png").write_bytes(b"\x89PNG small")
    (tmp_path / "evil.png").symlink_to("/etc/passwd")
    files = [{"path": "evil.png", "bytes": 10}, {"path": "chart.png", "bytes": 10}]
    image = runner._first_image(str(tmp_path), files)
    assert image and image["path"] == "chart.png" and image["mimeType"] == "image/png"
    assert runner._first_image(str(tmp_path), [{"path": "chart.png", "bytes": 10 * 1024 * 1024}]) is None


def test_refuses_without_token_or_runner(runner, monkeypatch):
    from wings import keyholder
    monkeypatch.setattr(keyholder, "RUNNER_TOKEN", None)
    with pytest.raises(RuntimeError, match="WINGS_RUNNER_TOKEN"):
        asyncio.run(runner._post({"language": "python", "code": "1"}, timeout=1))
    monkeypatch.setattr(keyholder, "RUNNER_TOKEN", "t")
    monkeypatch.setenv("WINGS_RUNNER_URL", "no-such-runner.invalid:8790")
    with pytest.raises(RuntimeError, match="not running"):
        asyncio.run(runner._post({"language": "python", "code": "1"}, timeout=1))


def _server(monkeypatch, root):
    path = "/wings-runner/runner_server.py"
    if not os.path.exists(path):
        pytest.skip("runner_server.py not mounted")
    monkeypatch.setenv("WINGS_RUNNER_ROOT", str(root))
    spec = importlib.util.spec_from_file_location("runner_server", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_server_folder_rejects_escapes(monkeypatch, tmp_path):
    srv = _server(monkeypatch, tmp_path)
    (tmp_path / "default").mkdir()
    (tmp_path / "out").symlink_to("/etc")
    assert srv._folder("") == os.path.realpath(tmp_path)
    assert srv._folder("default") == os.path.realpath(tmp_path / "default")
    for bad in ("../etc", "/etc", "default/../..", "out", "missing", "a\x00b"):
        assert srv._folder(bad) is None, bad


def test_server_runs_code_with_clean_env_and_reports_files(monkeypatch, tmp_path):
    srv = _server(monkeypatch, tmp_path)
    monkeypatch.setenv("SECRET_TOKEN", "should-not-leak")
    (tmp_path / "default").mkdir()
    status, out = asyncio.run(srv.run_code({"language": "python", "cwd": "default", "timeout": 20,
                                            "code": "import os\nopen('a.txt','w').write('x')\nprint(sorted(os.environ))"}))
    assert status == 200 and out["exit_code"] == 0
    assert "SECRET_TOKEN" not in out["stdout"] and "HOME" in out["stdout"]
    assert [f["path"] for f in out["files_changed"]] == ["a.txt"]
    status, out = asyncio.run(srv.run_code({"language": "python", "cwd": "default", "timeout": 1,
                                            "code": "import time; time.sleep(10)"}))
    assert out["timed_out"] and out["exit_code"] == -9
    status, out = asyncio.run(srv.run_code({"language": "ruby", "cwd": "default", "code": "1"}))
    assert status == 400


def test_background_marker_runs_in_the_foreground_with_the_full_limit(runner, monkeypatch, tmp_path):
    import src.tool_execution as te
    root = tmp_path / "ws"
    (root / "default").mkdir(parents=True)
    monkeypatch.setenv("WINGS_RUNNER_ROOT", str(root))
    monkeypatch.setenv("WINGS_RUNNER_MAX_TIMEOUT", "240")
    monkeypatch.setattr(te, "agent_cwd", lambda: str(root / "default"))
    sent = {}

    async def fake_post(payload, timeout):
        sent.update(payload)
        return {"stdout": "done", "stderr": "", "exit_code": 0, "files_changed": []}

    monkeypatch.setattr(runner, "_post", fake_post)
    assert runner.is_background_marked("\n#!bg\nsleep 1")
    assert runner.is_background_marked("@background\nmake")
    assert not runner.is_background_marked("echo #!bg")
    out = asyncio.run(runner.run_owned("@background\npip download x", {}, tool="bash", timeout=30))
    assert sent["timeout"] == 240 and sent["code"] == "pip download x"
    assert "Ran in the foreground" in out["output"] and out["exit_code"] == 0
    out = asyncio.run(runner.run_owned("echo hi", {}, tool="bash", timeout=30))
    assert sent["timeout"] == 30 and "foreground" not in out["output"]


def test_background_marker_is_split_on_newlines_only(runner):
    # bash reads everything up to the newline as one comment line; splitting
    # on other separators would run text the person saw as a comment.
    for sep in ("\r", "\x0b", "\x0c", "\x1c", "\x85", "\u2028", "\u2029"):
        code = f"#!bg{sep}rm -rf ./*\nls -la"
        assert not runner.is_background_marked(code)
        assert runner.split_background_marker(code) == (False, code)
    assert runner.split_background_marker("#!bg\r\nmake all") == (True, "make all")
    assert runner.split_background_marker("\n#!bg\nsleep 1\n") == (True, "sleep 1")

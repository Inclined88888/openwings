"""Wings code runner client: upstream's python and bash tools run here.

Upstream Odysseus runs the agent's code only inside bubblewrap containment,
which Docker's default security profile does not allow, so in a Docker
deployment those tools always refuse. Wings sends the code to the `runner`
container instead (compose profile `runner`): no network, no secrets, all
capabilities dropped, read-only root, and the only writable place is the
agent workspace (the same folder the app's file tools use, shared through the
agent-workspaces volume, mounted at /workspaces in both). See
docker/runner/runner_server.py.

Called from upstream's single execution choke point, _run_owned_command in
src/agent_tools/subprocess_tools.py (patch 0009), after upstream's own checks
(no sudo, no ad-hoc HTTP downloads, resource identity) have passed.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import socket

logger = logging.getLogger("wings.runner")

_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")
_MAX_IMAGE_BYTES = 2 * 1024 * 1024


def enabled() -> bool:
    flag = os.environ.get("WINGS_RUNNER", "").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    if flag in ("1", "true", "yes", "on"):
        return True
    profiles = os.environ.get("WINGS_COMPOSE_PROFILES", "")
    return "runner" in {p.strip().lower() for p in profiles.split(",")}


def shared_root() -> str:
    """The folder both containers see at the same path: the agent-workspaces
    volume (the default chat workspace lives in it, see ODYSSEUS_WORKSPACE_DEFAULT)."""
    return os.path.realpath(os.environ.get("WINGS_RUNNER_ROOT", "/workspaces"))


def relative_cwd(cwd: str) -> str | None:
    """cwd relative to the shared root, or None when the runner cannot see it."""
    root = shared_root()
    real = os.path.realpath(cwd)
    if real == root:
        return ""
    if real.startswith(root + os.sep):
        return os.path.relpath(real, root)
    return None


def split_background_marker(code: str) -> tuple[bool, str]:
    """(True, code without the marker line) when the first non-empty line is
    upstream's background marker (#!bg), else (False, code).

    Lines are split on newline only, exactly as upstream's _split_bg_marker
    and bash do. str.splitlines() also breaks on carriage returns, form feeds
    and Unicode separators, so "#!bg<CR>rm -rf ." would lose only "#!bg" and
    run text that bash, and the person approving it, read as a comment."""
    text = str(code or "")
    try:
        from src.tool_execution import _split_bg_marker
        marked, rest = _split_bg_marker(text)
        return bool(marked), rest if marked else text
    except Exception:
        lines = text.split("\n")
        i = 0
        while i < len(lines) and not lines[i].strip():
            i += 1
        if i < len(lines) and lines[i].strip().lower() in ("#!bg", "#bg", "# bg", "#background",
                                                           "# background", "@background", "# @background"):
            del lines[i]
            return True, "\n".join(lines).strip()
        return False, text


def is_background_marked(code: str) -> bool:
    """First non-empty line is upstream's background marker (#!bg)."""
    return split_background_marker(code)[0]


def _endpoint() -> tuple[str, int]:
    raw = os.environ.get("WINGS_RUNNER_URL", "runner:8790").strip() or "runner:8790"
    host, sep, port = raw.rpartition(":")
    if not sep or not port.isdigit():
        return raw, 8790
    return host, int(port)


async def _post(payload: dict, timeout: float) -> dict:
    from wings import keyholder
    token = keyholder.RUNNER_TOKEN
    if not token:
        raise RuntimeError("WINGS_RUNNER_TOKEN is not set. Run ./scripts/setup.sh, then docker compose up -d.")
    host, port = _endpoint()
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError:
        raise RuntimeError("The code runner is not running. Enable it with ./scripts/setup.sh --with-runner "
                           "(or add 'runner' to COMPOSE_PROFILES in .env and run docker compose up -d).") from None
    ip = infos[0][4][0]
    body = json.dumps(payload).encode()
    reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), 10)
    try:
        writer.write(
            b"POST /run HTTP/1.1\r\nHost: runner\r\nContent-Type: application/json\r\n"
            + b"X-Wings-Runner-Token: " + token.encode() + b"\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), timeout)
    finally:
        writer.close()
    head, _, data = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1]) if head.startswith(b"HTTP/") else 0
    try:
        result = json.loads(data or b"{}")
    except ValueError:
        result = {}
    if status != 200:
        raise RuntimeError(f"code runner answered {status}: {str(result.get('error') or '')[:200]}")
    return result


def _first_image(cwd: str, files: list[dict]) -> dict | None:
    """The first image the code produced, to show in the chat (and to vision models)."""
    for item in files:
        path = str(item.get("path") or "")
        if not path.lower().endswith(_IMAGE_SUFFIXES) or int(item.get("bytes") or 0) > _MAX_IMAGE_BYTES:
            continue
        full = os.path.join(cwd, path)
        try:
            fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            continue
        try:
            st = os.fstat(fd)
            import stat as _stat
            if not _stat.S_ISREG(st.st_mode) or st.st_size > _MAX_IMAGE_BYTES:
                continue
            data = os.read(fd, _MAX_IMAGE_BYTES + 1)
        finally:
            os.close(fd)
        mime = "image/png" if path.lower().endswith(".png") else "image/jpeg" if path.lower().endswith((".jpg", ".jpeg")) \
            else "image/gif" if path.lower().endswith(".gif") else "image/webp"
        return {"mimeType": mime, "data": base64.b64encode(data).decode(), "path": path}
    return None


async def run_owned(command, ctx: dict, *, tool: str, timeout: int, argv: bool = False) -> dict:
    """Run upstream's prepared python/bash command in the runner; upstream result shape."""
    from src.tool_execution import agent_cwd, _truncate
    from src.constants import MAX_OUTPUT_CHARS, WORKSPACE_MOUNT

    if tool not in ("python", "bash"):
        return {"error": f"{tool}: not supported by the code runner", "exit_code": 1}
    cwd = agent_cwd()
    rel = relative_cwd(cwd)
    if rel is None:
        return {"error": (f"{tool}: this chat works in {cwd}, which the code runner cannot see. "
                          "The runner only works inside /workspaces (the default chat workspace is "
                          "/workspaces/default)."), "exit_code": 1}
    if argv:
        code = str(command[-1])
    else:
        code = str(command)
    # The tool contract promises /workspace; the runner mounts the workspace at
    # its real path, so rewrite the alias the same way upstream does.
    code = code.replace(WORKSPACE_MOUNT + "/", cwd.rstrip("/") + "/")
    if code.strip() == WORKSPACE_MOUNT:
        code = cwd
    cap = int(os.environ.get("WINGS_RUNNER_MAX_TIMEOUT", "300") or 300)
    wall = max(5, min(int(timeout or 60), cap))
    # A bash command marked #!bg asked to run detached; Docker has no detached
    # jobs, so it runs now with the runner's full time limit (patch 0014).
    background_asked, stripped = split_background_marker(code) if tool == "bash" else (False, code)
    if background_asked:
        wall = cap
        code = stripped
    try:
        result = await _post({"language": tool, "code": code, "cwd": rel, "timeout": wall}, timeout=wall + 20)
    except (OSError, RuntimeError, asyncio.TimeoutError) as exc:
        return {"error": f"{tool}: {exc}", "exit_code": 1}
    stdout = str(result.get("stdout") or "").rstrip()
    stderr = str(result.get("stderr") or "").rstrip()
    files = [f for f in result.get("files_changed") or [] if isinstance(f, dict)]
    note = ""
    if files:
        from wings.files_routes import download_path
        shown = "\n".join(
            f"  {f.get('path')} ({int(f.get('bytes') or 0)} bytes), download: "
            f"{download_path(os.path.join(rel, str(f.get('path'))) if rel else str(f.get('path')))}"
            for f in files[:20])
        more = f"\n  and {len(files) - 20} more" if len(files) > 20 else ""
        note = (f"\nFiles created or changed in the workspace (the person can download them from these links, "
                f"or from Workspace files in the sidebar):\n{shown}{more}")
    if result.get("timed_out"):
        return {"error": f"{tool}: timed out after {wall}s; the runner stopped it.", "exit_code": 124, "timed_out": True,
                "stdout": _truncate(stdout, MAX_OUTPUT_CHARS), "stderr": _truncate(stderr + note, MAX_OUTPUT_CHARS)}
    if background_asked:
        note += (f"\nRan in the foreground in the code runner (background jobs are not available in this "
                 f"install), with a {wall}s time limit. No follow-up will arrive; this is the full result.")
    output = stdout
    if stderr:
        output = (output + "\nSTDERR: " + stderr).strip()
    output = (output + note).strip() or "(no output)"
    out = {"output": _truncate(output, MAX_OUTPUT_CHARS), "exit_code": int(result.get("exit_code") or 0)}
    image = _first_image(cwd, files)
    if image is not None:
        out["images"] = [{"mimeType": image["mimeType"], "data": image["data"]}]
    return out


async def status() -> dict:
    if not enabled():
        return {"enabled": False}
    host, port = _endpoint()
    try:
        await asyncio.get_running_loop().getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        return {"enabled": True, "reachable": True}
    except OSError:
        return {"enabled": True, "reachable": False}

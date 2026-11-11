#!/usr/bin/env python3
"""Wings code runner: runs the agent's Python and shell code in this
container, never in the Wings app container.

The app sends POST /run with the code; this server runs it as an
unprivileged user in the session's workspace folder with CPU, memory, file
size and process limits and a wall clock timeout, then returns the output
and the files the code created or changed.

Isolation (set in docker-compose.yml): no network at all (internal Docker
network shared with the app only), no secrets in the environment, all
capabilities dropped, no-new-privileges, read-only root filesystem, and only
the workspaces volume is writable.

Every request must come from a container named in WINGS_RUNNER_CLIENTS
(default "odysseus") and carry X-Wings-Runner-Token equal to
WINGS_RUNNER_TOKEN; without a token everything is refused.
"""
import asyncio
import hmac
import json
import os
import re
import resource
import shutil
import signal
import socket
import sys
import time

TOKEN = os.environ.pop("WINGS_RUNNER_TOKEN", "").strip()
CLIENTS = [c.strip() for c in os.environ.get("WINGS_RUNNER_CLIENTS", "odysseus").split(",") if c.strip()]
# The agent-workspaces volume, mounted at the same path as in the app
# container so absolute paths mean the same thing on both sides.
ROOT = os.environ.get("WINGS_RUNNER_ROOT", "/workspaces")
PORT = int(os.environ.get("WINGS_RUNNER_PORT", "8790"))
MAX_TIMEOUT = int(os.environ.get("WINGS_RUNNER_MAX_TIMEOUT", "300"))
MEMORY_MB = int(os.environ.get("WINGS_RUNNER_MEMORY_MB", "2048"))
MAX_OUTPUT = 60_000
MAX_BODY = 2 * 1024 * 1024
CONCURRENCY = asyncio.Semaphore(int(os.environ.get("WINGS_RUNNER_CONCURRENCY", "2")))
_PART_RE = re.compile(r"^[^/\x00]{1,255}$")


def _folder(rel):
    """Absolute folder for a path relative to ROOT; None if it would escape."""
    rel = str(rel or "").strip()
    if rel.startswith("/") or "\x00" in rel:
        return None
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." or not _PART_RE.match(p) for p in parts):
        return None
    root = os.path.realpath(ROOT)
    folder = os.path.realpath(os.path.join(root, *parts))
    if folder != root and not folder.startswith(root + os.sep):
        return None
    return folder if os.path.isdir(folder) else None


def _limits(timeout):
    def apply():
        os.setsid()
        resource.setrlimit(resource.RLIMIT_CPU, (timeout + 5, timeout + 10))
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_MB * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_NOFILE, (512, 512))
        resource.setrlimit(resource.RLIMIT_NPROC, (256, 256))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    return apply


def _snapshot(folder):
    """{relative path: (size, mtime_ns)} for regular files, no symlinks followed."""
    out = {}
    for base, dirs, files in os.walk(folder, followlinks=False):
        dirs[:] = [d for d in dirs if d not in (".tmp", ".cache", "__pycache__")]
        for name in files:
            path = os.path.join(base, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if not os.path.islink(path) and os.path.isfile(path):
                out[os.path.relpath(path, folder)] = (st.st_size, st.st_mtime_ns)
        if len(out) > 5000:
            break
    return out


def _clip(data: bytes) -> tuple[str, bool]:
    text = data.decode("utf-8", "replace")
    if len(text) <= MAX_OUTPUT:
        return text, False
    half = MAX_OUTPUT // 2
    return text[:half] + f"\n... [{len(text) - MAX_OUTPUT} characters cut] ...\n" + text[-half:], True


async def run_code(req):
    language = req.get("language")
    code = req.get("code")
    if language not in ("python", "bash") or not isinstance(code, str) or not code.strip():
        return 400, {"error": "language must be python or bash, and code must be non-empty"}
    folder = _folder(req.get("cwd"))
    if folder is None:
        return 400, {"error": "cwd must be an existing folder inside the agent workspace"}
    timeout = max(1, min(int(req.get("timeout") or 60), MAX_TIMEOUT))
    scratch = os.path.join(folder, ".tmp")
    home = os.path.join(scratch, "home")
    os.makedirs(home, exist_ok=True)
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": home,
        "TMPDIR": scratch,
        "LANG": "C.UTF-8",
        "MPLBACKEND": "Agg",
        "MPLCONFIGDIR": os.path.join(home, ".mpl"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "TZ": os.environ.get("TZ", "Etc/UTC"),
    }
    # The environment above is already clean, so python needs no -I (which
    # would also drop the workspace folder from sys.path).
    argv = ["python3", "-c", code] if language == "python" else ["bash", "--noprofile", "--norc", "-c", code]
    before = _snapshot(folder)
    started = time.monotonic()
    async with CONCURRENCY:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=folder, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            preexec_fn=_limits(timeout))
        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = await proc.communicate()
        else:
            # Children the code left running in the background die with it.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    after = _snapshot(folder)
    changed = sorted(p for p, meta in after.items() if before.get(p) != meta)
    out, cut_out = _clip(stdout or b"")
    err, cut_err = _clip(stderr or b"")
    return 200, {
        "stdout": out, "stderr": err, "exit_code": -9 if timed_out else proc.returncode,
        "timed_out": timed_out, "seconds": round(time.monotonic() - started, 2),
        "files_changed": [{"path": p, "bytes": after[p][0]} for p in changed[:100]],
        "truncated": cut_out or cut_err,
    }


async def _peer_allowed(peer_ip):
    if peer_ip.startswith("::ffff:"):
        peer_ip = peer_ip[7:]
    for name in CLIENTS:
        try:
            for info in await asyncio.get_running_loop().getaddrinfo(name, None, proto=socket.IPPROTO_TCP):
                if info[4][0] == peer_ip:
                    return True
        except OSError:
            pass
    return False


async def _respond(writer, status, payload):
    body = json.dumps(payload).encode()
    reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 413: "Too Large", 500: "Error"}.get(status, "OK")
    writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n"
                 f"Connection: close\r\n\r\n".encode() + body)
    try:
        await writer.drain()
    except ConnectionError:
        pass
    writer.close()


async def handle(reader, writer):
    peer = (writer.get_extra_info("peername") or ("?",))[0]
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 15)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
        writer.close()
        return
    lines = head.decode("latin-1").split("\r\n")
    method, path = (lines[0].split(" ") + ["", ""])[:2]
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    if path == "/health" and method == "GET":
        return await _respond(writer, 200, {"ok": True})
    if not TOKEN or not hmac.compare_digest(headers.get("x-wings-runner-token", ""), TOKEN) or not await _peer_allowed(str(peer)):
        print(f"runner: refused request from {peer}", file=sys.stderr, flush=True)
        return await _respond(writer, 403, {"error": "forbidden"})
    if method != "POST" or path != "/run":
        return await _respond(writer, 404, {"error": "not found"})
    try:
        length = int(headers.get("content-length", "0"))
    except ValueError:
        length = -1
    if length < 0 or length > MAX_BODY:
        return await _respond(writer, 413, {"error": "request too large"})
    try:
        body = await asyncio.wait_for(reader.readexactly(length), 30)
        req = json.loads(body or b"{}")
        status, payload = await run_code(req if isinstance(req, dict) else {})
    except Exception as exc:  # report, never crash the server
        status, payload = 500, {"error": f"{exc.__class__.__name__}: {str(exc)[:300]}"}
    await _respond(writer, status, payload)


async def main():
    os.makedirs(ROOT, exist_ok=True)
    if not shutil.which("bash"):
        print("runner: bash not found", file=sys.stderr)
    server = await asyncio.start_server(handle, host="0.0.0.0", port=PORT, limit=64 * 1024)
    print(f"runner: listening on {PORT}, workspaces in {ROOT}, "
          f"{'token set' if TOKEN else 'NO TOKEN: refusing everything'}", flush=True)
    await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())

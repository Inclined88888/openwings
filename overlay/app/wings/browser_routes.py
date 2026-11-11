"""Live view of the Wings browser, served by the Wings app itself.

  GET  /wings/browser/                 viewer page (sign-in required)
  GET  /wings/browser/live.js|css      viewer code
  GET  /wings/browser/novnc/<file>     noVNC client modules (vendored in this image)
  WS   /wings/browser/websockify       VNC stream, relayed from the browser container
  GET  /api/wings/browser/status       {enabled, allowed, connected} for the sidebar
  GET  /api/wings/browser/downloads    files the browser downloaded (name, size, time)
  GET  /wings/browser/downloads/<name> one of those files, always as an attachment

The browser container publishes no ports. Only this app can reach it (its
guard proxy admits the app container alone, with the shared token), and this
module relays it only
to a signed-in person (session cookie, not an API token or the agent's
internal token) whose account is in WINGS_BROWSER_USERS. The WebSocket is not
covered by the app's HTTP auth middleware, so it checks the session cookie
and the Origin header itself.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response

from wings import browser, browser_policy
from wings.email_gate import is_human_request

logger = logging.getLogger("wings.browser.live")
router = APIRouter()

_STATIC = Path(__file__).with_name("static")
_SESSION_COOKIE = "odysseus_session"
_ASSET_RE = re.compile(r"^(?:core|vendor)/[A-Za-z0-9_./-]{1,200}\.(js|mjs|json|css|svg|png)$")
_TYPES = {"js": "text/javascript", "mjs": "text/javascript", "json": "application/json",
          "css": "text/css", "svg": "image/svg+xml", "png": "image/png"}


def _view_host() -> tuple[str, int]:
    raw = os.environ.get("WINGS_BROWSER_VIEW", "browser:7900").strip() or "browser:7900"
    host, sep, port = raw.rpartition(":")
    if not sep or not port.isdigit():
        return raw, 7900
    return host, int(port)


async def _view_base(scheme: str) -> str:
    host, port = _view_host()
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    ip = infos[0][4][0]
    return f"{scheme}://[{ip}]:{port}" if ":" in ip else f"{scheme}://{ip}:{port}"


def _allowed(request: Request) -> bool:
    return is_human_request(request) and browser_policy.user_allowed(getattr(request.state, "current_user", None))


def _deny(request: Request, why: str):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": why}, status_code=403)
    return PlainTextResponse(why, status_code=403)


@router.get("/api/wings/browser/status")
async def browser_status(request: Request):
    data = await browser.status()
    data["allowed"] = bool(data.get("enabled")) and _allowed(request)
    return data


@router.get("/api/wings/browser/downloads")
async def browser_downloads(request: Request):
    if not browser.enabled() or not _allowed(request):
        return _deny(request, "Not allowed.")
    items = []
    for path in browser.list_downloads()[:200]:
        stat = path.stat()
        items.append({"name": path.name, "size": stat.st_size, "modified": int(stat.st_mtime)})
    return {"downloads": items}


@router.get("/wings/browser/downloads/{name}", include_in_schema=False)
async def browser_download_file(name: str, request: Request):
    if not browser.enabled() or not _allowed(request):
        return _deny(request, "Not allowed.")
    safe = browser.safe_download_name(name)
    try:
        data = browser.read_download_bytes(safe) if safe else None
    except browser.BrowserError:
        data = None
    if data is None:
        return PlainTextResponse("Not found", status_code=404)
    # Downloaded files are untrusted: never render them on the app's origin.
    from urllib.parse import quote
    return Response(data, media_type="application/octet-stream", headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(safe)}",
        "Cache-Control": "no-store", "Content-Security-Policy": "sandbox", "X-Content-Type-Options": "nosniff"})


@router.get("/wings/browser", include_in_schema=False)
async def viewer_redirect(request: Request):
    root = request.scope.get("root_path", "") or ""
    return RedirectResponse(url=root + "/wings/browser/", status_code=307)


@router.get("/wings/browser/", include_in_schema=False)
async def viewer_page(request: Request):
    if not browser.enabled():
        return PlainTextResponse("The Wings browser is not enabled. Run ./scripts/setup.sh --with-browser on the server.",
                                 status_code=404)
    if not _allowed(request):
        return _deny(request, "The live browser is reserved for the accounts in WINGS_BROWSER_USERS.")
    html = (_STATIC / "live.html").read_text(encoding="utf-8")
    return Response(html, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})


@router.get("/wings/browser/{name}", include_in_schema=False)
async def viewer_asset(name: str, request: Request):
    if name not in ("live.js", "live.css"):
        return PlainTextResponse("Not found", status_code=404)
    if not _allowed(request):
        return _deny(request, "Not allowed.")
    media = "text/javascript" if name.endswith(".js") else "text/css"
    return Response((_STATIC / name).read_text(encoding="utf-8"), media_type=media,
                    headers={"Cache-Control": "no-cache"})


@router.get("/wings/browser/novnc/{path:path}", include_in_schema=False)
async def novnc_asset(path: str, request: Request):
    if not browser.enabled() or not _allowed(request):
        return _deny(request, "Not allowed.")
    match = _ASSET_RE.match(path)
    if not match or ".." in path.split("/"):
        return PlainTextResponse("Not found", status_code=404)
    root = (_STATIC / "novnc").resolve()
    target = (root / path).resolve()
    if root not in target.parents or not target.is_file():
        return PlainTextResponse("Not found", status_code=404)
    return Response(target.read_bytes(), media_type=_TYPES[match.group(1)],
                    headers={"Cache-Control": "private, max-age=3600"})


def _origin_ok(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    if not origin:
        return False
    netloc = urlsplit(origin).netloc.lower()
    if netloc and netloc == (websocket.headers.get("host") or "").lower():
        return True
    allowed = [o.strip().rstrip("/").lower() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
    return origin.rstrip("/").lower() in allowed


@router.websocket("/wings/browser/websockify")
async def novnc_socket(websocket: WebSocket):
    auth_manager = getattr(websocket.app.state, "auth_manager", None)
    token = websocket.cookies.get(_SESSION_COOKIE)
    user = None
    try:
        if auth_manager is not None and auth_manager.validate_token(token):
            user = auth_manager.get_username_for_token(token)
    except Exception:
        user = None
    if not browser.enabled() or not user or not browser_policy.user_allowed(user) or not _origin_ok(websocket):
        await websocket.close(code=1008)
        return
    requested = websocket.scope.get("subprotocols") or []
    try:
        from websockets.asyncio.client import connect
        from wings import keyholder
        base = await _view_base("ws")
        upstream = await connect(f"{base}/websockify", subprotocols=["binary"] if "binary" in requested else None,
                                 max_size=None, open_timeout=10, ping_interval=None, proxy=None,
                                 additional_headers={"X-Wings-Browser-Token": keyholder.BROWSER_TOKEN or ""})
    except Exception as exc:
        logger.warning("live view: browser service unreachable: %s", exc.__class__.__name__)
        await websocket.close(code=1011)
        return
    await websocket.accept(subprotocol="binary" if "binary" in requested else None)
    logger.info("live view opened by %s", user)

    async def client_to_upstream():
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            if message.get("bytes") is not None:
                await upstream.send(message["bytes"])
            elif message.get("text") is not None:
                await upstream.send(message["text"])

    async def upstream_to_client():
        async for data in upstream:
            if isinstance(data, bytes):
                await websocket.send_bytes(data)
            else:
                await websocket.send_text(data)

    async def session_still_valid():
        # The relay is not covered by the HTTP auth middleware: end it when the
        # person signs out or the session expires.
        while True:
            await asyncio.sleep(30)
            try:
                if not auth_manager.validate_token(token):
                    return
            except Exception:
                return

    tasks = [asyncio.ensure_future(client_to_upstream()), asyncio.ensure_future(upstream_to_client()),
             asyncio.ensure_future(session_still_valid())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await upstream.close()
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info("live view closed by %s", user)

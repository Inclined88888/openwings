"""Sign in off: no sign in page for a browser on this computer (patch 0029).

OPENWINGS_SIGN_IN=off in .env. Accounts stay on (AUTH_ENABLED=true), so
everything still belongs to the admin account and every feature that needs a
signed-in person keeps working. wings/entrypoint.sh sets WINGS_SIGN_IN_OFF=1
only when the app port is published on 127.0.0.1 (APP_BIND) and the HTTPS
proxy profile is off; app.py then puts this middleware outside every other
one. It does two things.

1. Refuses everything but the app's own page in a browser on this computer:

   * Source address: only this container itself (127.0.0.1, the app calling
     itself) and the gateway of one of the app's Docker networks, which is
     where connections through the published port arrive from. Everything
     else is refused, including OpenWings' own helper containers (the code
     runner runs code a model wrote; the browser container shows any
     website) and any address the app does not recognise.
   * Proxies: a request carrying X-Forwarded-For, Forwarded or similar.
   * DNS rebinding: the Host header must be localhost, 127.0.0.1 or [::1].
   * Other websites open in the same browser: a request that changes
     something, and every websocket, must carry no Origin or exactly the
     address it was sent to, and Sec-Fetch-Site cross-site or same-site is
     refused for those and for any /api request that is not a top level
     page load. Page loads (a provider sending you back after a sign in)
     pass; they cannot read anything back, as with sign in on.

2. Signs that browser in. A page load from the gateway without a valid
   session gets a fresh session for the admin account: the cookie is added
   to the request on its way in and set on the response, exactly what a
   sign in does. Requests that are not page loads (scripts, fetch calls)
   get no session from here and need one as usual.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
import struct
import time

from wings.origin import _norm

logger = logging.getLogger(__name__)

LOCAL_NAMES = {"localhost", "127.0.0.1", "::1"}
PROXY_HEADERS = {
    b"x-forwarded-for", b"x-forwarded-host", b"x-forwarded-proto", b"x-forwarded-port",
    b"forwarded", b"x-real-ip", b"cf-connecting-ip", b"true-client-ip", b"x-client-ip",
}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

MESSAGE = (
    "Sign in is off, so OpenWings only answers its own page in a browser on "
    "this computer (http://localhost:<port>). To use it from anywhere else, "
    "turn sign in back on: remove OPENWINGS_SIGN_IN=off from .env and run "
    "./scripts/upgrade.sh --no-pull."
)


def _route_file_networks(path: str = "/proc/net/route") -> list:
    """IPv4 networks directly attached to this container (not the default route)."""
    nets = []
    try:
        with open(path, encoding="ascii") as fh:
            next(fh, None)
            for line in fh:
                parts = line.split()
                if len(parts) < 8:
                    continue
                dest, gateway, mask = parts[1], parts[2], parts[7]
                if int(mask, 16) == 0 or int(gateway, 16) != 0:
                    continue
                d = socket.inet_ntoa(struct.pack("<L", int(dest, 16)))
                m = socket.inet_ntoa(struct.pack("<L", int(mask, 16)))
                try:
                    nets.append(ipaddress.ip_network(f"{d}/{m}", strict=False))
                except ValueError:
                    continue
    except (OSError, ValueError):
        pass
    return nets


_cache = {"at": 0.0, "nets": []}


def own_networks() -> list:
    """Re-read every few seconds, so a network attached later is known."""
    now = time.monotonic()
    if now - _cache["at"] > 5:
        _cache["nets"] = _route_file_networks()
        _cache["at"] = now
    return _cache["nets"]


def _ip(host):
    try:
        ip = ipaddress.ip_address(str(host).split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip


def is_loopback(host) -> bool:
    ip = _ip(host)
    return bool(ip and ip.is_loopback)


def is_gateway(host, networks=None) -> bool:
    """The first address of one of the app's Docker networks (Docker gives it
    to the gateway): where the published port delivers connections from."""
    ip = _ip(host)
    if ip is None or isinstance(ip, ipaddress.IPv6Address):
        return False
    for net in (own_networks() if networks is None else networks):
        if net.num_addresses > 2 and ip == net.network_address + 1:
            return True
    return False


def client_allowed(host, networks=None) -> bool:
    return bool(host) and (is_loopback(host) or is_gateway(host, networks))


def _host_name(host_header: str) -> str:
    h = (host_header or "").strip().lower()
    if not h or any(c in h for c in "/\\@?# ,"):
        return ""
    if h.startswith("["):
        end = h.find("]")
        return h[1:end] if end > 0 else ""
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def _headers(scope) -> dict:
    out = {}
    for k, v in scope.get("headers") or []:
        k = k.lower()
        if k in out:
            out[k] = None  # sent twice: treated as unusable
        else:
            out[k] = v.decode("latin-1")
    return out


def _is_page_load(scope, headers) -> bool:
    return (
        scope.get("type") == "http"
        and (scope.get("method") or "GET").upper() == "GET"
        and (headers.get(b"sec-fetch-mode") or "").lower() == "navigate"
    )


def refusal(scope, networks=None):
    """Why this request may not reach the app, or None to let it through."""
    kind = scope.get("type")
    if kind not in ("http", "websocket"):
        return None
    headers = _headers(scope)
    if any(k in PROXY_HEADERS for k in headers):
        return "proxied"
    client = scope.get("client")
    if not client_allowed(client[0] if client else None, networks):
        return "client"
    host = headers.get(b"host")
    if host is None or _host_name(host) not in LOCAL_NAMES:
        return "host"
    method = (scope.get("method") or "GET").upper()
    path = scope.get("path") or "/"
    guarded = kind == "websocket" or method not in SAFE_METHODS
    site = (headers.get(b"sec-fetch-site") or "").strip().lower()
    if site in ("cross-site", "same-site"):
        if guarded:
            return "site"
        if (path == "/api" or path.startswith("/api/")) and not _is_page_load(scope, headers):
            return "site"
    if guarded and b"origin" in headers:
        origin = headers[b"origin"]
        scheme = "https" if scope.get("scheme") in ("https", "wss") else "http"
        if origin is None or not _norm(origin) or _norm(origin) != _norm(f"{scheme}://{host}"):
            return "origin"
    return None


def _admin(auth_manager):
    users = getattr(auth_manager, "users", None) or {}
    wanted = (os.environ.get("ODYSSEUS_ADMIN_USER") or os.environ.get("OPENWINGS_ADMIN_USER") or "admin").strip().lower()
    if wanted in users and auth_manager.is_admin(wanted):
        return wanted
    admins = sorted(u for u in users if auth_manager.is_admin(u))
    return admins[0] if admins else None


def _cookie_value(headers, name: str):
    raw = headers.get(b"cookie") or ""
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return None


class LocalOnlyGuard:
    """ASGI middleware, installed by app.py when WINGS_SIGN_IN_OFF=1."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        why = refusal(scope)
        if why is not None:
            return await self._refuse(scope, send, why)
        if scope.get("type") == "http":
            token = self._session_for(scope)
            if token:
                return await self._with_cookie(scope, receive, send, token)
        return await self.app(scope, receive, send)

    def _session_for(self, scope):
        """A new admin session for a page load from the published port that
        has none, or None."""
        client = scope.get("client")
        if not client or not is_gateway(client[0]):
            return None
        headers = _headers(scope)
        if not _is_page_load(scope, headers):
            return None
        try:
            from routes.auth_routes import SESSION_COOKIE
            app = scope.get("app")
            auth_manager = getattr(getattr(app, "state", None), "auth_manager", None)
            if auth_manager is None or not auth_manager.is_configured:
                return None
            current = _cookie_value(headers, SESSION_COOKIE)
            if current and auth_manager.validate_token(current):
                return None
            user = _admin(auth_manager)
            return auth_manager.create_session_trusted(user) if user else None
        except Exception:
            logger.warning("Sign in is off: could not start a session", exc_info=True)
            return None

    async def _with_cookie(self, scope, receive, send, token):
        from core.auth import TOKEN_TTL
        from routes.auth_routes import SESSION_COOKIE
        cookie = f"{SESSION_COOKIE}={token}"
        headers = [(k, v) for k, v in scope.get("headers") or [] if k.lower() != b"cookie"]
        others = [p.strip() for p in (_headers(scope).get(b"cookie") or "").split(";")
                  if p.strip() and not p.strip().startswith(SESSION_COOKIE + "=")]
        headers.append((b"cookie", "; ".join([cookie] + others).encode("latin-1")))
        inner = dict(scope, headers=headers)
        set_cookie = f"{cookie}; Path=/; Max-Age={TOKEN_TTL}; HttpOnly; SameSite=Lax".encode("latin-1")

        async def send_with_cookie(message):
            if message.get("type") == "http.response.start":
                message = dict(message, headers=list(message.get("headers") or []) + [(b"set-cookie", set_cookie)])
            await send(message)

        return await self.app(inner, receive, send_with_cookie)

    async def _refuse(self, scope, send, why):
        client = scope.get("client")
        logger.warning(
            "Sign in is off: refused %s %s from %s (%s)",
            scope.get("method") or scope.get("type"), scope.get("path"),
            client[0] if client else "unknown", why,
        )
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"error": MESSAGE}).encode()
        await send({
            "type": "http.response.start", "status": 403,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})

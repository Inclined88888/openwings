"""Which origin a sign-in should come back to (patch 0021).

A provider's sign-in page sends the browser back to a callback URL on this
app. Inside Docker the app cannot see the host port the browser uses, so a
fixed guess (upstream's http://localhost:7000) lands on a dead port. The rule
here:

1. The origin of the request that started the sign-in (scheme and Host
   header), if it is one this install already trusts: listed in
   ALLOWED_ORIGINS (the same list that gates cross-origin calls) or equal to
   the configured base below. The session cookie is per host, so returning to
   the exact origin the person is using keeps them signed in on the way back.
2. Otherwise the configured base: OAUTH_REDIRECT_BASE_URL, then
   APP_PUBLIC_URL, then http://localhost:<APP_PORT or 7000> (upstream's own
   fallback). docker-compose.yml sets OAUTH_REDIRECT_BASE_URL from APP_PORT.

A Host header that is not on the list is never used, so a crafted Host cannot
point a callback somewhere else.
"""
from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit


def _norm(origin: str) -> str:
    """scheme://host[:port] in lower case, default ports dropped; '' if bad."""
    try:
        parts = urlsplit((origin or "").strip())
    except ValueError:
        return ""
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return ""
    if parts.username or parts.password:
        return ""
    host = parts.hostname.lower()
    try:
        port = parts.port
    except ValueError:
        return ""
    if ":" in host:
        host = f"[{host}]"
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


def configured_base() -> str:
    raw = (
        os.environ.get("OAUTH_REDIRECT_BASE_URL")
        or os.environ.get("APP_PUBLIC_URL")
        or f"http://localhost:{os.environ.get('APP_PORT', '7000')}"
    )
    return _norm(raw) or raw.strip().rstrip("/")


def allowed_origins() -> set[str]:
    out = {configured_base()}
    for item in os.environ.get("ALLOWED_ORIGINS", "").split(","):
        n = _norm(item)
        if n:
            out.add(n)
    return out


def request_origin(request) -> str:
    """The origin the browser used for this request, normalised ('' if none)."""
    try:
        host = request.headers.get("host") or ""
        scheme = request.url.scheme or "http"
    except Exception:
        return ""
    if not host or any(c in host for c in "/\\@?# "):
        return ""
    return _norm(f"{scheme}://{host}")


def redirect_base_for(request) -> str:
    """Origin to send a sign-in back to: the request's own when trusted."""
    origin = request_origin(request) if request is not None else ""
    if origin and origin in allowed_origins():
        return origin
    return configured_base()


def is_loopback_or_https(origin: str) -> bool:
    """True for https origins and for http on localhost / a loopback IP."""
    n = _norm(origin)
    if not n:
        return False
    parts = urlsplit(n)
    if parts.scheme == "https":
        return True
    host = (parts.hostname or "").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False

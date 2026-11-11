"""Connect (patch 0021): use what people already have, in one click.

  GET  /api/wings/connect/status                what the Connect panels show
  POST /api/wings/connect/openrouter/start      begin "Connect OpenRouter"
  GET  /api/wings/connect/openrouter/callback   OpenRouter sends the browser here
  POST /api/wings/connect/openrouter/code       finish by pasting the code
  POST /api/wings/connect/apps/{slug}           Connect Notion / Linear / Jira
  /api/wings/connect/chatgpt/...                Continue with ChatGPT (patch
                                                0022, wings/chatgpt_routes.py)

OpenRouter: its documented OAuth PKCE flow
(https://openrouter.ai/docs/guides/overview/auth/oauth). The browser goes to
https://openrouter.ai/auth with callback_url, an S256 code_challenge and a
random state; OpenRouter returns with ?code=...&state=...; the server sends the
code and the verifier to https://openrouter.ai/api/v1/auth/keys and gets an API
key on the person's own OpenRouter account. OpenRouter accepts callbacks on
https URLs and on localhost at any port; an install reached over plain http on
another address uses OpenRouter's documented no-callback mode instead (the
code is shown on OpenRouter's page and pasted back here). The key becomes a
model endpoint through the very function behind POST /api/model-endpoints, so
it is stored the way upstream stores keys (encrypted at rest) and is never
sent to the browser or written to a log.

Every flow is bound to the signed-in admin who started it: the callback and
the paste step finish only for that same person, only once, and only within
ten minutes (OpenRouter's code lifetime). The callback redirects only to fixed
paths of this app.

Apps: remote MCP servers that let any client register itself (dynamic client
registration) and sign in with PKCE, so Connect needs no setup by the owner.
The existing MCP OAuth client (src/mcp_oauth.py) does the work; this only
creates the server row and starts the connect with the callback on the origin
the browser is using.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from wings.origin import is_loopback_or_https, redirect_base_for, request_origin

logger = logging.getLogger(__name__)

OPENROUTER_AUTH_URL = "https://openrouter.ai/auth"
OPENROUTER_KEYS_URL = "https://openrouter.ai/api/v1/auth/keys"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_CALLBACK_PATH = "/api/wings/connect/openrouter/callback"
KEY_LABEL = "OpenWings"
FLOW_TTL_SECONDS = 600   # OpenRouter codes expire 10 minutes after issue
MAX_PENDING = 64

# One-click app connectors: remote MCP servers with dynamic client
# registration and PKCE (checked against each server's OAuth metadata).
APPS = {
    "notion": {
        "name": "Notion",
        "url": "https://mcp.notion.com/mcp",
        "about": "Search and edit your pages and databases.",
        "note": "",
    },
    "linear": {
        "name": "Linear",
        "url": "https://mcp.linear.app/mcp",
        "about": "Find, create and update issues and projects.",
        "note": "",
    },
    "atlassian": {
        "name": "Jira and Confluence",
        "url": "https://mcp.atlassian.com/v2/mcp",
        "about": "Atlassian: Jira issues and Confluence pages.",
        "note": "On a company Atlassian site, an Atlassian admin may first need to allow new AI tools.",
    },
}


# ---------------------------------------------------------------- PKCE

def new_verifier() -> str:
    """RFC 7636 code verifier: 86 URL-safe characters (43..128 allowed)."""
    return secrets.token_urlsafe(64)


def s256_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def openrouter_authorize_url(challenge: str, *, callback_url: Optional[str], state: Optional[str]) -> str:
    params = {}
    if callback_url:
        params["callback_url"] = callback_url
    params["code_challenge"] = challenge
    params["code_challenge_method"] = "S256"
    if state:
        params["state"] = state
    params["key_label"] = KEY_LABEL
    return f"{OPENROUTER_AUTH_URL}?{urlencode(params)}"


@dataclass
class Flow:
    user: str
    verifier: str
    mode: str                      # "callback" or "paste"
    created: float = field(default_factory=time.monotonic)


_flows: dict[str, Flow] = {}


def _prune(now: Optional[float] = None) -> None:
    now = time.monotonic() if now is None else now
    for k in [k for k, f in _flows.items() if now - f.created > FLOW_TTL_SECONDS]:
        _flows.pop(k, None)
    while len(_flows) > MAX_PENDING:
        oldest = min(_flows, key=lambda k: _flows[k].created)
        _flows.pop(oldest, None)


def start_flow(user: str, mode: str) -> tuple[str, Flow]:
    _prune()
    flow = Flow(user=user, verifier=new_verifier(), mode=mode)
    state = secrets.token_urlsafe(32)
    _flows[state] = flow
    return state, flow


class ConnectError(Exception):
    """A connect step failed; `code` is a fixed, display-safe reason."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def take_flow(state: str, user: Optional[str], mode: Optional[str] = None) -> Flow:
    """Return and consume the pending flow for `state`, only for its owner."""
    _prune()
    flow = _flows.get(state or "")
    if flow is None or (mode and flow.mode != mode):
        raise ConnectError("expired")
    if not user or not secrets.compare_digest(flow.user, user):
        raise ConnectError("other_user")
    _flows.pop(state, None)
    return flow


def _post_json(url: str, payload: dict, timeout: float = 20.0):
    import httpx
    return httpx.post(url, json=payload, timeout=timeout, headers={"Accept": "application/json"})


def exchange_code(code: str, verifier: str, *, post: Optional[Callable] = None) -> str:
    """Trade the authorization code for the person's OpenRouter API key."""
    code = (code or "").strip()
    if not code or len(code) > 512:
        raise ConnectError("no_code")
    try:
        resp = (post or _post_json)(OPENROUTER_KEYS_URL, {
            "code": code,
            "code_verifier": verifier,
            "code_challenge_method": "S256",
        })
    except Exception as exc:
        logger.warning("OpenRouter key exchange failed: %s", type(exc).__name__)
        raise ConnectError("exchange_failed") from None
    status = getattr(resp, "status_code", 0)
    if status != 200:
        logger.warning("OpenRouter key exchange refused (HTTP %s)", status)
        raise ConnectError("expired" if status == 403 else "exchange_failed")
    try:
        key = (resp.json() or {}).get("key")
    except Exception:
        key = None
    if not isinstance(key, str) or not key.strip() or len(key) > 512:
        raise ConnectError("exchange_failed")
    return key.strip()


# ---------------------------------------------------------------- helpers

def _person_admin(request: Request) -> str:
    """The signed-in admin behind this browser session, or HTTP 403."""
    from wings.email_gate import is_human_request
    if not is_human_request(request):
        raise HTTPException(403, "Only a signed-in person can connect accounts.")
    from core.middleware import require_admin
    require_admin(request)
    return getattr(request.state, "current_user", "") or ""


def _is_person_admin(request: Request) -> bool:
    try:
        _person_admin(request)
        return True
    except HTTPException:
        return False


def _create_endpoint_route(request: Request):
    for route in request.app.router.routes:
        if getattr(route, "path", "") == "/api/model-endpoints" and "POST" in (getattr(route, "methods", None) or ()):
            return route.endpoint
    return None


async def add_openrouter_endpoint(request: Request, key: str) -> dict:
    """Create the model endpoint through upstream's own create function
    (validation, dedupe, encrypted key column, default-model pick)."""
    create = _create_endpoint_route(request)
    if create is None:
        raise ConnectError("save_failed")
    from starlette.concurrency import run_in_threadpool
    try:
        result = await run_in_threadpool(
            create,
            request=request,
            name="OpenRouter",
            base_url=OPENROUTER_BASE_URL,
            api_key=key,
            skip_probe="false",
            require_models="false",
            model_type="llm",
            endpoint_kind="api",
            model_refresh_mode="auto",
            model_refresh_interval="",
            model_refresh_timeout="15",
            supports_tools="",
            pinned_models="",
            container_local="false",
            shared="true",
        )
    except HTTPException as exc:
        logger.warning("OpenRouter endpoint not saved (HTTP %s)", exc.status_code)
        raise ConnectError("save_failed") from None
    result = dict(result or {})
    return {
        "id": result.get("id"),
        "name": result.get("name") or "OpenRouter",
        "models": len(result.get("models") or []),
        "online": bool(result.get("online")),
    }


def _model_endpoint_count() -> int:
    try:
        from core.database import ModelEndpoint, SessionLocal
        db = SessionLocal()
        try:
            return db.query(ModelEndpoint).count()
        finally:
            db.close()
    except Exception:
        return -1


def _has_models(ep) -> bool:
    import json
    for column in ("cached_models", "pinned_models"):
        try:
            ids = json.loads(getattr(ep, column, None) or "[]")
        except Exception:
            ids = []
        if isinstance(ids, list) and any(ids):
            return True
    return False


def _usable_endpoint_count(owner: Optional[str] = None, admin: bool = True) -> int:
    """Turned-on model endpoints that have at least one model. An endpoint
    added with a key the provider refused has none, so the first-run panel
    stays up until something actually works. For a member, only the
    endpoints they can use (shared ones and their own). A switched-off
    sign-in (wings/unofficial.py) does not count."""
    try:
        from core.database import ModelEndpoint, SessionLocal
        db = SessionLocal()
        try:
            q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
            if not admin:
                q = q.filter((ModelEndpoint.owner.is_(None)) | (ModelEndpoint.owner == (owner or "")))
            from wings.unofficial import url_blocked
            return sum(1 for ep in q.all() if _has_models(ep) and not url_blocked(ep.base_url))
        finally:
            db.close()
    except Exception:
        return -1


def openrouter_callback_base(request: Request) -> Optional[str]:
    """The origin OpenRouter may send this browser back to, or None.

    OpenRouter calls back only to https or localhost addresses, and the
    callback must reach the browser that started the sign-in: a trusted
    origin on the same host the browser is using. Otherwise (a LAN address
    over plain http, or an address this install does not list) the sign-in
    uses OpenRouter's code-on-screen mode instead."""
    from urllib.parse import urlsplit
    base = redirect_base_for(request)
    here = request_origin(request)
    if not here or urlsplit(base).netloc != urlsplit(here).netloc:
        return None
    return base if is_loopback_or_https(base) else None


def _back(params: dict) -> RedirectResponse:
    # Fixed path of this app only: never a URL taken from the request.
    return RedirectResponse("/?" + urlencode(params), status_code=303)


# ---------------------------------------------------------------- access log

# Sign-in callbacks carry a one-time authorization code and state in the
# query string; the access log keeps only the path (patch 0022).
CALLBACK_PATHS = (
    "/api/wings/connect/chatgpt/callback",
    OPENROUTER_CALLBACK_PATH,
    "/api/mcp/oauth/callback",
)


class CallbackQueryFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        # uvicorn.access: (client, method, path with query, http version, status)
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            for path in CALLBACK_PATHS:
                if args[2].startswith(path + "?"):
                    record.args = args[:2] + (path + "?[redacted]",) + args[3:]
                    break
        return True


def install_access_log_filter() -> None:
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, CallbackQueryFilter) for f in access.filters):
        access.addFilter(CallbackQueryFilter())


# ---------------------------------------------------------------- routes

def setup_connect_routes(mcp_manager=None) -> APIRouter:
    install_access_log_filter()
    router = APIRouter(tags=["wings-connect"])
    from wings.chatgpt_routes import setup_chatgpt_routes
    router.include_router(setup_chatgpt_routes())

    @router.get("/api/wings/connect/status")
    def connect_status(request: Request):
        if not _is_person_admin(request):
            # A member sees no Connect panel; the welcome screen only says
            # whom to ask while there is no AI they can use.
            user = getattr(request.state, "current_user", None)
            return {"admin": False, "ai_ready": _usable_endpoint_count(user, admin=False) != 0}
        from wings.unofficial import allowed as unofficial_allowed
        from wings.chatgpt_routes import status_payload as chatgpt_status
        return {
            "admin": True,
            "chatgpt": chatgpt_status(request, getattr(request.state, "current_user", "") or ""),
            "model_endpoints": _model_endpoint_count(),
            "usable_endpoints": _usable_endpoint_count(),
            "unofficial_signin": unofficial_allowed(),
            "openrouter_mode": "callback" if openrouter_callback_base(request) else "paste",
            "apps": [
                {"slug": slug, "name": a["name"], "url": a["url"], "about": a["about"], "note": a["note"]}
                for slug, a in APPS.items()
            ],
        }

    @router.post("/api/wings/connect/openrouter/start")
    def openrouter_start(request: Request):
        user = _person_admin(request)
        origin = openrouter_callback_base(request)
        if origin:
            state, flow = start_flow(user, "callback")
            url = openrouter_authorize_url(
                s256_challenge(flow.verifier),
                callback_url=origin + OPENROUTER_CALLBACK_PATH,
                state=state,
            )
            return {"mode": "callback", "authorize_url": url}
        # No callback that would reach this browser: OpenRouter's page shows
        # the code for the person to paste here (PKCE still applies).
        state, flow = start_flow(user, "paste")
        url = openrouter_authorize_url(s256_challenge(flow.verifier), callback_url=None, state=None)
        return {"mode": "paste", "authorize_url": url, "flow": state}

    @router.get("/api/wings/connect/openrouter/callback")
    async def openrouter_callback(request: Request, code: str = "", state: str = ""):
        if not _is_person_admin(request):
            return _back({"ow_connect_error": "not_admin"})
        if not code:
            return _back({"ow_connect_error": "denied"})
        user = getattr(request.state, "current_user", "") or ""
        try:
            flow = take_flow(state, user, mode="callback")
            key = exchange_code(code, flow.verifier)
            await add_openrouter_endpoint(request, key)
        except ConnectError as exc:
            return _back({"ow_connect_error": exc.code})
        return _back({"ow_connect": "openrouter"})

    @router.post("/api/wings/connect/openrouter/code")
    async def openrouter_code(request: Request):
        user = _person_admin(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            flow = take_flow(str(body.get("flow") or ""), user, mode="paste")
            key = exchange_code(str(body.get("code") or ""), flow.verifier)
            endpoint = await add_openrouter_endpoint(request, key)
        except ConnectError as exc:
            return JSONResponse({"ok": False, "error": exc.code}, status_code=400)
        return {"ok": True, "endpoint": endpoint}

    @router.post("/api/wings/connect/apps/{slug}")
    async def connect_app(slug: str, request: Request):
        user = _person_admin(request)
        app_entry = APPS.get(slug)
        if app_entry is None:
            raise HTTPException(404, "Unknown app")
        if mcp_manager is None:
            raise HTTPException(503, "Tool servers are not available")
        from core.database import McpServer, SessionLocal
        url = app_entry["url"]
        db = SessionLocal()
        try:
            srv = (
                db.query(McpServer)
                .filter(McpServer.url.in_([url, url + "/"]))
                .order_by(McpServer.created_at)
                .first()
            )
            if srv is None:
                srv = McpServer(
                    id=str(uuid.uuid4())[:8], name=app_entry["name"], transport="http",
                    command=None, args="[]", env="{}", url=url, is_enabled=True,
                )
                db.add(srv)
            else:
                srv.is_enabled = True
                srv.transport = "http"
            db.commit()
            server_id, name = srv.id, srv.name
        finally:
            db.close()

        from src.mcp_oauth import redirect_uri_for_request, set_flow_context
        await mcp_manager.disconnect_server(server_id)
        set_flow_context(server_id, redirect_uri=redirect_uri_for_request(request), owner=user)
        connected = await mcp_manager.connect_server(
            server_id=server_id, name=name, transport="http",
            command=None, args=[], env={}, url=url,
        )
        status = mcp_manager.get_server_status(server_id) or {}
        return {
            "id": server_id,
            "slug": slug,
            "connected": bool(connected),
            "status": status.get("status", "disconnected"),
            "auth_url": status.get("auth_url"),
            "tool_count": status.get("tool_count", 0),
            "error": status.get("error"),
        }

    return router

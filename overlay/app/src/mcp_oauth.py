"""mcp_oauth.py — generic OAuth for remote (Streamable HTTP) MCP servers.

Bridges the mcp SDK's OAuthClientProvider (RFC 9728 discovery, Dynamic Client
Registration, authorization-code + PKCE, token refresh) to Odysseus's web
callback route. Tokens and the dynamic registration persist per-server,
encrypted, so the interactive flow runs only once.

Wings (patch 0021): a flow started from the web app returns to the origin the
browser is using (wings/origin.py) instead of one fixed guess, and remembers
who started it: the callback completes only for that signed-in person (an
admin for flows started by the app itself, e.g. a reconnect at startup).
"""
import asyncio
import json
import logging
import os
import time
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)


def _resolve_redirect_base() -> str:
    """Origin the browser is sent back to after authorizing.

    Falls back to the port the app binds natively (APP_PORT, read the same way
    by app.py and launcher.py) rather than a fixed 7000: the macOS launcher
    defaults to 7860, and a callback on the wrong port reaches nothing. The
    hostname stays `localhost` rather than internal_api_base()'s 127.0.0.1 —
    this URI is registered with the authorization server (via DCR, or by hand
    for Google clients), so changing the host invalidates registrations that
    already exist.
    """
    return (
        os.environ.get("OAUTH_REDIRECT_BASE_URL")
        or os.environ.get("APP_PUBLIC_URL")
        or f"http://localhost:{os.environ.get('APP_PORT', '7000')}"
    ).rstrip("/")


# OAuth redirect URI registered with every authorization server via DCR. Loopback
# is allowed for native/desktop clients (RFC 8252); remote users finish via the
# paste-back flow. Deployments whose externally reachable origin differs from the
# port Odysseus binds — reverse proxy, public domain, or Docker, whose host port
# map is invisible inside the container — must set OAUTH_REDIRECT_BASE_URL (or
# APP_PUBLIC_URL), otherwise the redirect never lands back on Odysseus.
_REDIRECT_BASE = _resolve_redirect_base()
REDIRECT_URI = f"{_REDIRECT_BASE}/api/mcp/oauth/callback"

# How long the background connect waits for the user to authorize before giving up.
AUTH_WAIT_SECONDS = 300

_pending: Dict[str, asyncio.Future] = {}   # state -> Future[(code, state)]
_pending_ts: Dict[str, float] = {}         # state -> monotonic timestamp, for pruning
_auth_urls: Dict[str, str] = {}            # server_id -> authorization URL
# Wings: who started each flow, and where it must come back to.
_pending_owner: Dict[str, Optional[str]] = {}  # state -> username (None = started by the app)
_flow_ctx: Dict[str, dict] = {}                # server_id -> {"redirect_uri", "owner"}


def set_flow_context(server_id: str, redirect_uri: Optional[str] = None, owner: Optional[str] = None) -> None:
    """Record, before a connect, the callback URL and the person starting it.

    Consumed by the next build_provider() for this server. Without it (a
    connect the app starts by itself) the redirect URI already registered with
    the server is reused and only an admin may complete the callback."""
    _flow_ctx[server_id] = {"redirect_uri": redirect_uri or None, "owner": owner or None}


def redirect_uri_for_request(request) -> str:
    """Callback URL on the origin this request came from, when trusted."""
    from wings.origin import redirect_base_for
    return f"{redirect_base_for(request)}/api/mcp/oauth/callback"


def pending_owner(state: str) -> Tuple[bool, Optional[str]]:
    """(is_pending, owner) for a state value."""
    return (state in _pending, _pending_owner.get(state))


def _prune_stale() -> None:
    """Drop abandoned flows whose authorization window has elapsed so the
    module-level registries don't grow unbounded (e.g. a user who never
    finishes the browser step)."""
    now = time.monotonic()
    for state in [s for s, ts in _pending_ts.items() if now - ts > AUTH_WAIT_SECONDS]:
        fut = _pending.pop(state, None)
        _pending_ts.pop(state, None)
        _pending_owner.pop(state, None)
        if fut is not None and not fut.done():
            fut.cancel()


def _discard_pending(state: Optional[str]) -> None:
    if state is None:
        return
    _pending.pop(state, None)
    _pending_ts.pop(state, None)
    _pending_owner.pop(state, None)


def register_pending(state: str, owner: Optional[str] = None) -> asyncio.Future:
    _prune_stale()
    fut = asyncio.get_running_loop().create_future()
    _pending[state] = fut
    _pending_ts[state] = time.monotonic()
    _pending_owner[state] = owner or None
    return fut


def may_finish(state: str, user: Optional[str] = None, is_admin: bool = False) -> bool:
    """True when `user` may finish (or cancel) the waiting flow for `state`.

    A flow started by a signed-in person: only that person. One the app, an
    API token or the agent started (owner None): only an admin's own browser
    (the caller decides what counts as that, routes/mcp/mcp_routes.py)."""
    fut = _pending.get(state)
    if fut is None or fut.done():
        return False
    owner = _pending_owner.get(state)
    if owner is not None:
        if user != owner:
            logger.warning("MCP OAuth: callback for a flow started by another user refused")
            return False
    elif not is_admin:
        logger.warning("MCP OAuth: callback for an app-started flow refused (not an admin)")
        return False
    return True


def resolve_pending(state: str, code: str, user: Optional[str] = None, is_admin: bool = False) -> bool:
    """Hand the code to the waiting flow, only for the person who started it."""
    if not may_finish(state, user, is_admin):
        return False
    _pending[state].set_result((code, state))
    return True


def server_for_state(state: str) -> Optional[str]:
    """The server whose waiting sign-in carries this state, if any."""
    if not state:
        return None
    for server_id, url in list(_auth_urls.items()):
        if (parse_qs(urlparse(url).query).get("state") or [None])[0] == state:
            return server_id
    return None


def pop_auth_url(server_id: str) -> Optional[str]:
    return _auth_urls.get(server_id)


def clear_auth_url(server_id: str) -> None:
    _auth_urls.pop(server_id, None)


class DbTokenStorage:
    """SDK TokenStorage backed by the encrypted McpServer.oauth_tokens column."""

    def __init__(self, server_id: str, session_factory=None):
        self.server_id = server_id
        if session_factory is None:
            from core.database import SessionLocal
            session_factory = SessionLocal
        self._sf = session_factory
        self._required_redirect: Optional[str] = None

    def require_redirect_uri(self, redirect_uri: str) -> None:
        """Wings: a stored registration made for another callback URL is not
        reused (the server would refuse the new redirect); the SDK then
        registers again for this one. Tokens are kept, so a still-valid
        access token works without any sign-in."""
        self._required_redirect = redirect_uri

    def _load(self) -> dict:
        from core.database import McpServer
        db = self._sf()
        try:
            srv = db.query(McpServer).filter(McpServer.id == self.server_id).first()
            if srv and srv.oauth_tokens:
                parsed = json.loads(srv.oauth_tokens)
                if isinstance(parsed, dict):
                    return parsed
        finally:
            db.close()
        return {}

    def _update(self, key: str, value: dict) -> None:
        """Load, set one key, and persist the oauth_tokens JSON in a single
        session/commit (avoids the load+save double round-trip per write)."""
        from core.database import McpServer
        db = self._sf()
        try:
            srv = db.query(McpServer).filter(McpServer.id == self.server_id).first()
            if srv is None:
                return
            data = json.loads(srv.oauth_tokens) if srv.oauth_tokens else {}
            if not isinstance(data, dict):
                data = {}
            data[key] = value
            srv.oauth_tokens = json.dumps(data)
            db.commit()
        finally:
            db.close()

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        data = self._load().get("tokens")
        return OAuthToken.model_validate(data) if data else None

    async def set_tokens(self, tokens) -> None:
        self._update("tokens", json.loads(tokens.model_dump_json()))

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        data = self._load().get("client_info")
        if data and self._required_redirect:
            registered = [str(u) for u in (data.get("redirect_uris") or [])]
            if self._required_redirect not in registered:
                return None
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def set_client_info(self, client_info) -> None:
        self._update("client_info", json.loads(client_info.model_dump_json()))


def build_provider(server_id: str, url: str, on_redirect=None):
    """Construct an OAuthClientProvider that drives the browser flow via the
    Odysseus callback route.

    on_redirect(authorization_url): optional sync callback invoked the moment
    the authorization URL is known (after discovery + DCR). The manager uses it
    to publish 'needs_auth' + auth_url to connection state regardless of how
    long discovery/DCR took.
    """
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata

    # Wings: the callback goes to the origin the person is using when the flow
    # was started from the web app; otherwise to the one already registered
    # with this server (so a background reconnect can still refresh); else the
    # configured default.
    ctx = _flow_ctx.pop(server_id, None) or {}
    owner = ctx.get("owner")
    storage = DbTokenStorage(server_id)
    redirect_uri = ctx.get("redirect_uri")
    explicit_redirect = bool(redirect_uri)
    if not redirect_uri:
        try:
            registered = (storage._load().get("client_info") or {}).get("redirect_uris") or []
            redirect_uri = str(registered[0]) if registered else None
        except Exception:
            redirect_uri = None
    redirect_uri = redirect_uri or REDIRECT_URI
    if explicit_redirect:
        storage.require_redirect_uri(redirect_uri)

    client_metadata = OAuthClientMetadata(
        client_name="OpenWings",
        redirect_uris=[redirect_uri],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        # Leave scope unset: the SDK applies the MCP scope-selection strategy and
        # overwrites this from the server's WWW-Authenticate / protected-resource
        # metadata before building the auth URL. Hardcoding an OIDC scope here
        # would break the many MCP servers that are not OpenID providers.
        scope=None,
        token_endpoint_auth_method="none",
    )

    async def redirect_handler(authorization_url: str) -> None:
        state = (parse_qs(urlparse(authorization_url).query).get("state") or [None])[0]
        if state:
            register_pending(state, owner=owner)
        _auth_urls[server_id] = authorization_url
        if on_redirect is not None:
            try:
                on_redirect(authorization_url)
            except Exception as e:
                logger.warning(f"MCP OAuth on_redirect callback failed: {e}")
        logger.info(f"MCP OAuth: server {server_id} awaiting authorization (state={state})")

    async def callback_handler() -> Tuple[str, Optional[str]]:
        auth_url = _auth_urls.get(server_id)
        state = (parse_qs(urlparse(auth_url).query).get("state") or [None])[0] if auth_url else None
        fut = _pending.get(state)
        if fut is None:
            raise RuntimeError("No pending OAuth flow for this server")
        try:
            code, ret_state = await asyncio.wait_for(fut, timeout=AUTH_WAIT_SECONDS)
            return code, ret_state
        finally:
            _discard_pending(state)
            _auth_urls.pop(server_id, None)

    return OAuthClientProvider(
        server_url=url,
        client_metadata=client_metadata,
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )

"""Connect your accounts (patch 0025): shared plumbing for sign-ins that act
for one person.

Patch 0025 builds the free connections on top of this (wings/accounts_mail.py,
wings/ics_feeds.py, wings/accounts_google.py) and patch 0026 adds Microsoft
the same way. Everything a provider needs lives here once:

1. Install-wide OAuth clients, one per provider (`get_client`, `save_client`,
   `delete_client`, `client_status`). Table wings_oauth_clients; the client
   secret is an EncryptedText column (upstream's Fernet key), never logged and
   never sent to the browser (`client_status` shows a masked id only). Only an
   admin may set or remove one (the routes check; nothing here trusts the
   caller). A provider may also read a client from .env (`env_client`), which
   keeps upstream's GOOGLE_OAUTH_* setup working; a stored client wins.
   A public client (no secret, Microsoft) stores client_secret = None.

2. Per-person tokens (`save_link`, `get_link`, `links_for`, `access_token`,
   `remove_link`). Table wings_account_links, one row per person, provider
   and account address; access and refresh tokens are EncryptedText. Only
   the person who signed in gets a token back (`access_token(link_id,
   owner)` refuses everyone else, admins included). Refresh happens near
   expiry, one at a time per link; a refused refresh (invalid_grant) clears
   the tokens and marks the link "needs sign-in". `remove_link` revokes at the
   provider when it has a revocation endpoint, then deletes the row.

3. Sign-in flows (`start_flow`, `peek_flow`, `take_flow`, `authorize_url`,
   `exchange_code`). Each attempt has a random state, an S256 PKCE pair,
   the person who started it, the origin the browser used, the exact
   redirect URI and the scopes. It is used once and expires after ten
   minutes; only its owner can finish it.

4. The origin bounce (`bounce`). A registered redirect such as
   http://localhost:7011/... can differ from the address the person uses
   (http://127.0.0.1:7011), and session cookies are per host. When the
   callback arrives on another origin than the one stored in the flow, the
   browser is sent, with the same path and query, to the stored origin
   (only if this install still trusts it, wings/origin.py) and the flow
   finishes there with the person's session. The target never comes from
   the request.

5. Assistant tools for one person (`register_tool`, `install_tools`). A tool
   is a function schema, an async handler(args, owner) and the words that
   make a request about it. `install_tools` adds them to upstream's tool
   catalog, handler registry and turn families once at start-up; the handler
   always receives the signed-in person of the chat as `owner` and must act
   for that person only.

6. Test endpoints (`test_url`, `mail_target`). WINGS_TEST_ENDPOINTS (an env
   variable that exists only for tests; nothing in the UI reads or shows it)
   maps real provider URL prefixes and mail host:port pairs to local stub
   servers, so every flow can be proven end to end without real accounts.
   It and WINGS_TEST_ALLOW_HOSTS are honoured only together with
   WINGS_TEST_MODE=1, and the app logs a warning at start-up when they are
   active, so a stray variable in a real install does nothing.

7. Mail host pinning (`MAIL_TARGETS`, `mail_token`): a provider's mail token
   is only handed out for an account whose IMAP and SMTP hosts are that
   provider's own mail servers, so a token can never be sent to another
   server.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urlencode, urlsplit

logger = logging.getLogger(__name__)

FLOW_TTL_SECONDS = 600
MAX_PENDING = 512             # backstop for everyone together
MAX_PENDING_PER_OWNER = 5     # one person cannot push out anyone else's sign-in
REFRESH_SKEW_SECONDS = 120
HTTP_TIMEOUT = 20.0
REAUTH_ERRORS = frozenset({"invalid_grant", "invalid_token", "unauthorized_client"})


class AccountError(Exception):
    """A step failed; `code` is fixed and safe to show, `message` is plain
    words for a person."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


class NeedsSignIn(AccountError):
    def __init__(self, message: str = "Sign in again: Settings > Integrations > Connect your accounts."):
        super().__init__("needs_signin", message)


# ------------------------------------------------------------------ test endpoints

def test_mode() -> bool:
    """True only in a test stack (WINGS_TEST_MODE=1): the test hooks below
    are ignored everywhere else."""
    return os.environ.get("WINGS_TEST_MODE", "").strip() == "1"


def test_hooks_active() -> bool:
    return test_mode() and bool(os.environ.get("WINGS_TEST_ENDPOINTS", "").strip()
                                or os.environ.get("WINGS_TEST_ALLOW_HOSTS", "").strip())


def warn_test_hooks() -> None:
    """Called at start-up: say loudly when the test hooks are on, and when
    they are set but ignored (no WINGS_TEST_MODE=1)."""
    set_any = bool(os.environ.get("WINGS_TEST_ENDPOINTS", "").strip()
                   or os.environ.get("WINGS_TEST_ALLOW_HOSTS", "").strip())
    if test_hooks_active():
        logger.warning("TEST MODE: WINGS_TEST_ENDPOINTS / WINGS_TEST_ALLOW_HOSTS are active. Sign-ins, "
                       "tokens and mail logins go to the test stubs they name. Never use this in a real install.")
    elif set_any:
        logger.warning("WINGS_TEST_ENDPOINTS / WINGS_TEST_ALLOW_HOSTS are set but ignored "
                       "(they work only with WINGS_TEST_MODE=1, in a test stack).")


def _test_map() -> Dict[str, str]:
    if not test_mode():
        return {}
    raw = os.environ.get("WINGS_TEST_ENDPOINTS", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("WINGS_TEST_ENDPOINTS is not JSON; ignored")
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def test_url(url: str) -> str:
    """The URL to call: `url`, or its test stub when WINGS_TEST_ENDPOINTS maps
    a prefix of it (tests only)."""
    for prefix, target in _test_map().items():
        if "://" in prefix and url.startswith(prefix):
            return target.rstrip("/") + url[len(prefix.rstrip("/")):]
    return url


def mail_target(host: str, port: int, security: str) -> Tuple[str, int, str]:
    """(host, port, security) to use for a provider's mail server; tests may
    map "imap.gmail.com:993" to "mailstub:1143:none"."""
    target = _test_map().get(f"{host}:{int(port)}")
    if target:
        parts = target.split(":")
        if len(parts) == 3 and parts[1].isdigit():
            return parts[0], int(parts[1]), parts[2]
    return host, int(port), security


def test_hosts_allowed() -> set[str]:
    """Host names tests may reach although they resolve to private space
    (test mode only)."""
    if not test_mode():
        return set()
    return {h.strip().lower() for h in os.environ.get("WINGS_TEST_ALLOW_HOSTS", "").split(",") if h.strip()}


# ------------------------------------------------------------------ providers

@dataclass(frozen=True)
class Provider:
    name: str                      # "google"
    label: str                     # "Google"
    authorize_url: str
    token_url: str
    callback_path: str             # fixed path registered with the provider
    revoke_url: Optional[str] = None
    userinfo_url: Optional[str] = None
    authorize_extra: Tuple[Tuple[str, str], ...] = ()
    public_client: bool = False    # no client secret (Microsoft's public client)
    env_client: Optional[Callable[[], Optional["Client"]]] = None
    built_in_client: Optional[Callable[[], Optional["Client"]]] = None


PROVIDERS: Dict[str, Provider] = {}


def register_provider(provider: Provider) -> Provider:
    PROVIDERS[provider.name] = provider
    return provider


def provider(name: str) -> Provider:
    p = PROVIDERS.get(name)
    if p is None:
        raise AccountError("unknown_provider", "This kind of account is not available.")
    return p


# ------------------------------------------------------------------ storage

_models: Dict[str, Any] = {}
_models_lock = threading.Lock()
_tables_made: Dict[Any, set] = {}


def _db():
    from core.database import SessionLocal
    return SessionLocal()


def _model(kind: str):
    """OpenWings' own tables (made on first use; upstream's are unchanged)."""
    with _models_lock:
        cls = _models.get(kind)
        if cls is None:
            from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text
            from core.database import Base, EncryptedText, utcnow_naive
            if kind == "client":
                class WingsOAuthClient(Base):
                    __tablename__ = "wings_oauth_clients"
                    __table_args__ = {"extend_existing": True}
                    provider = Column(String, primary_key=True)
                    client_id = Column(String, nullable=False)
                    client_secret = Column(EncryptedText, nullable=True)
                    redirect_uri = Column(String, nullable=True)
                    updated_by = Column(String, nullable=True)
                    updated_at = Column(DateTime, default=utcnow_naive, onupdate=utcnow_naive)
                cls = WingsOAuthClient
            elif kind == "link":
                class WingsAccountLink(Base):
                    __tablename__ = "wings_account_links"
                    __table_args__ = {"extend_existing": True}
                    id = Column(String, primary_key=True)
                    owner = Column(String, nullable=False, index=True)
                    provider = Column(String, nullable=False, index=True)
                    account = Column(String, nullable=False, default="")
                    display_name = Column(String, nullable=True)
                    scopes = Column(Text, nullable=True)
                    features = Column(Text, nullable=True)      # JSON: what this sign-in was for
                    access_token = Column(EncryptedText, nullable=True)
                    refresh_token = Column(EncryptedText, nullable=True)
                    expires_at = Column(Integer, nullable=True)
                    needs_signin = Column(Boolean, default=False)
                    created_at = Column(DateTime, default=utcnow_naive)
                    updated_at = Column(DateTime, default=utcnow_naive, onupdate=utcnow_naive)
                cls = WingsAccountLink
            else:
                raise KeyError(kind)
            _models[kind] = cls
    from core.database import engine
    made = _tables_made.setdefault(engine, set())
    if kind not in made:
        cls.__table__.create(bind=engine, checkfirst=True)
        made.add(kind)
    return cls


def register_model(kind: str, factory: Callable[[], Any]):
    """Let a feature module add its own table through the same lazy path."""
    with _models_lock:
        if kind not in _models:
            _models[kind] = factory()
    return _model(kind)


# ------------------------------------------------------------------ clients

@dataclass(frozen=True)
class Client:
    client_id: str
    client_secret: Optional[str]
    redirect_uri: Optional[str]
    source: str                    # "stored", "env" or "built_in"


CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._\-]{6,200}$")


def get_client(provider_name: str) -> Optional[Client]:
    """The install-wide client: stored by an admin, else .env, else a
    built-in public client (none for Google)."""
    p = provider(provider_name)
    Model = _model("client")
    db = _db()
    try:
        row = db.get(Model, provider_name)
        if row is not None and row.client_id:
            return Client(row.client_id, row.client_secret or None, row.redirect_uri or None, "stored")
    finally:
        db.close()
    for source in (p.env_client, p.built_in_client):
        if source is not None:
            c = source()
            if c is not None and c.client_id:
                return c
    return None


def save_client(provider_name: str, client_id: str, client_secret: Optional[str], redirect_uri: Optional[str], *, by: str) -> None:
    p = provider(provider_name)
    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip() or None
    if not CLIENT_ID_RE.match(client_id):
        raise AccountError("bad_client_id", "That Client ID does not look right. Copy it again from the console.")
    if not p.public_client and not client_secret:
        raise AccountError("no_secret", "Paste the client secret too.")
    if client_secret is not None and (len(client_secret) > 512 or any(c.isspace() for c in client_secret)):
        raise AccountError("bad_secret", "That client secret does not look right. Copy it again from the console.")
    Model = _model("client")
    db = _db()
    try:
        row = db.get(Model, provider_name)
        if row is None:
            row = Model(provider=provider_name, client_id=client_id)
            db.add(row)
        row.client_id = client_id
        if client_secret is not None or p.public_client:
            row.client_secret = client_secret
        row.redirect_uri = redirect_uri or None
        row.updated_by = by
        db.commit()
    finally:
        db.close()


def update_client_redirect(provider_name: str, redirect_uri: str, *, by: str) -> bool:
    """Change only the redirect URI of the saved client (the admin moved
    OpenWings to another address)."""
    Model = _model("client")
    db = _db()
    try:
        row = db.get(Model, provider_name)
        if row is None:
            return False
        row.redirect_uri = redirect_uri or None
        row.updated_by = by
        db.commit()
        return True
    finally:
        db.close()


def delete_client(provider_name: str) -> bool:
    Model = _model("client")
    db = _db()
    try:
        row = db.get(Model, provider_name)
        if row is None:
            return False
        db.delete(row)
        db.commit()
        return True
    finally:
        db.close()


def mask(value: str, keep: int = 6) -> str:
    value = value or ""
    if len(value) <= keep:
        return "•" * len(value)
    return "••••" + value[-keep:]


def client_status(provider_name: str) -> Dict[str, Any]:
    c = get_client(provider_name)
    if c is None:
        return {"configured": False}
    return {
        "configured": True,
        "source": c.source,
        "client_id": mask(c.client_id, 24),
        "has_secret": bool(c.client_secret),
        "redirect_uri": c.redirect_uri or "",
    }


# ------------------------------------------------------------------ PKCE and flows

def new_verifier() -> str:
    return secrets.token_urlsafe(64)


def s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


@dataclass
class Flow:
    state: str
    owner: str
    provider: str
    purpose: str
    scopes: Tuple[str, ...]
    verifier: str
    redirect_uri: str
    origin: str
    extra: Dict[str, Any] = field(default_factory=dict)
    created: float = field(default_factory=time.monotonic)


_flows: Dict[str, Flow] = {}
_flows_lock = threading.Lock()


def _prune(now: Optional[float] = None, owner: Optional[str] = None) -> None:
    """Drop expired attempts; when `owner` starts another one, drop only that
    person's oldest beyond MAX_PENDING_PER_OWNER (so nobody can push out
    someone else's sign-in); MAX_PENDING is a backstop for everyone."""
    now = time.monotonic() if now is None else now
    for k in [k for k, f in _flows.items() if now - f.created > FLOW_TTL_SECONDS]:
        _flows.pop(k, None)
    if owner is not None:
        mine = sorted((f.created, k) for k, f in _flows.items() if f.owner == owner)
        while len(mine) >= MAX_PENDING_PER_OWNER:
            _flows.pop(mine.pop(0)[1], None)
    while len(_flows) >= MAX_PENDING:
        _flows.pop(min(_flows, key=lambda k: _flows[k].created), None)


def auth_disabled() -> bool:
    """Single-user installs (AUTH_ENABLED=false) have no signed-in person;
    their flows belong to "" and finish without a session."""
    return os.environ.get("AUTH_ENABLED", "true").strip().lower() in {"0", "false", "no", "off"}


def start_flow(owner: str, provider_name: str, purpose: str, scopes: Iterable[str], *,
               redirect_uri: str, origin: str, extra: Optional[Dict[str, Any]] = None) -> Flow:
    owner = owner or ""
    if not owner and not auth_disabled():
        raise AccountError("not_signed_in", "Sign in to OpenWings first.")
    provider(provider_name)
    flow = Flow(state=secrets.token_urlsafe(32), owner=owner, provider=provider_name, purpose=purpose,
                scopes=tuple(dict.fromkeys(s for s in scopes if s)), verifier=new_verifier(),
                redirect_uri=redirect_uri, origin=origin, extra=dict(extra or {}))
    with _flows_lock:
        _prune(owner=owner)
        _flows[flow.state] = flow
    return flow


def peek_flow(state: str) -> Optional[Flow]:
    with _flows_lock:
        _prune()
        for key, flow in _flows.items():
            if secrets.compare_digest(key.encode(), (state or "").encode()):
                return flow
    return None


def take_flow(state: str, user: Optional[str]) -> Flow:
    """Consume the attempt for `state`, only for the person who started it."""
    with _flows_lock:
        _prune()
        hit = None
        for key, flow in _flows.items():
            if secrets.compare_digest(key.encode(), (state or "").encode()):
                hit = flow
        if hit is None:
            raise AccountError("expired", "That sign-in ran out of time or was already used. Start it again.")
        user = user or ""
        if not hit.owner and not auth_disabled():
            raise AccountError("other_user", "That sign-in was started by another OpenWings account. Start it again.")
        if (hit.owner or user) and not (user and secrets.compare_digest(hit.owner.encode(), user.encode())):
            raise AccountError("other_user", "That sign-in was started by another OpenWings account. Start it again.")
        _flows.pop(hit.state, None)
        return hit


def authorize_url(flow: Flow, client: Client, *, extra: Optional[Dict[str, str]] = None) -> str:
    p = provider(flow.provider)
    params: Dict[str, str] = {
        "client_id": client.client_id,
        "redirect_uri": flow.redirect_uri,
        "response_type": "code",
        "scope": " ".join(flow.scopes),
        "state": flow.state,
        "code_challenge": s256(flow.verifier),
        "code_challenge_method": "S256",
    }
    params.update(dict(p.authorize_extra))
    params.update(extra or {})
    return test_url(p.authorize_url) + "?" + urlencode(params, quote_via=quote)


def bounce(request, flow: Optional[Flow]):
    """A redirect to the same path and query on the flow's own origin, when
    the callback arrived on another origin; else None."""
    if flow is None or not flow.origin:
        return None
    from fastapi.responses import RedirectResponse
    from wings.origin import allowed_origins, request_origin
    here = request_origin(request)
    if here == flow.origin or flow.origin not in allowed_origins():
        return None
    query = request.url.query
    return RedirectResponse(flow.origin + request.url.path + ("?" + query if query else ""), status_code=303)


def bounce_unauthenticated(request):
    """For upstream's auth middleware: a provider callback without a session
    on this address, whose attempt started on another trusted address, goes
    back there (same path and query). None otherwise."""
    path = request.url.path
    if path not in {p.callback_path for p in PROVIDERS.values()}:
        return None
    state = request.query_params.get("state") or ""
    if not state:
        return None
    return bounce(request, peek_flow(state))


# ------------------------------------------------------------------ HTTP

def _http():
    import httpx
    return httpx


def post_form(url: str, data: Dict[str, str], *, timeout: float = HTTP_TIMEOUT, headers: Optional[Dict[str, str]] = None):
    return _http().post(test_url(url), data=data, timeout=timeout,
                        headers={"Accept": "application/json", **(headers or {})})


def get_json(url: str, token: str, *, params: Optional[Dict[str, Any]] = None, timeout: float = HTTP_TIMEOUT):
    return _http().get(test_url(url), params=params, timeout=timeout,
                       headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})


def oauth_error(resp) -> str:
    try:
        body = resp.json()
    except Exception:
        return ""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, str):
            return err[:64]
        if isinstance(err, dict) and isinstance(err.get("code"), str):
            return err["code"][:64]
    return ""


def _token_body(resp) -> Dict[str, Any]:
    try:
        body = resp.json()
    except Exception:
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("access_token"), str) or not body["access_token"]:
        raise AccountError("exchange_failed", "The sign-in did not hand over access. Please try again.")
    return body


def _client_auth(client: Client, data: Dict[str, str]) -> Dict[str, str]:
    data = dict(data)
    data["client_id"] = client.client_id
    if client.client_secret:
        data["client_secret"] = client.client_secret
    return data


def exchange_code(flow: Flow, code: str, client: Client, *, post: Optional[Callable] = None) -> Dict[str, Any]:
    code = (code or "").strip()
    if not code or len(code) > 2048:
        raise AccountError("no_code", "The sign-in came back without a code. Please try again.")
    p = provider(flow.provider)
    try:
        resp = (post or post_form)(p.token_url, _client_auth(client, {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": flow.redirect_uri,
            "code_verifier": flow.verifier,
        }))
    except Exception as exc:
        logger.warning("%s code exchange failed: %s", p.label, type(exc).__name__)
        raise AccountError("exchange_failed", f"{p.label} did not answer. Please try again.") from None
    status = getattr(resp, "status_code", 0)
    if status != 200:
        err = oauth_error(resp)
        logger.warning("%s code exchange refused (HTTP %s %s)", p.label, status, err or "-")
        if err == "invalid_client":
            raise AccountError("invalid_client", f"{p.label} did not accept this install's client ID or secret. Ask your admin to check them.")
        if err == "redirect_uri_mismatch":
            raise AccountError("redirect_mismatch", f"{p.label} says the redirect address does not match. Ask your admin to check it.")
        raise AccountError("exchange_failed", f"{p.label} did not hand over access. Please try again.")
    return _token_body(resp)


def refresh_tokens(provider_name: str, refresh_token: str, client: Client, *, scopes: Optional[Iterable[str]] = None,
                   post: Optional[Callable] = None) -> Dict[str, Any]:
    p = provider(provider_name)
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    if scopes:
        data["scope"] = " ".join(scopes)
    try:
        resp = (post or post_form)(p.token_url, _client_auth(client, data))
    except Exception as exc:
        logger.warning("%s token refresh failed: %s", p.label, type(exc).__name__)
        raise AccountError("temporary", f"{p.label} did not answer. Try again in a minute.") from None
    status = getattr(resp, "status_code", 0)
    if status == 200:
        return _token_body(resp)
    err = oauth_error(resp)
    logger.warning("%s token refresh refused (HTTP %s %s)", p.label, status, err or "-")
    if err in REAUTH_ERRORS or status in (400, 401):
        raise NeedsSignIn()
    raise AccountError("temporary", f"{p.label} did not answer. Try again in a minute.")


def revoke_token(provider_name: str, token: str, *, post: Optional[Callable] = None) -> bool:
    p = provider(provider_name)
    if not p.revoke_url or not token:
        return False
    try:
        resp = (post or post_form)(p.revoke_url, {"token": token})
        return getattr(resp, "status_code", 0) == 200
    except Exception as exc:
        logger.warning("%s revoke failed: %s", p.label, type(exc).__name__)
        return False


def expiry_from(tokens: Dict[str, Any], now: Optional[float] = None) -> Optional[int]:
    try:
        seconds = int(tokens.get("expires_in"))
    except (TypeError, ValueError):
        return None
    return int((now or time.time()) + max(0, seconds))


def granted_scopes(tokens: Dict[str, Any], asked: Iterable[str] = ()) -> List[str]:
    raw = tokens.get("scope")
    if isinstance(raw, str) and raw.strip():
        return sorted(set(raw.split()))
    return sorted(set(asked))


# ------------------------------------------------------------------ links (per person)

def _features(row) -> Dict[str, Any]:
    try:
        data = json.loads(row.features or "{}")
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def link_public(row) -> Dict[str, Any]:
    return {
        "id": row.id,
        "provider": row.provider,
        "account": row.account,
        "display_name": row.display_name or "",
        "scopes": (row.scopes or "").split(),
        "features": _features(row),
        "needs_signin": bool(row.needs_signin),
    }


def save_link(owner: str, provider_name: str, account: str, tokens: Dict[str, Any], *,
              scopes: Iterable[str] = (), features: Optional[Dict[str, Any]] = None,
              display_name: str = "") -> str:
    """Store (or update) this person's sign-in for `account`. Scopes add up
    (incremental sign-in); a missing refresh token keeps the old one."""
    if not owner:
        raise AccountError("not_signed_in", "Sign in to OpenWings first.")
    account = (account or "").strip().lower()
    Model = _model("link")
    db = _db()
    try:
        row = (db.query(Model).filter(Model.owner == owner, Model.provider == provider_name,
                                      Model.account == account).first())
        if row is None:
            row = Model(id=uuid.uuid4().hex[:16], owner=owner, provider=provider_name, account=account)
            db.add(row)
        old_scopes = set((row.scopes or "").split())
        row.scopes = " ".join(sorted(old_scopes | set(scopes)))
        merged = _features(row)
        merged.update(features or {})
        row.features = json.dumps(merged, sort_keys=True)
        row.access_token = tokens.get("access_token") or None
        if isinstance(tokens.get("refresh_token"), str) and tokens["refresh_token"]:
            row.refresh_token = tokens["refresh_token"]
        row.expires_at = expiry_from(tokens)
        row.needs_signin = False
        if display_name:
            row.display_name = display_name[:200]
        db.commit()
        return row.id
    finally:
        db.close()


def update_features(owner: str, link_id: str, **features) -> None:
    Model = _model("link")
    db = _db()
    try:
        row = db.query(Model).filter(Model.id == link_id, Model.owner == owner).first()
        if row is None:
            return
        merged = _features(row)
        for k, v in features.items():
            if v is None:
                merged.pop(k, None)
            else:
                merged[k] = v
        row.features = json.dumps(merged, sort_keys=True)
        db.commit()
    finally:
        db.close()


def links_for(owner: str, provider_name: Optional[str] = None) -> List[Dict[str, Any]]:
    if not owner:
        return []
    Model = _model("link")
    db = _db()
    try:
        q = db.query(Model).filter(Model.owner == owner)
        if provider_name:
            q = q.filter(Model.provider == provider_name)
        return [link_public(r) for r in q.order_by(Model.created_at).all()]
    finally:
        db.close()


def get_link(owner: str, link_id: str) -> Optional[Dict[str, Any]]:
    Model = _model("link")
    db = _db()
    try:
        row = db.query(Model).filter(Model.id == link_id, Model.owner == owner).first()
        return link_public(row) if row is not None else None
    finally:
        db.close()


def find_link(owner: str, provider_name: str, *, account: Optional[str] = None,
              scope: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """This person's first link of a provider (optionally for an address, or
    one that was granted `scope`)."""
    for link in links_for(owner, provider_name):
        if account and link["account"] != account.strip().lower():
            continue
        if scope and scope not in link["scopes"]:
            continue
        return link
    return None


_refresh_locks: Dict[str, threading.Lock] = {}
_refresh_guard = threading.Lock()


@contextlib.contextmanager
def _refresh_lock(link_id: str):
    with _refresh_guard:
        lock = _refresh_locks.setdefault(link_id, threading.Lock())
    with lock:
        yield


def _due(expires_at: Optional[int], now: Optional[float] = None) -> bool:
    if not expires_at:
        return True
    return (now or time.time()) >= expires_at - REFRESH_SKEW_SECONDS


def access_token(link_id: str, owner: str, *, post: Optional[Callable] = None,
                 now: Optional[float] = None) -> str:
    """A current access token for this person's own sign-in, refreshed when
    due. Nobody else gets it (admins included)."""
    if not owner:
        raise AccountError("not_yours", "This account belongs to another person.")
    Model = _model("link")
    db = _db()
    try:
        row = db.query(Model).filter(Model.id == link_id, Model.owner == owner).first()
        if row is None:
            raise AccountError("not_yours", "This account is not connected for you.")
        if row.needs_signin:
            raise NeedsSignIn()
        if row.access_token and not _due(row.expires_at, now):
            return row.access_token
        with _refresh_lock(link_id):
            db.refresh(row)
            if row.access_token and not _due(row.expires_at, now):
                return row.access_token
            if not row.refresh_token:
                row.needs_signin = True
                db.commit()
                raise NeedsSignIn()
            client = get_client(row.provider)
            if client is None:
                raise AccountError("no_client", "This sign-in is not set up on this OpenWings any more. Ask your admin.")
            try:
                tokens = refresh_tokens(row.provider, row.refresh_token, client, post=post)
            except NeedsSignIn:
                row.access_token = None
                row.refresh_token = None
                row.needs_signin = True
                db.commit()
                raise
            row.access_token = tokens["access_token"]
            if isinstance(tokens.get("refresh_token"), str) and tokens["refresh_token"]:
                row.refresh_token = tokens["refresh_token"]
            row.expires_at = expiry_from(tokens, now)
            db.commit()
            return row.access_token
    finally:
        db.close()


def remove_link(owner: str, link_id: str, *, post: Optional[Callable] = None) -> bool:
    """Revoke at the provider (when it can), then forget the tokens."""
    Model = _model("link")
    db = _db()
    try:
        row = db.query(Model).filter(Model.id == link_id, Model.owner == owner).first()
        if row is None:
            return False
        token = row.refresh_token or row.access_token
        provider_name = row.provider
        db.delete(row)
        db.commit()
    finally:
        db.close()
    if token:
        revoke_token(provider_name, token, post=post)
    return True


# ------------------------------------------------------------------ mail sign-in tokens

# Modules that register a provider (and its tools) when imported; patch 0026
# appends its Microsoft module here.
PROVIDER_MODULES: List[str] = ["wings.accounts_google"]
# oauth_provider value of an upstream mail account -> fn(account_id, cfg)
# returning a current access token for IMAP/SMTP XOAUTH2 (or None).
MAIL_TOKEN_PROVIDERS: Dict[str, Callable[[str, Dict[str, Any]], Optional[str]]] = {}
# oauth_provider -> the provider's own mail servers, (host, port, security).
# A token is handed out only for an account on these hosts.
MAIL_TARGETS: Dict[str, List[Tuple[str, int, str]]] = {}


def mail_hosts(provider_name: str) -> set:
    """The host names a provider's mail token may be sent to (its own mail
    servers; in test mode also the stubs they are mapped to)."""
    out = set()
    for host, port, security in MAIL_TARGETS.get(provider_name, []):
        out.add(host.lower())
        out.add(mail_target(host, port, security)[0].lower())
    return out


def mail_host_ok(provider_name: str, cfg: Dict[str, Any]) -> bool:
    allowed = mail_hosts(provider_name)
    if not allowed:
        return False
    for key in ("imap_host", "smtp_host"):
        host = str(cfg.get(key) or "").strip().lower().rstrip(".")
        if host and host not in allowed:
            return False
    return True


def load_providers() -> None:
    import importlib
    for name in PROVIDER_MODULES:
        try:
            importlib.import_module(name)
        except Exception:
            logger.exception("Could not load %s", name)


def mail_token(cfg: Dict[str, Any]) -> Optional[str]:
    """Access token for a mail account that signs in with OAuth (XOAUTH2),
    refreshed when due; None when there is none."""
    provider_name = str(cfg.get("oauth_provider") or "")
    if not provider_name:
        return None
    fn = MAIL_TOKEN_PROVIDERS.get(provider_name)
    if fn is None:
        load_providers()
        fn = MAIL_TOKEN_PROVIDERS.get(provider_name)
    if fn is None:
        return None
    if not mail_host_ok(provider_name, cfg):
        # Never send a provider's token to another server (an account whose
        # hosts were changed, or a row that was not made for this sign-in).
        logger.warning("Mail sign-in token withheld: account %s is not on %s's mail servers",
                       str(cfg.get("account_id") or "?")[:40], provider_name)
        return None
    return fn(str(cfg.get("account_id") or ""), cfg)


def xoauth2(user: str, token: str) -> str:
    """The SASL XOAUTH2 initial response (not base64; imaplib and smtplib
    encode it)."""
    return f"user={user}\x01auth=Bearer {token}\x01\x01"


# ------------------------------------------------------------------ assistant tools

@dataclass(frozen=True)
class AccountTool:
    name: str
    family: str
    family_label: str
    words: str                     # regex: a request about this family
    description: str
    parameters: Dict[str, Any]
    handler: Callable[[Dict[str, Any], str], Awaitable[Dict[str, Any]]]
    # How upstream's tool safety gate classifies the tool (src/tool_capabilities):
    # what it does, and whether its result is outside text (a file's
    # contents can carry instructions, so later risky tools need approval).
    effects: Tuple[str, ...] = ("read_private",)
    result_integrity: str = "external_untrusted"
    # Whether this person can use the tool at all (for example: connected
    # Google Drive). Tools a person cannot use are left out of their chats.
    available: Optional[Callable[[str], bool]] = None


TOOLS: Dict[str, AccountTool] = {}
_installed = {"done": False}


def register_tool(tool: AccountTool) -> None:
    TOOLS[tool.name] = tool
    if _installed["done"]:
        _install_one(tool)


def _wrap(tool: AccountTool):
    async def run(content: str, ctx: dict) -> dict:
        owner = str((ctx or {}).get("owner") or "").strip()
        if not owner:
            return {"error": f"{tool.name} works only for a signed-in person.", "exit_code": 1}
        try:
            args = json.loads(content) if (content or "").strip() else {}
        except ValueError:
            return {"error": f"{tool.name} arguments must be a JSON object.", "exit_code": 1}
        if not isinstance(args, dict):
            return {"error": f"{tool.name} arguments must be a JSON object.", "exit_code": 1}
        try:
            return await tool.handler(args, owner)
        except AccountError as exc:
            return {"error": exc.message, "exit_code": 1}
        except Exception as exc:  # never leak tokens or bodies
            logger.warning("%s failed: %s", tool.name, type(exc).__name__)
            return {"error": f"{tool.name} failed ({type(exc).__name__}).", "exit_code": 1}
    run.__name__ = f"wings_{tool.name}"
    return run


def _install_one(tool: AccountTool) -> None:
    schema = {"type": "function", "function": {
        "name": tool.name, "description": tool.description, "parameters": tool.parameters}}
    from src import tool_schemas
    if not any(s.get("function", {}).get("name") == tool.name for s in tool_schemas.FUNCTION_TOOL_SCHEMAS):
        tool_schemas.FUNCTION_TOOL_SCHEMAS.append(schema)
    from src.tool_types import TOOL_TAGS
    if isinstance(TOOL_TAGS, set):
        TOOL_TAGS.add(tool.name)          # native calls with this name convert
    from types import MappingProxyType
    from src import tool_capabilities as caps
    if tool.name not in caps.TOOL_CAPABILITIES:
        entry = caps._capabilities(*(caps.ToolEffect(e) for e in tool.effects),
                                   result_integrity=caps.ResultIntegrity(tool.result_integrity))
        caps.TOOL_CAPABILITIES = MappingProxyType({**caps.TOOL_CAPABILITIES, tool.name: entry})
    # The list of tools with an explicit capability entry follows, so code
    # (and upstream's tests) that check every tool has one see these too.
    caps.KNOWN_CAPABILITY_TOOLS = frozenset(caps.TOOL_CAPABILITIES)
    import src.agent_tools as agent_tools
    agent_tools.TOOL_HANDLERS[tool.name] = _wrap(tool)
    from src import turn_contract
    turn_contract.FAMILY_TOOLS[tool.family] = frozenset(turn_contract.FAMILY_TOOLS.get(tool.family, frozenset()) | {tool.name})
    turn_contract._FAMILY_WORDS[tool.family] = tool.words
    try:
        from src import tool_index
        tool_index.BUILTIN_TOOL_DESCRIPTIONS.setdefault(tool.name, tool.description)
    except Exception:
        pass
    from wings import agent_tools as wings_agent_tools
    spec = wings_agent_tools.FAMILIES.setdefault(tool.family, {"label": tool.family_label, "contract": [tool.family]})
    if tool.family not in spec.setdefault("contract", []):
        spec["contract"].append(tool.family)


def unavailable_tools(owner: str) -> set:
    """Names of account tools this person cannot use (nothing connected for
    them), to add to their chat's disabled tools."""
    out = set()
    for tool in TOOLS.values():
        if tool.available is None:
            continue
        try:
            ok = bool(owner) and tool.available(owner)
        except Exception:
            ok = False
        if not ok:
            out.add(tool.name)
    return out


def install_tools() -> None:
    """Add every registered tool to upstream's registries (once, at start)."""
    if _installed["done"]:
        return
    _installed["done"] = True
    for tool in TOOLS.values():
        try:
            _install_one(tool)
        except Exception:
            logger.exception("Could not add assistant tool %s", tool.name)


# ------------------------------------------------------------------ requests

def route_endpoint(request, path: str, method: str):
    """Upstream's own route function for `method path` (to reuse its
    validation and storage), or None. FastAPI 0.16x keeps included routers
    as nested entries, so look through them as FastAPI itself does."""
    routes = request.app.router.routes
    try:
        from fastapi.routing import _iter_routes_with_context
        pairs = list(_iter_routes_with_context(routes))
    except Exception:
        pairs = [(r, None) for r in routes]
    for route, ctx in pairs:
        if ctx is not None:
            p, methods = ctx.path, ctx.methods
        else:
            p, methods = getattr(route, "path", ""), getattr(route, "methods", None)
        if p == path and method in (methods or ()):
            return getattr(route, "endpoint", None) or getattr(ctx, "endpoint", None)
    return None


def person(request) -> str:
    """The signed-in person behind this browser session, or HTTP 403."""
    from fastapi import HTTPException
    from wings.email_gate import is_human_request
    if not is_human_request(request):
        raise HTTPException(403, "Only a signed-in person can connect accounts.")
    return getattr(request.state, "current_user", "") or ""


def person_is_admin(request) -> bool:
    try:
        from core.middleware import require_admin
        require_admin(request)
        return True
    except Exception:
        return False


def require_admin_person(request) -> str:
    user = person(request)
    from core.middleware import require_admin
    require_admin(request)
    return user


def origin_for(request) -> str:
    """The trusted origin this browser uses (stored in a flow for the bounce)."""
    from wings.origin import allowed_origins, configured_base, request_origin
    here = request_origin(request)
    return here if here in allowed_origins() else configured_base()


# Top-level names that are not on the public suffix list (home and company
# networks, reserved names): Google refuses redirect URIs on them.
PRIVATE_SUFFIXES = frozenset({
    "local", "lan", "home", "internal", "intranet", "corp", "private", "localdomain",
    "arpa", "test", "example", "invalid", "localhost", "home.arpa", "box", "router", "fritz.box",
})


def is_loopback_host(host: str) -> bool:
    import ipaddress
    host = (host or "").lower().strip("[]")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def safe_redirect_uri(uri: str) -> bool:
    """Redirect URI rules from Google's OAuth docs: https, or http on
    localhost / a loopback IP; no user info, fragment, wildcard or path
    traversal; no raw IP other than loopback; and a host name whose ending
    is a public one (not .local, .lan, .home and the like, not a single
    word)."""
    import ipaddress
    try:
        parts = urlsplit((uri or "").strip())
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    if parts.username or parts.password or parts.fragment or "*" in uri or "/.." in parts.path or "\\.." in parts.path:
        return False
    host = parts.hostname.lower().rstrip(".")
    if is_loopback_host(host):
        return True
    try:
        ipaddress.ip_address(host)
        return False                       # raw IPs: only loopback
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) < 2 or labels[-1] in PRIVATE_SUFFIXES or ".".join(labels[-2:]) in PRIVATE_SUFFIXES:
        return False
    return parts.scheme == "https"

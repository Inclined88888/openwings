"""Continue with ChatGPT (patch 0022): a ChatGPT Plus or Pro plan powers
OpenWings through OpenAI's official Sign in with ChatGPT for open-source apps.

Written from OpenAI's documentation, which is the source of truth:
  https://developers.openai.com/siwc/token-sharing-open-source  (overview)
  .../sign-in, .../profiles-and-sessions, .../models-and-inference,
  .../errors-and-recovery, .../token-reference, .../preview-limitations,
  .../self-hosted-vms, https://developers.openai.com/siwc/ui-ux-guidelines
No code is taken from OpenAI's devkit (it has a noncommercial licence).

What the docs ask for, and where it happens here:

* OpenWings registers itself on each install: the first sign-in uses
  client_id=dynamic_agent_client with agent_name_hint=OpenWings and this
  install's ext_agent_host_id (host_id(), urn:uuid:..., saved before the first
  sign-in). The callback returns the issued client_id (oaiapp_...), which is
  saved before the code exchange and reused for every later sign-in. No
  client secret, no partner key, nothing for the owner to register.
* Fresh state, nonce and PKCE (S256) per attempt; the pending attempt is bound
  to the signed-in OpenWings user, used once, and expires after ten minutes.
* The redirect is http://127.0.0.1:<port>/api/wings/connect/chatgpt/callback:
  loopback only, never "localhost", and only the port ever changes
  (CALLBACK_PATH must never change). The browser has to reach OpenWings over
  127.0.0.1 for that (same computer, or an SSH tunnel); a server reached any
  other way gets OpenAI's self-hosted steps instead (export / import below).
* The ID token is checked against OpenAI's JWKS (signature, issuer, audience =
  issued client_id, expiry, nonce); its sub is the account identity and must
  stay the same on later sign-ins. Plan use is on only if the granted scopes
  include chatgpt.tokens.use.direct.
* Tokens live on this server only, encrypted at rest: access and refresh token
  in upstream's ProviderAuthSession (EncryptedText), the ID token and the
  account details in this module's own table (EncryptedText as well). They
  are never sent to the browser, logged, or put in a URL.
* One person per connection: the endpoint and the tokens belong to the
  OpenWings user who signed in, and resolve_runtime_credentials() refuses
  every other user, admins included ("Using one user's subscription to fulfill
  another user's requests" is not allowed).
* Refresh near expiry, one at a time (thread lock plus a file lock, so the
  command-line export/import cannot race the app); a refresh replaces the
  access token, expiry, scopes and refresh token together; the documented
  terminal codes clear the tokens and ask for a new sign-in.
* Models: GET https://api.openai.com/v1/models -> {"models": [{slug,
  display_name, visibility}]}, keeping visibility == "list" in server order.
* Inference: POST https://api.openai.com/v1/responses with store:false,
  stream:true, the whole history in `input`, system text in `instructions`,
  and none of the unsupported fields (no temperature, no max_output_tokens,
  ...). A request counts as done only after response.completed. Plan errors
  stop the request: no silent switch to another way of paying.
* Sign out revokes the refresh token at the revocation_endpoint from OpenAI's
  discovery document, clears the tokens, and keeps the client_id link and the
  host id. If revocation is not confirmed, the person is told to disconnect
  OpenWings in ChatGPT settings.
* OpenWings uses its own name everywhere (agent_name_hint, User-Agent) and
  never ChatGPT's private backend, Codex's client id or its headers.
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
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urlencode, urlsplit

logger = logging.getLogger(__name__)

PROVIDER = "chatgpt-plan"
ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = ISSUER + "/api/accounts/authorize"
TOKEN_URL = ISSUER + "/api/accounts/oauth/token"
JWKS_URL = ISSUER + "/.well-known/jwks.json"
DISCOVERY_URL = ISSUER + "/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"
RESPONSES_URL = "https://api.openai.com/v1/responses"
MODELS_URL = "https://api.openai.com/v1/models"
USAGE_URL = "https://chatgpt.com/settings/usage"
# Fixed for good: OpenAI matches scheme, host and path exactly and only lets
# the port vary between sign-ins of one registration.
CALLBACK_PATH = "/api/wings/connect/chatgpt/callback"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
DYNAMIC_CLIENT = "dynamic_agent_client"
AGENT_NAME = "OpenWings"
USER_AGENT = "OpenWings/1.0"
ENDPOINT_NAME = "ChatGPT plan"
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
HOST_ID_RE = re.compile(
    r"^(urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|urn:ietf:params:oauth:jwk-thumbprint:[A-Za-z0-9:_-]{1,200}"
    r"|did:key:[A-Za-z0-9]{1,200})$"
)
ID_TOKEN_ALGS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512")
FLOW_TTL_SECONDS = 600
MAX_PENDING = 32
REFRESH_SKEW_SECONDS = 120
CLOCK_LEEWAY_SECONDS = 5
# Refresh errors after which the refresh token is unusable (errors-and-recovery).
REAUTH_CODES = frozenset({
    "invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
    "refresh_token_invalidated", "refresh_token_reused",
})
EXPORT_FORMAT = "openwings-chatgpt-plan/1"
_FORWARDING_HEADERS = (
    "forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
    "x-real-ip", "cf-connecting-ip", "cf-ray", "cf-visitor",
)


class PlanError(Exception):
    """A ChatGPT plan step failed; `code` is a fixed, display-safe reason."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


class PlanReauthRequired(PlanError):
    def __init__(self, message: str = "Sign in to ChatGPT again: Settings > Add Models > Continue with ChatGPT."):
        super().__init__("needs_signin", message)


class PlanAuthNotFound(PlanError):
    def __init__(self, message: str = "This ChatGPT plan connection belongs to another OpenWings user."):
        super().__init__("not_yours", message)


class PlanTemporary(PlanError):
    def __init__(self, message: str = "OpenAI's sign-in service did not answer. Try again in a minute."):
        super().__init__("temporary", message)


# ------------------------------------------------------------------ basics

def _data_dir() -> Path:
    return Path(os.environ.get("ODYSSEUS_DATA_DIR") or "/app/data") / "wings"


def host_id(data_dir: Optional[Path] = None) -> str:
    """This install's ext_agent_host_id: created once, before the first
    sign-in, and reused (docs: "Choose and persist this host's
    ext_agent_host_id before its first sign-in"). Opaque, not a secret."""
    base = Path(data_dir) if data_dir else _data_dir()
    path = base / "chatgpt-host-id"
    try:
        value = path.read_text(encoding="ascii").strip()
        if HOST_ID_RE.match(value):
            return value
        logger.warning("ChatGPT host id file was not valid; making a new one")
    except FileNotFoundError:
        pass
    except (OSError, UnicodeDecodeError):
        logger.warning("ChatGPT host id file could not be read; making a new one")
    base.mkdir(parents=True, exist_ok=True)
    value = "urn:uuid:" + str(uuid.uuid4())
    tmp = base / f".chatgpt-host-id.{secrets.token_hex(6)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, value.encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    return value


def is_plan_url(url: str) -> bool:
    """True for the one URL ChatGPT plan inference goes to."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and (parts.hostname or "").lower().rstrip(".") == "api.openai.com"
        and parts.port in (None, 443)
        and (parts.path or "").rstrip("/") == "/v1/responses"
        and not parts.query
    )


def new_verifier() -> str:
    return secrets.token_urlsafe(64)


def s256_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass
class Loopback:
    host: str   # "127.0.0.1" or "localhost"
    port: int


def loopback_of(request) -> Optional[Loopback]:
    """How this browser reaches OpenWings, when it is the loopback address.

    OpenAI's redirect must be http://127.0.0.1:<port>/..., which only works
    when the browser itself reaches OpenWings on 127.0.0.1 (the same computer,
    or an SSH tunnel). Plain http, a loopback Host header and no reverse proxy
    in between (a proxy adds forwarding headers)."""
    try:
        scheme = request.url.scheme
        host_header = (request.headers.get("host") or "").strip()
        if any(request.headers.get(h) for h in _FORWARDING_HEADERS):
            return None
    except Exception:
        return None
    if scheme != "http" or not host_header or any(c in host_header for c in "/\\@?# "):
        return None
    try:
        parts = urlsplit("http://" + host_header)
        port = parts.port or 80
        name = (parts.hostname or "").lower()
    except ValueError:
        return None
    if not 0 < port < 65536:
        return None
    if name == "127.0.0.1":
        return Loopback("127.0.0.1", port)
    if name in ("localhost", "::1"):
        return Loopback("localhost", port)
    return None


def redirect_uri_for(port: int) -> str:
    return f"http://127.0.0.1:{int(port)}{CALLBACK_PATH}"


def server_port() -> int:
    """The port OpenWings is published on, for the SSH tunnel hint."""
    raw = (os.environ.get("WINGS_APP_PORT") or "").strip()
    if raw.isdigit() and 0 < int(raw) < 65536:
        return int(raw)
    try:
        parts = urlsplit(os.environ.get("OAUTH_REDIRECT_BASE_URL", ""))
        if parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost") and parts.port:
            return parts.port
    except ValueError:
        pass
    return 7011


# ------------------------------------------------------------------ pending sign-ins

@dataclass
class PendingSignIn:
    user: str
    state: str
    nonce: str
    verifier: str
    redirect_uri: str
    client_id: Optional[str]          # None: new registration
    consent: bool = False
    created: float = field(default_factory=time.monotonic)


_pending: List[PendingSignIn] = []
_pending_lock = threading.Lock()


def _prune(now: Optional[float] = None) -> None:
    now = time.monotonic() if now is None else now
    _pending[:] = [p for p in _pending if now - p.created <= FLOW_TTL_SECONDS]
    while len(_pending) > MAX_PENDING:
        _pending.pop(0)


def start_sign_in(user: str, port: int, client_id: Optional[str], *, consent: bool = False) -> PendingSignIn:
    flow = PendingSignIn(
        user=user,
        state=secrets.token_urlsafe(32),
        nonce=secrets.token_urlsafe(32),
        verifier=new_verifier(),
        redirect_uri=redirect_uri_for(port),
        client_id=client_id,
        consent=consent,
    )
    with _pending_lock:
        _prune()
        _pending.append(flow)
    return flow


def take_sign_in(state: str, user: Optional[str]) -> PendingSignIn:
    """Consume the pending attempt for `state` (exact, timing-safe match),
    only for the OpenWings user who started it."""
    state = state or ""
    with _pending_lock:
        _prune()
        hit = None
        for p in _pending:
            if secrets.compare_digest(p.state.encode(), state.encode()):
                hit = p
        if hit is None:
            raise PlanError("expired")
        if not user or not secrets.compare_digest(hit.user.encode(), user.encode()):
            raise PlanError("other_user")
        _pending.remove(hit)
        return hit


def authorize_url(flow: PendingSignIn, host: str, *, login_hint: Optional[str] = None) -> str:
    """The documented authorization request (sign-in, step 2)."""
    params: Dict[str, str] = {"client_id": flow.client_id or DYNAMIC_CLIENT}
    if not flow.client_id:
        params["agent_name_hint"] = AGENT_NAME
    params["ext_agent_host_id"] = host
    if flow.client_id and login_hint:
        params["login_hint"] = login_hint
    params.update({
        "response_type": "code",
        "redirect_uri": flow.redirect_uri,
        "scope": SCOPES,
        "resource": RESOURCE,
        "state": flow.state,
        "nonce": flow.nonce,
        "code_challenge_method": "S256",
        "code_challenge": s256_challenge(flow.verifier),
    })
    if flow.consent:
        # Turning plan use on after an earlier decline (errors-and-recovery).
        params["prompt"] = "consent"
    return AUTHORIZE_URL + "?" + urlencode(params, quote_via=quote)


def callback_client_id(flow: PendingSignIn, returned: Optional[str]) -> str:
    """The client id this attempt exchanges its code with."""
    returned = (returned or "").strip() or None
    if flow.client_id is None:
        if not returned or returned == DYNAMIC_CLIENT or not CLIENT_ID_RE.match(returned):
            raise PlanError("registration_incomplete")
        return returned
    if returned is not None and not secrets.compare_digest(returned.encode(), flow.client_id.encode()):
        raise PlanError("client_mismatch")
    return flow.client_id


# ------------------------------------------------------------------ HTTP

def _http():
    import httpx
    return httpx


def _post_form(url: str, data: Dict[str, str], timeout: float = 20.0):
    return _http().post(url, data=data, timeout=timeout,
                        headers={"Accept": "application/json", "User-Agent": USER_AGENT})


def _get(url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 15.0):
    h = {"Accept": "application/json", "User-Agent": USER_AGENT}
    h.update(headers or {})
    return _http().get(url, headers=h, timeout=timeout)


def oauth_error_code(resp) -> str:
    try:
        body = resp.json()
    except Exception:
        return ""
    if not isinstance(body, dict):
        return ""
    err = body.get("error")
    if isinstance(err, dict):
        err = err.get("code") or err.get("type") or ""
    if not err:
        err = body.get("error_code") or body.get("code") or ""
    return str(err).strip().lower()[:80]


def _token_payload(resp) -> Dict[str, Any]:
    try:
        data = resp.json()
    except Exception:
        raise PlanError("exchange_failed") from None
    if not isinstance(data, dict):
        raise PlanError("exchange_failed")
    access = data.get("access_token")
    if not isinstance(access, str) or not access.strip():
        raise PlanError("exchange_failed")
    token_type = str(data.get("token_type") or "Bearer")
    if token_type.lower() != "bearer":
        raise PlanError("exchange_failed")
    return data


def exchange_code(code: str, verifier: str, client_id: str, redirect_uri: str, *,
                  post: Optional[Callable] = None) -> Dict[str, Any]:
    """Sign-in step 3: the form-encoded authorization_code grant."""
    code = (code or "").strip()
    if not code or len(code) > 2048:
        raise PlanError("no_code")
    try:
        resp = (post or _post_form)(TOKEN_URL, {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "resource": RESOURCE,
        })
    except Exception as exc:
        logger.warning("ChatGPT code exchange failed: %s", type(exc).__name__)
        raise PlanTemporary() from None
    status = getattr(resp, "status_code", 0)
    if status != 200:
        code_name = oauth_error_code(resp)
        logger.warning("ChatGPT code exchange refused (HTTP %s %s)", status, code_name or "-")
        if code_name == "invalid_grant":
            raise PlanError("expired")
        if code_name == "invalid_client":
            raise PlanError("invalid_client")
        raise PlanError("exchange_failed")
    data = _token_payload(resp)
    if not isinstance(data.get("id_token"), str) or not data["id_token"]:
        raise PlanError("exchange_failed")
    return data


def granted_scopes(token_response: Dict[str, Any], fallback: Iterable[str] = ()) -> List[str]:
    raw = token_response.get("scope")
    if isinstance(raw, list):
        items = [str(s) for s in raw]
    elif isinstance(raw, str):
        items = raw.split()
    else:
        items = list(fallback)
    return sorted({s.strip() for s in items if s and s.strip()})


# ------------------------------------------------------------------ ID token

_jwks_cache: Dict[str, Any] = {"set": None, "at": 0.0, "tried": 0.0}
_discovery_cache: Dict[str, Any] = {"doc": None, "at": 0.0}


def _fetch_jwks(get: Optional[Callable] = None):
    import jwt
    resp = (get or _get)(JWKS_URL)
    if getattr(resp, "status_code", 0) != 200:
        raise PlanTemporary("OpenAI's sign-in keys could not be read. Try again in a minute.")
    return jwt.PyJWKSet.from_dict(resp.json())


def _signing_key(kid: Optional[str], alg: str, jwks=None, get: Optional[Callable] = None):
    """Key for `kid`, from a cached JWKS refreshed when an unknown kid shows up."""
    def find(keyset):
        for k in getattr(keyset, "keys", []) or []:
            if (kid is None or k.key_id == kid) and getattr(k, "algorithm_name", alg) in (alg, None):
                return k
        return None

    if jwks is not None:
        key = find(jwks)
        if key is None:
            raise PlanError("bad_id_token")
        return key
    now = time.monotonic()
    keyset = _jwks_cache["set"]
    if keyset is None or now - _jwks_cache["at"] > 3600:
        keyset = _fetch_jwks(get)
        _jwks_cache.update({"set": keyset, "at": now})
    key = find(keyset)
    if key is None and now - _jwks_cache["tried"] > 60:
        _jwks_cache["tried"] = now
        keyset = _fetch_jwks(get)
        _jwks_cache.update({"set": keyset, "at": now})
        key = find(keyset)
    if key is None:
        raise PlanError("bad_id_token")
    return key


def validate_id_token(id_token: str, *, client_id: str, nonce: Optional[str], jwks=None,
                      verify_exp: bool = True, get: Optional[Callable] = None) -> Dict[str, Any]:
    """Sign-in step 4: signature against OpenAI's JWKS, issuer, audience (the
    issued client id), expiry and the nonce of this attempt."""
    import jwt
    try:
        header = jwt.get_unverified_header(id_token)
    except Exception:
        raise PlanError("bad_id_token") from None
    alg = header.get("alg")
    if alg not in ID_TOKEN_ALGS:
        raise PlanError("bad_id_token")
    key = _signing_key(header.get("kid"), alg, jwks=jwks, get=get)
    try:
        claims = jwt.decode(
            id_token, key.key, algorithms=[alg], audience=client_id, issuer=ISSUER,
            leeway=CLOCK_LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "sub", "iss", "aud"], "verify_exp": verify_exp},
        )
    except Exception as exc:
        logger.warning("ChatGPT ID token rejected: %s", type(exc).__name__)
        raise PlanError("bad_id_token") from None
    if nonce is not None and not secrets.compare_digest(str(claims.get("nonce") or "").encode(), nonce.encode()):
        raise PlanError("bad_id_token")
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub.strip():
        raise PlanError("bad_id_token")
    return claims


def discovery(get: Optional[Callable] = None) -> Dict[str, Any]:
    """OpenAI's OpenID configuration, checked to belong to OpenAI's issuer."""
    now = time.monotonic()
    doc = _discovery_cache["doc"]
    if doc is not None and now - _discovery_cache["at"] < 3600:
        return doc
    resp = (get or _get)(DISCOVERY_URL)
    if getattr(resp, "status_code", 0) != 200:
        raise PlanTemporary()
    doc = resp.json()
    if not isinstance(doc, dict) or doc.get("issuer") != ISSUER:
        raise PlanError("bad_discovery")
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri", "revocation_endpoint"):
        val = doc.get(key)
        if val is None:
            continue
        parts = urlsplit(str(val))
        if parts.scheme != "https" or parts.netloc != "auth.openai.com":
            raise PlanError("bad_discovery")
    _discovery_cache.update({"doc": doc, "at": now})
    return doc


def revoke(refresh_token: str, client_id: str, *, post: Optional[Callable] = None,
           discover: Optional[Callable] = None, attempts: int = 3,
           sleep: Callable[[float], None] = time.sleep) -> bool:
    """End the renewable session (profiles-and-sessions). True only when OpenAI
    answered 200; a network failure or 5xx is retried with backoff."""
    if not refresh_token or not client_id:
        return False
    try:
        endpoint = (discover or discovery)().get("revocation_endpoint")
    except Exception as exc:
        logger.warning("ChatGPT revocation endpoint unavailable: %s", type(exc).__name__)
        return False
    if not endpoint:
        return False
    for i in range(max(1, attempts)):
        try:
            resp = (post or _post_form)(endpoint, {
                "token": refresh_token,
                "token_type_hint": "refresh_token",
                "client_id": client_id,
            })
        except Exception as exc:
            logger.warning("ChatGPT revocation attempt failed: %s", type(exc).__name__)
            resp = None
        status = getattr(resp, "status_code", 0)
        if status == 200:
            return True
        if resp is not None and status < 500:
            logger.warning("ChatGPT revocation refused (HTTP %s)", status)
            return False
        if i + 1 < attempts:
            sleep(0.5 * (3 ** i))
    return False


# ------------------------------------------------------------------ models

def fetch_models(access_token: str, *, get: Optional[Callable] = None) -> List[Tuple[str, str]]:
    """[(slug, display_name)] for this account, in OpenAI's order."""
    if not access_token:
        return []
    try:
        resp = (get or _get)(MODELS_URL, {"Authorization": f"Bearer {access_token}"})
    except Exception as exc:
        logger.warning("ChatGPT model list failed: %s", type(exc).__name__)
        raise PlanTemporary("OpenAI's model list did not answer. Try again in a minute.") from None
    status = getattr(resp, "status_code", 0)
    if status != 200:
        logger.warning("ChatGPT model list refused (HTTP %s)", status)
        raise PlanError("models_failed", f"OpenAI did not list the models (HTTP {status}).")
    try:
        data = resp.json()
    except Exception:
        raise PlanError("models_failed") from None
    out: List[Tuple[str, str]] = []
    seen = set()
    for item in (data.get("models") if isinstance(data, dict) else None) or []:
        if not isinstance(item, dict) or item.get("visibility") != "list":
            continue
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug.strip() or slug in seen:
            continue
        name = item.get("display_name")
        name = name.strip() if isinstance(name, str) and name.strip() else slug
        seen.add(slug)
        out.append((slug.strip(), name[:80]))
    return out


# ------------------------------------------------------------------ storage

_Link = None
_link_lock = threading.Lock()


def link_model():
    """OpenWings' own table for the ChatGPT account link (created on first
    use; upstream's tables are not changed)."""
    global _Link
    if _Link is not None:
        return _Link
    with _link_lock:
        if _Link is not None:
            return _Link
        from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text
        from core.database import Base, EncryptedText, engine, utcnow_naive

        class ChatGPTPlanLink(Base):
            __tablename__ = "wings_chatgpt_plan"
            __table_args__ = {"extend_existing": True}
            id = Column(String, primary_key=True)
            owner = Column(String, nullable=False, index=True)
            client_id = Column(String, nullable=False)
            auth_id = Column(String, nullable=True, index=True)
            subject = Column(EncryptedText, nullable=True)
            email = Column(EncryptedText, nullable=True)
            id_token = Column(EncryptedText, nullable=True)
            scopes = Column(String, nullable=True)
            plan_enabled = Column(Boolean, default=False)
            expires_at = Column(Integer, nullable=True)
            earliest_refresh_at = Column(Integer, nullable=True)
            needs_signin = Column(Boolean, default=False)
            transferred = Column(Boolean, default=False)
            welcomed = Column(Boolean, default=False)
            revoke_unconfirmed = Column(Boolean, default=False)
            model_names = Column(Text, nullable=True)
            saved_at = Column(DateTime, nullable=True)
            created_at = Column(DateTime, default=utcnow_naive)

        ChatGPTPlanLink.__table__.create(bind=engine, checkfirst=True)
        _Link = ChatGPTPlanLink
        return _Link


def _db():
    from core.database import SessionLocal
    return SessionLocal()


# Set by routes/model_routes.py (the picker's per-user cache lives there).
invalidate_models_cache: Optional[Callable[[], None]] = None


def _picker_changed() -> None:
    fn = invalidate_models_cache
    if fn is not None:
        with contextlib.suppress(Exception):
            fn()


def get_link(db, owner: str):
    Link = link_model()
    return (
        db.query(Link).filter(Link.owner == owner)
        .order_by(Link.created_at.desc()).first()
    )


def _auth_row(db, link):
    if link is None or not link.auth_id:
        return None
    from core.database import ProviderAuthSession
    return (
        db.query(ProviderAuthSession)
        .filter(ProviderAuthSession.id == link.auth_id,
                ProviderAuthSession.provider == PROVIDER,
                ProviderAuthSession.owner == link.owner)
        .first()
    )


def _endpoint(db, owner: str, auth_id: Optional[str]):
    if not auth_id:
        return None
    from core.database import ModelEndpoint
    return (
        db.query(ModelEndpoint)
        .filter(ModelEndpoint.provider_auth_id == auth_id, ModelEndpoint.owner == owner)
        .order_by(ModelEndpoint.created_at).first()
    )


def _new_id(db, cls) -> str:
    for _ in range(8):
        cand = str(uuid.uuid4())[:8]
        if db.query(cls).filter(cls.id == cand).first() is None:
            return cand
    return uuid.uuid4().hex[:12]


def _jwt_exp(token: str) -> Optional[int]:
    try:
        seg = token.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        exp = json.loads(base64.urlsafe_b64decode(seg.encode("ascii"))).get("exp")
        return int(exp) if exp else None
    except Exception:
        return None


def _expiry(tokens: Dict[str, Any], now: Optional[float] = None) -> Optional[int]:
    now = time.time() if now is None else now
    try:
        expires_in = int(tokens.get("expires_in"))
        if expires_in > 0:
            return int(now) + expires_in
    except (TypeError, ValueError):
        pass
    return _jwt_exp(str(tokens.get("access_token") or ""))


def _int_or_none(v) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def remember_client_id(owner: str, client_id: str) -> None:
    """Save the issued client id before the code exchange (sign-in step 3),
    so a failed exchange does not make the next try register a new app."""
    Link = link_model()
    db = _db()
    try:
        link = get_link(db, owner)
        if link is None:
            from core.database import utcnow_naive
            db.add(Link(id=_new_id(db, Link), owner=owner, client_id=client_id, created_at=utcnow_naive()))
        elif link.client_id != client_id:
            auth = _auth_row(db, link)
            if auth is not None and (auth.refresh_token or "").strip():
                raise PlanError("already_connected")
            link.client_id = client_id
            link.subject = None
            link.email = None
            link.id_token = None
        db.commit()
    finally:
        db.close()


def store_sign_in(owner: str, client_id: str, tokens: Dict[str, Any], claims: Dict[str, Any], *,
                  transferred: bool = False, models: Optional[List[Tuple[str, str]]] = None,
                  now: Optional[float] = None) -> Dict[str, Any]:
    """Save a validated sign-in: tokens in ProviderAuthSession, account details
    in the link, and (plan use on) the owner's "ChatGPT plan" endpoint."""
    from core.database import ModelEndpoint, ProviderAuthSession, utcnow_naive
    Link = link_model()
    scopes = granted_scopes(tokens)
    plan = PLAN_SCOPE in scopes
    refresh_token = tokens.get("refresh_token")
    if plan and not (isinstance(refresh_token, str) and refresh_token.strip()):
        raise PlanError("no_refresh_token")
    db = _db()
    try:
        link = get_link(db, owner)
        if link is None:
            link = Link(id=_new_id(db, Link), owner=owner, client_id=client_id, created_at=utcnow_naive())
            db.add(link)
        elif link.client_id != client_id:
            raise PlanError("client_mismatch")
        sub = str(claims.get("sub") or "")
        if link.subject and not secrets.compare_digest(link.subject.encode(), sub.encode()):
            # A returning sign-in must be the same ChatGPT account.
            raise PlanError("other_account")
        auth = _auth_row(db, link)
        if auth is None:
            auth = ProviderAuthSession(
                id=_new_id(db, ProviderAuthSession), provider=PROVIDER, owner=owner,
                label=ENDPOINT_NAME, base_url=RESPONSES_URL, auth_mode=PROVIDER,
            )
            db.add(auth)
            link.auth_id = auth.id
        auth.base_url = RESPONSES_URL
        auth.access_token = tokens["access_token"]
        auth.refresh_token = refresh_token if isinstance(refresh_token, str) and refresh_token else None
        auth.last_refresh = utcnow_naive()
        link.subject = sub
        email = claims.get("email")
        link.email = email.strip()[:320] if isinstance(email, str) and email.strip() else link.email
        link.id_token = tokens.get("id_token") or link.id_token
        link.scopes = " ".join(scopes)
        link.plan_enabled = plan
        link.expires_at = _expiry(tokens, now)
        link.earliest_refresh_at = _int_or_none(tokens.get("earliest_refresh_at"))
        link.needs_signin = False
        link.transferred = bool(transferred)
        link.revoke_unconfirmed = False
        link.saved_at = utcnow_naive()
        ep = _endpoint(db, owner, auth.id)
        if plan:
            if ep is None:
                ep = ModelEndpoint(
                    id=_new_id(db, ModelEndpoint), name=ENDPOINT_NAME, base_url=RESPONSES_URL,
                    model_type="llm", endpoint_kind="api", owner=owner,
                )
                db.add(ep)
            _configure_endpoint(ep, auth.id)
            if models is not None:
                ep.cached_models = json.dumps([m[0] for m in models])
                link.model_names = json.dumps({m[0]: m[1] for m in models})
        elif ep is not None:
            ep.is_enabled = False
        db.commit()
        _picker_changed()
        return {"plan_enabled": plan, "auth_id": auth.id, "endpoint_id": ep.id if ep is not None else None,
                "models": len(models or [])}
    finally:
        db.close()


def _configure_endpoint(ep, auth_id: str) -> None:
    ep.base_url = RESPONSES_URL
    ep.api_key = None
    ep.provider_auth_id = auth_id
    ep.is_enabled = True
    # Odysseus' own text tool protocol travels in the prompt; no tool
    # schemas are sent on this route (namespaced tools are not wired yet).
    ep.supports_tools = False
    ep.model_type = "llm"
    ep.endpoint_kind = "api"
    ep.model_refresh_mode = "manual"


def update_models(owner: str, models: List[Tuple[str, str]]) -> int:
    db = _db()
    try:
        link = get_link(db, owner)
        ep = _endpoint(db, owner, link.auth_id if link else None)
        if link is None or ep is None:
            return 0
        ep.cached_models = json.dumps([m[0] for m in models])
        link.model_names = json.dumps({m[0]: m[1] for m in models})
        db.commit()
        _picker_changed()
        return len(models)
    finally:
        db.close()


def display_names(auth_id: Optional[str]) -> Dict[str, str]:
    """{slug: display_name} for the picker (models-and-inference: show
    display_name, send slug)."""
    if not auth_id:
        return {}
    Link = link_model()
    db = _db()
    try:
        link = db.query(Link).filter(Link.auth_id == auth_id).first()
        raw = link.model_names if link is not None else None
    finally:
        db.close()
    try:
        names = json.loads(raw) if raw else {}
        return {str(k): str(v) for k, v in names.items()} if isinstance(names, dict) else {}
    except Exception:
        return {}


def clear_tokens(db, link, *, needs_signin: bool = False, keep_id_token: bool = False) -> None:
    auth = _auth_row(db, link)
    if auth is not None:
        auth.access_token = None
        auth.refresh_token = None
    if not keep_id_token:
        link.id_token = None
    link.expires_at = None
    link.earliest_refresh_at = None
    link.needs_signin = needs_signin
    ep = _endpoint(db, link.owner, link.auth_id)
    if ep is not None:
        ep.is_enabled = False


# ------------------------------------------------------------------ refresh and runtime

_thread_locks: Dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


@contextlib.contextmanager
def refresh_lock(auth_id: str):
    """One refresh at a time per session, across threads and processes."""
    with _thread_locks_guard:
        tl = _thread_locks.setdefault(auth_id, threading.Lock())
    with tl:
        fd = None
        try:
            import fcntl
            d = _data_dir()
            d.mkdir(parents=True, exist_ok=True)
            fd = os.open(d / "chatgpt-refresh.lock", os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
        except Exception:
            if fd is not None:
                os.close(fd)
            fd = None
        try:
            yield
        finally:
            if fd is not None:
                with contextlib.suppress(Exception):
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)


def needs_refresh(link, access_token: str, now: Optional[float] = None) -> bool:
    now = time.time() if now is None else now
    exp = link.expires_at or _jwt_exp(access_token or "")
    if not exp:
        return True
    if now < exp - REFRESH_SKEW_SECONDS:
        return False
    earliest = link.earliest_refresh_at
    if earliest and now < earliest and now < exp:
        return False
    return True


def refresh_tokens(refresh_token: str, client_id: str, *, post: Optional[Callable] = None) -> Dict[str, Any]:
    """POST the refresh_token grant (profiles-and-sessions); scope is left out
    to keep the grant."""
    try:
        resp = (post or _post_form)(TOKEN_URL, {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
            "resource": RESOURCE,
        })
    except Exception as exc:
        logger.warning("ChatGPT token refresh failed: %s", type(exc).__name__)
        raise PlanTemporary() from None
    status = getattr(resp, "status_code", 0)
    if status == 200:
        return _token_payload(resp)
    code = oauth_error_code(resp)
    logger.warning("ChatGPT token refresh refused (HTTP %s %s)", status, code or "-")
    if code in REAUTH_CODES:
        raise PlanReauthRequired()
    if code == "invalid_client":
        raise PlanError("invalid_client", "OpenAI did not accept OpenWings' registration (invalid_client).")
    if status >= 500 or status == 429 or status == 0:
        raise PlanTemporary()
    raise PlanError("refresh_failed", f"ChatGPT sign-in could not be renewed (HTTP {status}).")


def resolve_runtime_credentials(auth_id: str, owner: Optional[str] = None, *,
                                post: Optional[Callable] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Current access token for one user's own ChatGPT plan connection.

    Only the OpenWings user who signed in gets it: any other user (admins
    included) and calls without a user are refused."""
    if not owner:
        raise PlanAuthNotFound("A ChatGPT plan connection can only answer the person who connected it.")
    from core.database import utcnow_naive
    db = _db()
    try:
        Link = link_model()
        link = db.query(Link).filter(Link.auth_id == auth_id, Link.owner == owner).first()
        auth = _auth_row(db, link)
        if link is None or auth is None:
            raise PlanAuthNotFound()
        if not link.plan_enabled:
            raise PlanError("plan_off", "ChatGPT plan use is turned off for this sign-in. Turn it on in Settings > Add Models.")
        access = auth.access_token or ""
        if not access and not auth.refresh_token:
            raise PlanReauthRequired()
        if needs_refresh(link, access, now):
            with refresh_lock(auth_id):
                db.refresh(auth)
                db.refresh(link)
                access = auth.access_token or ""
                if not auth.refresh_token:
                    raise PlanReauthRequired()
                if needs_refresh(link, access, now):
                    try:
                        tokens = refresh_tokens(auth.refresh_token, link.client_id, post=post)
                    except PlanReauthRequired:
                        clear_tokens(db, link, needs_signin=True, keep_id_token=True)
                        db.commit()
                        _picker_changed()
                        raise
                    except PlanTemporary:
                        exp = link.expires_at or _jwt_exp(access) or 0
                        if access and (now or time.time()) < exp:
                            return {"provider": PROVIDER, "base_url": RESPONSES_URL, "api_key": access}
                        raise
                    scopes = granted_scopes(tokens, fallback=(link.scopes or "").split())
                    # Access token, expiry, scopes and refresh token together.
                    auth.access_token = tokens["access_token"]
                    if isinstance(tokens.get("refresh_token"), str) and tokens["refresh_token"]:
                        auth.refresh_token = tokens["refresh_token"]
                    auth.last_refresh = utcnow_naive()
                    link.expires_at = _expiry(tokens, now)
                    link.earliest_refresh_at = _int_or_none(tokens.get("earliest_refresh_at"))
                    link.scopes = " ".join(scopes)
                    link.plan_enabled = PLAN_SCOPE in scopes
                    if isinstance(tokens.get("id_token"), str) and tokens["id_token"]:
                        link.id_token = tokens["id_token"]
                    db.commit()
                    if not link.plan_enabled:
                        raise PlanError("plan_off", "ChatGPT plan use was turned off for OpenWings in ChatGPT.")
                    access = auth.access_token
        return {"provider": PROVIDER, "base_url": RESPONSES_URL, "api_key": access}
    finally:
        db.close()


def access_token_for(owner: str) -> Tuple[Optional[str], Optional[str]]:
    """(auth_id, access token) for the owner's connection, refreshed if due."""
    db = _db()
    try:
        link = get_link(db, owner)
        auth_id = link.auth_id if link is not None else None
    finally:
        db.close()
    if not auth_id:
        return None, None
    creds = resolve_runtime_credentials(auth_id, owner)
    return auth_id, creds.get("api_key")


def sign_out(owner: str, *, revoke_fn: Optional[Callable] = None) -> Dict[str, Any]:
    """Revoke, then clear the tokens; the client id link and host id stay."""
    db = _db()
    try:
        link = get_link(db, owner)
        if link is None:
            return {"ok": True, "revoked": True, "had_session": False}
        auth = _auth_row(db, link)
        refresh_token = (auth.refresh_token if auth is not None else None) or ""
        revoked = True
        if refresh_token:
            revoked = (revoke_fn or revoke)(refresh_token, link.client_id)
        clear_tokens(db, link)
        link.revoke_unconfirmed = bool(refresh_token) and not revoked
        db.commit()
        _picker_changed()
        return {"ok": True, "revoked": revoked, "had_session": bool(refresh_token),
                "transferred": bool(link.transferred)}
    finally:
        db.close()


def forget(owner: str) -> None:
    """"Use a different ChatGPT account": drop the signed-out link, so the next
    sign-in registers anew. Tokens are revoked first if any are left."""
    sign_out(owner)
    Link = link_model()
    from core.database import ModelEndpoint, ProviderAuthSession
    db = _db()
    try:
        for link in db.query(Link).filter(Link.owner == owner).all():
            if link.auth_id:
                for ep in db.query(ModelEndpoint).filter(ModelEndpoint.provider_auth_id == link.auth_id,
                                                         ModelEndpoint.owner == owner).all():
                    db.delete(ep)
                auth = _auth_row(db, link)
                if auth is not None:
                    db.delete(auth)
            db.delete(link)
        db.commit()
        _picker_changed()
    finally:
        db.close()


def on_auth_deleted(db, auth_row) -> None:
    """Upstream deletes a ProviderAuthSession once its endpoint is deleted
    (Settings > model endpoints). For a ChatGPT plan session that is a sign
    out: revoke, and keep only the client id link."""
    if getattr(auth_row, "provider", None) != PROVIDER:
        return
    Link = link_model()
    link = db.query(Link).filter(Link.auth_id == auth_row.id).first()
    refresh_token = auth_row.refresh_token or ""
    revoked = True
    if refresh_token and link is not None:
        revoked = revoke(refresh_token, link.client_id, attempts=2)
    if link is not None:
        link.auth_id = None
        link.id_token = None
        link.expires_at = None
        link.earliest_refresh_at = None
        link.model_names = None
        link.revoke_unconfirmed = bool(refresh_token) and not revoked


def status_for(owner: str) -> Dict[str, Any]:
    db = _db()
    try:
        link = get_link(db, owner)
        if link is None:
            return {"state": "none"}
        auth = _auth_row(db, link)
        has_tokens = bool(auth is not None and (auth.refresh_token or auth.access_token))
        ep = _endpoint(db, owner, link.auth_id)
        try:
            models = len(json.loads(ep.cached_models or "[]")) if ep is not None else 0
        except Exception:
            models = 0
        if has_tokens and link.plan_enabled:
            state = "connected"
        elif has_tokens:
            state = "plan_off"
        elif link.needs_signin:
            state = "needs_signin"
        else:
            state = "signed_out"
        return {
            "state": state,
            "email": link.email or "",
            "models": models,
            "endpoint_id": ep.id if ep is not None else None,
            "welcomed": bool(link.welcomed),
            "transferred": bool(link.transferred),
            "revoke_unconfirmed": bool(link.revoke_unconfirmed),
            "registered": True,
        }
    finally:
        db.close()


def mark_welcomed(owner: str) -> None:
    db = _db()
    try:
        link = get_link(db, owner)
        if link is not None:
            link.welcomed = True
            db.commit()
    finally:
        db.close()


# ------------------------------------------------------------------ inference

def headers(access_token: Optional[str]) -> Dict[str, str]:
    h = {"Accept": "text/event-stream", "User-Agent": USER_AGENT}
    if access_token:
        h["Authorization"] = f"Bearer {access_token}"
    return h


# Fields this route accepts from OpenWings (preview-limitations). Anything
# else, temperature and max_output_tokens included, never crosses.
ALLOWED_PAYLOAD_KEYS = frozenset({"model", "instructions", "input", "store", "stream", "reasoning"})


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and isinstance(p.get("text"), str):
                parts.append(p["text"])
            elif isinstance(p, dict) and isinstance(p.get("content"), str):
                parts.append(p["content"])
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(parts)
    return "" if content is None else str(content)


def build_payload(model: str, messages: List[Dict], reasoning_effort: Optional[str] = None) -> Dict[str, Any]:
    """Responses request for ChatGPT plan use: store:false, stream:true, the
    full history in `input`, system text as `instructions` (system-role items
    are rejected on this route)."""
    instructions = "\n\n".join(
        t for t in (_text_of(m.get("content")).strip() for m in messages or [] if m.get("role") == "system") if t
    )
    items = []
    for m in messages or []:
        role = m.get("role") or "user"
        if role == "system":
            continue
        if role not in ("user", "assistant", "developer"):
            role = "user"   # tool results travel as text, like upstream's Responses route
        text = _text_of(m.get("content"))
        kind = "output_text" if role == "assistant" else "input_text"
        items.append({"role": role, "content": [{"type": kind, "text": text}]})
    payload: Dict[str, Any] = {"model": model, "input": items, "store": False, "stream": True}
    if instructions:
        payload["instructions"] = instructions
    effort = str(reasoning_effort or "").strip().lower()
    if effort and effort != "default":
        payload["reasoning"] = {"effort": effort}
    return {k: v for k, v in payload.items() if k in ALLOWED_PAYLOAD_KEYS}


_CODE_MESSAGES = {
    "subscription_sharing_user_not_eligible":
        "This ChatGPT account cannot use its plan in other apps (plan, workspace or policy). Use an API key or OpenRouter instead.",
    "subscription_sharing_usage_limit_exceeded":
        "ChatGPT usage limit reached for OpenWings. Manage usage: " + USAGE_URL,
    "subscription_sharing_usage_unavailable":
        "ChatGPT could not check your plan's usage just now. Try again in a minute.",
    "subscription_sharing_user_unavailable":
        "ChatGPT account details are not available just now. Try again in a minute.",
    "subscription_sharing_unsupported_capability":
        "ChatGPT plan use does not support part of this request",
    "subscription_sharing_route_not_supported":
        "ChatGPT plan use does not support this kind of request.",
    "subscription_sharing_invalid_user":
        "ChatGPT did not accept this sign-in. If it keeps happening, sign in again: Settings > Add Models > Continue with ChatGPT.",
    "chatpass_v2_scope_not_authorized":
        "ChatGPT did not allow this request for OpenWings' sign-in. Sign in again with plan use turned on.",
    "chatpass_v2_invalid_authorization_context":
        "ChatGPT did not allow this request for OpenWings' sign-in. Sign in again with plan use turned on.",
}


def _redact(text: str, hdrs: Optional[Dict[str, str]]) -> str:
    out = str(text or "")
    for k, v in (hdrs or {}).items():
        if str(k).lower() == "authorization" and v:
            secret = str(v).removeprefix("Bearer ")
            out = out.replace(str(v), "[redacted]").replace(secret, "[redacted]")
    return out


def error_text(status: int, code: str = "", message: str = "", param: str = "",
               request_id: str = "") -> str:
    """A plain message for a plan error, keeping status, code and request id."""
    code = (code or "").strip()
    base = _CODE_MESSAGES.get(code)
    if base and code == "subscription_sharing_unsupported_capability" and param:
        base = f"{base} ({param})."
    elif base and code == "subscription_sharing_unsupported_capability":
        base += "."
    if not base:
        if status == 401:
            base = "ChatGPT did not accept the sign-in or its permissions. Sign in again: Settings > Add Models > Continue with ChatGPT."
        elif status == 403:
            base = "ChatGPT refused this request (a policy or region limit). " + (message[:200] if message else "")
        elif status == 429:
            base = "ChatGPT usage limit reached for OpenWings. Manage usage: " + USAGE_URL
        elif status == 503:
            base = "ChatGPT plan use is not available right now. Try again in a minute."
        else:
            base = f"ChatGPT plan request failed (HTTP {status})." + (f" {message[:200]}" if message else "")
    tail = [f"HTTP {status}"] if status else []
    if code:
        tail.append(code)
    if request_id:
        tail.append(f"request {request_id[:80]}")
    return base.strip() + (f" [{', '.join(tail)}]" if tail else "")


def http_error(status: int, body: str, hdrs: Optional[Dict[str, str]] = None,
               request_hdrs: Optional[Dict[str, str]] = None) -> str:
    """Error before the stream opens: a Responses error object or {"detail": ...}."""
    code = message = param = ""
    try:
        data = json.loads(body or "")
    except Exception:
        data = None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            code = str(err.get("code") or "")
            message = str(err.get("message") or "")
            param = str(err.get("param") or "")
        elif isinstance(data.get("detail"), str):
            message = data["detail"]
    rid = ""
    for k, v in (hdrs or {}).items():
        if str(k).lower() == "x-request-id":
            rid = str(v)
    logger.warning("ChatGPT plan request failed: HTTP %s code=%s request_id=%s", status, code or "-", rid or "-")
    return _redact(error_text(status, code, message, param, rid), request_hdrs)


def stream_error(err: Any, request_hdrs: Optional[Dict[str, str]] = None, status_default: int = 400) -> Tuple[int, str]:
    """A response.failed / error event after the stream opened."""
    err = err if isinstance(err, dict) else {"message": str(err or "")}
    code = str(err.get("code") or "")
    status = status_default
    for key in ("status", "status_code", "http_status"):
        try:
            if err.get(key):
                status = int(err[key])
                break
        except (TypeError, ValueError):
            pass
    if code == "subscription_sharing_usage_limit_exceeded":
        status = 429
    elif code in ("subscription_sharing_usage_unavailable", "subscription_sharing_user_unavailable"):
        status = 503
    logger.warning("ChatGPT plan stream failed: code=%s", code or "-")
    return status, _redact(error_text(status, code, str(err.get("message") or ""), str(err.get("param") or "")), request_hdrs)


def complete_sync(model: str, messages: List[Dict], hdrs: Dict[str, str], *, timeout: float = 120.0,
                  reasoning_effort: Optional[str] = None) -> str:
    """Blocking call for upstream's synchronous llm_call: streams (the route
    only streams) and returns the text once response.completed arrives."""
    from fastapi import HTTPException
    h = {"Content-Type": "application/json"}
    h.update(hdrs or {})
    if not any(str(k).lower() == "authorization" for k in h):
        raise HTTPException(401, PlanReauthRequired().message)
    payload = build_payload(model, messages, reasoning_effort)
    parts: List[str] = []
    event = ""
    with _http().stream("POST", RESPONSES_URL, json=payload, headers=h, timeout=timeout) as r:
        if r.status_code != 200:
            raise HTTPException(r.status_code, http_error(r.status_code, r.read().decode(errors="replace"), dict(r.headers), h))
        for line in r.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            try:
                data = json.loads(line[5:].strip())
            except Exception:
                continue
            kind = data.get("type") or event
            if kind == "response.output_text.delta" and isinstance(data.get("delta"), str):
                parts.append(data["delta"])
            elif kind == "response.completed":
                return "".join(parts)
            elif kind in ("response.failed", "error", "response.incomplete"):
                err = data.get("error") or (data.get("response") or {}).get("error") or data
                if kind == "response.incomplete":
                    reason = ((data.get("response") or {}).get("incomplete_details") or {}).get("reason") or "incomplete"
                    raise HTTPException(502, f"ChatGPT stopped this answer early ({reason}).")
                status, text = stream_error(err, h)
                raise HTTPException(status, text)
    raise HTTPException(502, "The ChatGPT answer was cut off before it finished.")


# ------------------------------------------------------------------ self-hosted move (CLI)

def export_credentials(owner: str) -> Dict[str, Any]:
    """OpenAI's self-hosted steps, step 3: the protected credential record of
    the selected account, for a server that cannot take the 127.0.0.1 sign-in.
    This install stops using it (the server owns later refreshes). The host id
    is left out: the server keeps its own."""
    db = _db()
    try:
        link = get_link(db, owner)
        auth = _auth_row(db, link)
        if link is None or auth is None or not auth.refresh_token:
            raise PlanError("nothing_to_export", "No ChatGPT sign-in to move for this user.")
        with refresh_lock(auth.id):
            db.refresh(auth)
            record = {
                "format": EXPORT_FORMAT,
                "issuer": ISSUER,
                "client_id": link.client_id,
                "subject": link.subject,
                "email": link.email,
                "id_token": link.id_token,
                "access_token": auth.access_token,
                "refresh_token": auth.refresh_token,
                "token_type": "Bearer",
                "expires_at": link.expires_at,
                "earliest_refresh_at": link.earliest_refresh_at,
                "scopes": (link.scopes or "").split(),
                "saved_at": int(time.time()),
            }
            clear_tokens(db, link)
            db.commit()
        _picker_changed()
        return record
    finally:
        db.close()


def import_credentials(owner: str, record: Dict[str, Any], *, jwks=None, models_get: Optional[Callable] = None) -> Dict[str, Any]:
    """OpenAI's self-hosted steps, step 4: take over a credential record made
    by export_credentials() on the person's own computer. The ID token must be
    OpenAI's and name the same client id and account (its expiry is not
    checked: the sign-in happened earlier). This install keeps its own host id."""
    if not isinstance(record, dict) or record.get("format") != EXPORT_FORMAT or record.get("issuer") != ISSUER:
        raise PlanError("bad_record", "That is not an OpenWings ChatGPT sign-in record.")
    client_id = str(record.get("client_id") or "")
    if not CLIENT_ID_RE.match(client_id) or client_id == DYNAMIC_CLIENT:
        raise PlanError("bad_record", "The record has no valid client id.")
    for key in ("access_token", "refresh_token", "id_token"):
        if not isinstance(record.get(key), str) or not record[key]:
            raise PlanError("bad_record", f"The record has no {key}.")
    claims = validate_id_token(record["id_token"], client_id=client_id, nonce=None, jwks=jwks, verify_exp=False)
    if record.get("subject") and record["subject"] != claims["sub"]:
        raise PlanError("bad_record", "The record's account does not match its ID token.")
    exp = _int_or_none(record.get("expires_at"))
    tokens = {
        "access_token": record["access_token"],
        "refresh_token": record["refresh_token"],
        "id_token": record["id_token"],
        "scope": " ".join(str(s) for s in (record.get("scopes") or [])),
        "earliest_refresh_at": record.get("earliest_refresh_at"),
        # Already expired is fine: the first use refreshes it here.
        "expires_in": max(1, exp - int(time.time())) if exp else None,
    }
    if PLAN_SCOPE not in granted_scopes(tokens):
        raise PlanError("plan_off", "This sign-in does not include ChatGPT plan use.")
    db = _db()
    try:
        link = get_link(db, owner)
        if link is not None and link.client_id != client_id:
            auth = _auth_row(db, link)
            if auth is not None and (auth.refresh_token or ""):
                raise PlanError("already_connected", "This user already has a different ChatGPT sign-in. Sign it out first.")
            link.client_id = client_id
            link.subject = None
            db.commit()
    finally:
        db.close()
    result = store_sign_in(owner, client_id, tokens, claims, transferred=True)
    try:
        _auth_id, access = access_token_for(owner)
        models = fetch_models(access or "", get=models_get)
        result["models"] = update_models(owner, models)
    except PlanError as exc:
        result["models_error"] = exc.message
    return result


def _cli(argv: Optional[List[str]] = None) -> int:
    import argparse
    import sys
    parser = argparse.ArgumentParser(
        prog="python -m wings.chatgpt_plan",
        description="Move a ChatGPT plan sign-in to a server (OpenAI's self-hosted steps). "
                    "export prints the record on stdout; import reads it from stdin. Pipe one into the other over SSH.",
    )
    parser.add_argument("action", choices=["export", "import"])
    parser.add_argument("--user", required=True, help="the OpenWings user (an admin) the sign-in belongs to")
    args = parser.parse_args(argv)
    user = args.user.strip().lower()
    try:
        from core.auth import AuthManager
        if not AuthManager().is_admin(user):
            print(f"{user!r} is not an admin of this OpenWings.", file=sys.stderr)
            return 2
        if args.action == "export":
            if sys.stdout.isatty():
                print("Refusing to print a sign-in to the screen. Pipe it into the import on your server.", file=sys.stderr)
                return 2
            record = export_credentials(user)
            sys.stdout.write(json.dumps(record))
            sys.stdout.flush()
            print("Moved out: this OpenWings no longer uses that ChatGPT sign-in.", file=sys.stderr)
            return 0
        raw = sys.stdin.read(1_000_000)
        result = import_credentials(user, json.loads(raw or "{}"))
        print(f"ChatGPT plan connected for {user}: {result.get('models', 0)} models."
              + (f" ({result['models_error']})" if result.get("models_error") else ""), file=sys.stderr)
        return 0
    except PlanError as exc:
        print(f"Could not {args.action}: {exc.message}", file=sys.stderr)
        return 1
    except json.JSONDecodeError:
        print("Could not import: the input was not a sign-in record.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_cli())

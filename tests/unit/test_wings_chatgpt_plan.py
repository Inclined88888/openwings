"""Unit tests for Continue with ChatGPT (patch 0022): OpenAI's Sign in with
ChatGPT for open-source apps, written from developers.openai.com/siwc.

OpenAI's endpoints are stubbed (no network): the authorization request, the
callback, the code exchange, ID token checks against a local JWKS, encrypted
storage, the owner-only rule, refresh, sign-out, the model list, the
Responses payload, plan errors, and the moved sign-in of OpenAI's
self-hosted steps.

Run with ./tests/unit/run.sh (inside the built image, network off)."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sqlite3
import stat
import sys
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

sys.path.insert(0, "/app")

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402

RESP_URL = "https://api.openai.com/v1/responses"
CLIENT = "oaiapp_TestClient-1"
SUB = "sub-123"


# ------------------------------------------------------------------ helpers

class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Keys:
    """An RSA key pair standing in for OpenAI's signing key."""

    def __init__(self, kid="k1"):
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        self.kid = kid
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.private.public_key()))
        jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
        self.jwks = {"keys": [jwk]}

    def id_token(self, **over):
        import jwt
        now = int(time.time())
        claims = {"iss": "https://auth.openai.com", "aud": [CLIENT], "sub": SUB, "nonce": "n0",
                  "iat": now, "exp": now + 3600, "email": "pat@example.com"}
        claims.update(over)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, self.private, algorithm="RS256", headers={"kid": self.kid})


@pytest.fixture
def cp(monkeypatch, tmp_path):
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    import wings.chatgpt_plan as cp
    cp._pending.clear()
    cp._jwks_cache.update({"set": None, "at": 0.0, "tried": 0.0})
    cp._discovery_cache.update({"doc": None, "at": 0.0})

    def no_network(*a, **k):
        raise AssertionError("no real request to OpenAI in tests")

    monkeypatch.setattr(cp, "_post_form", no_network)
    monkeypatch.setattr(cp, "_get", no_network)
    yield cp
    cp._pending.clear()


@pytest.fixture
def db(cp, monkeypatch, tmp_path):
    """A fresh SQLite database and encryption key for each test."""
    import core.database as cdb
    import src.secret_storage as ss
    from cryptography.fernet import Fernet
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    path = tmp_path / "t.db"
    eng = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(cdb, "engine", eng)
    monkeypatch.setattr(cdb, "SessionLocal", sessionmaker(bind=eng, autoflush=False, autocommit=False))
    monkeypatch.setattr(ss, "_fernet", Fernet(Fernet.generate_key()))
    link = cp.link_model()
    cdb.Base.metadata.create_all(eng, tables=[cdb.ProviderAuthSession.__table__, cdb.ModelEndpoint.__table__, link.__table__])
    return SimpleNamespace(path=path, cdb=cdb, Session=cdb.SessionLocal, Link=link)


class _Auth:
    is_configured = True
    admins = {"admin", "admin2"}

    def is_admin(self, user):
        return user in self.admins


class _FakeSession(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request.state.current_user = request.headers.get("x-test-user")
        request.state.api_token = False
        return await call_next(request)


class _OpenAI:
    """Stub of auth.openai.com and api.openai.com for one test."""

    def __init__(self, keys, *, scope="openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"):
        self.keys = keys
        self.scope = scope
        self.posts = []
        self.gets = []
        self.exchange_status = 200
        self.exchange_error = ""
        self.refresh_error = ""
        self.revoke_status = 200
        self.sub = SUB
        self.n = 0

    def post(self, url, data, timeout=20.0):
        self.posts.append((url, dict(data)))
        if url == "https://auth.openai.com/api/accounts/oauth/token":
            if data["grant_type"] == "authorization_code":
                if self.exchange_status != 200:
                    return _Resp(self.exchange_status, {"error": self.exchange_error or "server_error"})
                return _Resp(200, self._tokens(nonce=self.nonce))
            if self.refresh_error:
                return _Resp(400, {"error": self.refresh_error})
            return _Resp(200, self._tokens())
        if url == "https://auth.openai.com/oauth/revoke":
            return _Resp(self.revoke_status, None)
        raise AssertionError(f"unexpected POST {url}")

    def _tokens(self, nonce=None):
        self.n += 1
        return {
            "access_token": f"at-{self.n}", "refresh_token": f"rt-{self.n}",
            "id_token": self.keys.id_token(nonce=nonce, sub=self.sub), "token_type": "Bearer",
            "expires_in": 3600, "scope": self.scope, "earliest_refresh_at": int(time.time()) + 600,
        }

    def get(self, url, headers=None, timeout=15.0):
        self.gets.append((url, dict(headers or {})))
        if url == "https://auth.openai.com/.well-known/jwks.json":
            return _Resp(200, self.keys.jwks)
        if url == "https://auth.openai.com/.well-known/openid-configuration":
            return _Resp(200, {"issuer": "https://auth.openai.com",
                               "authorization_endpoint": "https://auth.openai.com/api/accounts/authorize",
                               "token_endpoint": "https://auth.openai.com/api/accounts/oauth/token",
                               "jwks_uri": "https://auth.openai.com/.well-known/jwks.json",
                               "revocation_endpoint": "https://auth.openai.com/oauth/revoke"})
        if url == "https://api.openai.com/v1/models":
            assert (headers or {}).get("Authorization", "").startswith("Bearer at-")
            return _Resp(200, {"models": [
                {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1 Sol", "visibility": "list"},
                {"slug": "internal-x", "display_name": "Hidden", "visibility": "hide"},
                {"slug": "gpt-6.1-luna", "display_name": "GPT-6.1 Luna", "visibility": "list"},
            ]})
        raise AssertionError(f"unexpected GET {url}")


@pytest.fixture
def app(cp, db, monkeypatch):
    import importlib
    import wings.connect_routes as cr
    cr = importlib.reload(cr)
    monkeypatch.setattr(cr, "_model_endpoint_count", lambda: 1)
    keys = _Keys()
    oa = _OpenAI(keys)
    oa.nonce = None
    monkeypatch.setattr(cp, "_post_form", oa.post)
    monkeypatch.setattr(cp, "_get", oa.get)
    a = FastAPI()
    a.state.auth_manager = _Auth()
    a.add_middleware(_FakeSession)
    a.include_router(cr.setup_connect_routes(None))
    client = TestClient(a, base_url="http://127.0.0.1:7013")
    return SimpleNamespace(client=client, oa=oa, keys=keys, cp=cp, db=db)


def _qs(url):
    parts = urlsplit(url)
    return parts, {k: v[0] for k, v in parse_qs(parts.query).items()}


def _start(app, user="admin", host=None, **body):
    headers = {"x-test-user": user}
    if host:
        headers["host"] = host
    return app.client.post("/api/wings/connect/chatgpt/start", headers=headers, json=body)


def _callback(app, params, user="admin"):
    return app.client.get("/api/wings/connect/chatgpt/callback", params=params,
                          headers={"x-test-user": user}, follow_redirects=False)


def _sign_in(app, user="admin", client_id=CLIENT):
    r = _start(app, user)
    assert r.status_code == 200, r.text
    _, q = _qs(r.json()["authorize_url"])
    app.oa.nonce = q["nonce"]
    params = {"code": "code-1", "state": q["state"], "scope": app.oa.scope}
    if client_id:
        params["client_id"] = client_id
    return q, _callback(app, params, user)


# ------------------------------------------------------------------ basics

def test_host_id_is_made_once_and_kept(cp, tmp_path):
    a = cp.host_id()
    assert a.startswith("urn:uuid:") and cp.HOST_ID_RE.match(a)
    assert cp.host_id() == a
    path = tmp_path / "wings" / "chatgpt-host-id"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    path.write_text("me@example.com")   # never an identifying value
    b = cp.host_id()
    assert b != a and b.startswith("urn:uuid:")


def test_plan_url_is_exactly_the_responses_route(cp):
    assert cp.is_plan_url(RESP_URL) and cp.is_plan_url(RESP_URL + "/")
    for other in ("https://api.openai.com/v1", "https://api.openai.com/v1/chat/completions",
                  "http://api.openai.com/v1/responses", "https://api.openai.com.evil/v1/responses",
                  RESP_URL + "?x=1", "https://chatgpt.com/backend-api/codex/responses"):
        assert not cp.is_plan_url(other), other


def _req(host, scheme="http", **headers):
    h = {"host": host}
    h.update(headers)
    return SimpleNamespace(url=SimpleNamespace(scheme=scheme), headers=h)


def test_sign_in_needs_the_browser_on_loopback(cp):
    assert cp.loopback_of(_req("127.0.0.1:7013")) == cp.Loopback("127.0.0.1", 7013)
    assert cp.loopback_of(_req("localhost:7013")) == cp.Loopback("localhost", 7013)
    assert cp.loopback_of(_req("192.168.1.5:7013")) is None
    assert cp.loopback_of(_req("ai.example.com", scheme="https")) is None
    assert cp.loopback_of(_req("127.0.0.1:7013", scheme="https")) is None
    # Behind a reverse proxy the browser is not on this machine.
    assert cp.loopback_of(_req("127.0.0.1:7000", **{"x-forwarded-for": "203.0.113.9"})) is None


def test_authorize_url_new_registration_follows_the_docs(cp):
    flow = cp.start_sign_in("admin", 7013, None)
    url = cp.authorize_url(flow, "urn:uuid:00000000-0000-4000-8000-000000000000", login_hint="x@y.z")
    parts, q = _qs(url)
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "auth.openai.com", "/api/accounts/authorize")
    assert q == {
        "client_id": "dynamic_agent_client",
        "agent_name_hint": "OpenWings",
        "ext_agent_host_id": "urn:uuid:00000000-0000-4000-8000-000000000000",
        "response_type": "code",
        "redirect_uri": "http://127.0.0.1:7013/api/wings/connect/chatgpt/callback",
        "scope": "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct",
        "resource": "https://api.openai.com/v1",
        "state": flow.state,
        "nonce": flow.nonce,
        "code_challenge_method": "S256",
        "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(flow.verifier.encode()).digest()).rstrip(b"=").decode(),
    }
    assert "+" not in parts.query and "%20" in parts.query          # every value URL-encoded
    assert len(flow.state) >= 32 and len(flow.nonce) >= 32 and flow.state != flow.nonce
    assert "login_hint" not in q and "prompt" not in q and "id_token_hint" not in q


def test_authorize_url_returning_account(cp):
    flow = cp.start_sign_in("admin", 54321, CLIENT, consent=True)
    _, q = _qs(cp.authorize_url(flow, "urn:uuid:00000000-0000-4000-8000-000000000000", login_hint="pat@example.com"))
    assert q["client_id"] == CLIENT and "agent_name_hint" not in q
    assert q["login_hint"] == "pat@example.com" and q["prompt"] == "consent"
    # Only the port changes between sign-ins; never "localhost".
    assert q["redirect_uri"] == "http://127.0.0.1:54321/api/wings/connect/chatgpt/callback"


def test_callback_client_id_rules(cp):
    new = cp.start_sign_in("admin", 7013, None)
    assert cp.callback_client_id(new, CLIENT) == CLIENT
    for bad in (None, "", "dynamic_agent_client", "oaiapp bad", "x" * 201):
        with pytest.raises(cp.PlanError) as e:
            cp.callback_client_id(new, bad)
        assert e.value.code == "registration_incomplete"
    again = cp.start_sign_in("admin", 7013, CLIENT)
    assert cp.callback_client_id(again, None) == CLIENT
    with pytest.raises(cp.PlanError) as e:
        cp.callback_client_id(again, "oaiapp_other")
    assert e.value.code == "client_mismatch"


def test_pending_sign_in_is_single_use_owned_and_expires(cp):
    f = cp.start_sign_in("admin", 7013, None)
    with pytest.raises(cp.PlanError) as e:
        cp.take_sign_in(f.state, "admin2")
    assert e.value.code == "other_user"
    assert cp.take_sign_in(f.state, "admin") is f
    with pytest.raises(cp.PlanError):
        cp.take_sign_in(f.state, "admin")
    old = cp.start_sign_in("admin", 7013, None)
    old.created -= cp.FLOW_TTL_SECONDS + 1
    with pytest.raises(cp.PlanError) as e:
        cp.take_sign_in(old.state, "admin")
    assert e.value.code == "expired"


def test_code_exchange_posts_the_documented_form(cp):
    seen = []

    def post(url, data):
        seen.append((url, data))
        return _Resp(200, {"access_token": "at", "id_token": "it", "token_type": "Bearer", "refresh_token": "rt"})

    cp.exchange_code("c1", "v1", CLIENT, "http://127.0.0.1:7013/api/wings/connect/chatgpt/callback", post=post)
    assert seen == [("https://auth.openai.com/api/accounts/oauth/token", {
        "grant_type": "authorization_code", "client_id": CLIENT, "code": "c1", "code_verifier": "v1",
        "redirect_uri": "http://127.0.0.1:7013/api/wings/connect/chatgpt/callback",
        "resource": "https://api.openai.com/v1",
    })]
    with pytest.raises(cp.PlanError) as e:
        cp.exchange_code("c1", "v1", CLIENT, "r", post=lambda u, d: _Resp(400, {"error": "invalid_grant"}))
    assert e.value.code == "expired"

    def down(u, d):
        raise OSError("down")
    with pytest.raises(cp.PlanTemporary):
        cp.exchange_code("c1", "v1", CLIENT, "r", post=down)


# ------------------------------------------------------------------ ID token

def test_id_token_checks(cp):
    import jwt
    keys = _Keys()
    jwks = jwt.PyJWKSet.from_dict(keys.jwks)
    claims = cp.validate_id_token(keys.id_token(), client_id=CLIENT, nonce="n0", jwks=jwks)
    assert claims["sub"] == SUB
    bad = [
        keys.id_token(aud=["oaiapp_other"]),
        keys.id_token(iss="https://evil.example"),
        keys.id_token(nonce="other"),
        keys.id_token(exp=int(time.time()) - 60),
        keys.id_token(sub=None),
        _Keys().id_token(),                                         # signed by another key
        jwt.encode({"iss": "https://auth.openai.com", "aud": CLIENT, "sub": SUB, "nonce": "n0",
                    "iat": int(time.time()), "exp": int(time.time()) + 60}, "s" * 32, algorithm="HS256",
                   headers={"kid": "k1"}),                           # algorithm confusion
    ]
    for token in bad:
        with pytest.raises(cp.PlanError) as e:
            cp.validate_id_token(token, client_id=CLIENT, nonce="n0", jwks=jwks)
        assert e.value.code == "bad_id_token"


def test_unknown_signing_key_refetches_the_jwks_once(cp):
    old, new = _Keys("old"), _Keys("new")
    calls = []

    def get(url, headers=None):
        calls.append(url)
        return _Resp(200, (old if len(calls) == 1 else new).jwks)

    cp._signing_key("old", "RS256", get=get)
    cp._signing_key("new", "RS256", get=get)
    assert calls == ["https://auth.openai.com/.well-known/jwks.json"] * 2


# ------------------------------------------------------------------ models, payload, errors

def test_model_list_keeps_listed_models_in_order(cp):
    oa = _OpenAI(_Keys())
    assert cp.fetch_models("at-1", get=oa.get) == [("gpt-6.1-sol", "GPT-6.1 Sol"), ("gpt-6.1-luna", "GPT-6.1 Luna")]


def test_responses_payload_has_only_supported_fields(cp):
    p = cp.build_payload("gpt-6.1-sol", [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "tool", "content": "result"},
    ], "high")
    assert p == {
        "model": "gpt-6.1-sol",
        "instructions": "Be brief.",
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"role": "assistant", "content": [{"type": "output_text", "text": "hello"}]},
            {"role": "user", "content": [{"type": "input_text", "text": "result"}]},
        ],
        "store": False,
        "stream": True,
        "reasoning": {"effort": "high"},
    }
    plain = cp.build_payload("m", [{"role": "user", "content": "x"}])
    for forbidden in ("temperature", "max_output_tokens", "top_p", "previous_response_id", "metadata",
                      "user", "tools", "truncation", "background", "instructions", "reasoning"):
        assert forbidden not in plain


def test_plan_errors_keep_status_code_and_request_id(cp):
    body = json.dumps({"error": {"code": "subscription_sharing_usage_limit_exceeded", "message": "limit"}})
    text = cp.http_error(429, body, {"x-request-id": "req_9"}, {"Authorization": "Bearer at-secret"})
    assert "https://chatgpt.com/settings/usage" in text
    assert "HTTP 429" in text and "subscription_sharing_usage_limit_exceeded" in text and "req_9" in text
    detail = cp.http_error(503, json.dumps({"detail": "direct routing unavailable"}), {})
    assert "HTTP 503" in detail
    leak = cp.http_error(400, json.dumps({"error": {"message": "bad token at-secret"}}), {}, {"Authorization": "Bearer at-secret"})
    assert "at-secret" not in leak
    status, msg = cp.stream_error({"code": "subscription_sharing_unsupported_capability", "param": "tools"})
    assert status == 400 and "(tools)" in msg


def test_revoke_uses_the_discovered_endpoint(cp):
    oa = _OpenAI(_Keys())
    assert cp.revoke("rt-1", CLIENT, post=oa.post, discover=lambda: cp.discovery(get=oa.get)) is True
    assert oa.posts[-1] == ("https://auth.openai.com/oauth/revoke",
                            {"token": "rt-1", "token_type_hint": "refresh_token", "client_id": CLIENT})
    oa.revoke_status = 503
    assert cp.revoke("rt-1", CLIENT, post=oa.post, discover=lambda: cp.discovery(get=oa.get), sleep=lambda s: None) is False
    assert len([p for p in oa.posts if p[0].endswith("/oauth/revoke")]) == 1 + 3
    cp._discovery_cache.update({"doc": None, "at": 0.0})
    evil = lambda url, headers=None: _Resp(200, {"issuer": "https://auth.openai.com", "revocation_endpoint": "https://evil.example/revoke"})
    assert cp.revoke("rt-1", CLIENT, post=oa.post, discover=lambda: cp.discovery(get=evil)) is False


# ------------------------------------------------------------------ the whole sign-in

def test_full_sign_in_stores_encrypted_and_owner_only(app):
    q, r = _sign_in(app)
    assert r.status_code == 303 and r.headers["location"] == "/?ow_chatgpt=connected"
    url, form = [p for p in app.oa.posts if p[1].get("grant_type") == "authorization_code"][0]
    assert form["client_id"] == CLIENT and form["redirect_uri"] == q["redirect_uri"] and form["resource"] == "https://api.openai.com/v1"
    assert form["code_verifier"] and hashlib.sha256(form["code_verifier"].encode()).digest() == base64.urlsafe_b64decode(q["code_challenge"] + "=")
    # Tokens never reach the browser.
    assert "at-1" not in r.text and "rt-1" not in r.text and "at-1" not in r.headers["location"]
    st = app.client.get("/api/wings/connect/status", headers={"x-test-user": "admin"}).json()["chatgpt"]
    assert st["state"] == "connected" and st["email"] == "pat@example.com" and st["models"] == 2 and st["mode"] == "ready"
    assert "at-1" not in json.dumps(st) and "rt-1" not in json.dumps(st)
    # Encrypted at rest.
    raw = sqlite3.connect(app.db.path)
    for col in ("access_token", "refresh_token"):
        val = raw.execute(f"select {col} from provider_auth_sessions where provider='chatgpt-plan'").fetchone()[0]
        assert val.startswith("enc:") and "rt-1" not in val and "at-1" not in val
    for col in ("id_token", "email", "subject"):
        assert raw.execute(f"select {col} from wings_chatgpt_plan").fetchone()[0].startswith("enc:")
    owner, base, auth_id, tools, models = raw.execute(
        "select owner, base_url, provider_auth_id, supports_tools, cached_models from model_endpoints").fetchone()
    assert (owner, base, tools) == ("admin", RESP_URL, 0) and json.loads(models) == ["gpt-6.1-sol", "gpt-6.1-luna"]
    assert app.cp.display_names(auth_id) == {"gpt-6.1-sol": "GPT-6.1 Sol", "gpt-6.1-luna": "GPT-6.1 Luna"}
    # One person per connection: everyone else is refused, admins included.
    assert app.cp.resolve_runtime_credentials(auth_id, "admin")["api_key"] == "at-1"
    for other in ("admin2", None, ""):
        with pytest.raises(app.cp.PlanAuthNotFound):
            app.cp.resolve_runtime_credentials(auth_id, other)


def test_issued_client_id_is_saved_before_the_exchange(app):
    app.oa.exchange_status = 500
    _, r = _sign_in(app)
    assert r.headers["location"] == "/?ow_chatgpt=exchange_failed"
    s = app.db.Session()
    try:
        assert s.query(app.db.Link).one().client_id == CLIENT
    finally:
        s.close()
    # The next attempt reuses the issued client id: no new registration.
    _, q = _qs(_start(app).json()["authorize_url"])
    assert q["client_id"] == CLIENT and "agent_name_hint" not in q


def test_returning_sign_in_may_leave_out_client_id_but_not_change_account(app):
    _sign_in(app)
    q, r = _sign_in(app, client_id=None)
    assert r.headers["location"] == "/?ow_chatgpt=connected" and q["login_hint"] == "pat@example.com"
    app.oa.sub = "someone-else"
    _, r = _sign_in(app, client_id=None)
    assert r.headers["location"] == "/?ow_chatgpt=other_account"
    s = app.db.Session()
    try:
        auth_id = s.query(app.db.Link).one().auth_id
    finally:
        s.close()
    assert app.cp.resolve_runtime_credentials(auth_id, "admin")["api_key"] == "at-2"


def test_callback_refusals(app):
    r = _start(app)
    _, q = _qs(r.json()["authorize_url"])
    assert _callback(app, {"code": "c", "state": q["state"], "client_id": CLIENT}, user="admin2").headers["location"] == "/?ow_chatgpt=other_user"
    assert _callback(app, {"code": "c", "state": "forged", "client_id": CLIENT}).headers["location"] == "/?ow_chatgpt=expired"
    assert _callback(app, {"error": "access_denied", "state": q["state"]}).headers["location"] == "/?ow_chatgpt=denied"
    assert app.oa.posts == []      # nothing was exchanged
    assert _callback(app, {"code": "c", "state": q["state"], "client_id": CLIENT}).headers["location"] == "/?ow_chatgpt=expired"


def test_without_plan_scope_the_sign_in_is_kept_but_plan_use_is_off(app):
    app.oa.scope = "openid profile email offline_access"
    _, r = _sign_in(app)
    assert r.headers["location"] == "/?ow_chatgpt=plan_off"
    raw = sqlite3.connect(app.db.path)
    assert raw.execute("select count(*) from model_endpoints").fetchone()[0] == 0
    st = app.client.get("/api/wings/connect/status", headers={"x-test-user": "admin"}).json()["chatgpt"]
    assert st["state"] == "plan_off"
    _, q = _qs(_start(app, consent=True).json()["authorize_url"])
    assert q["prompt"] == "consent" and q["client_id"] == CLIENT


def test_start_from_other_addresses(app):
    r = _start(app, host="localhost:7013")
    assert r.status_code == 409 and r.json()["mode"] == "switch"
    assert r.json()["switch_url"] == "http://127.0.0.1:7013/?ow_open=chatgpt"
    r = _start(app, host="192.168.1.5:7013")
    assert r.status_code == 409 and r.json()["mode"] == "remote" and "authorize_url" not in r.json()
    assert app.client.post("/api/wings/connect/chatgpt/start", json={}).status_code == 403     # not signed in


def test_refresh_replaces_tokens_together(app, monkeypatch):
    _sign_in(app)
    s = app.db.Session()
    link = s.query(app.db.Link).one()
    auth_id = link.auth_id
    link.expires_at = int(time.time()) + 30
    link.earliest_refresh_at = None
    s.commit()
    s.close()
    creds = app.cp.resolve_runtime_credentials(auth_id, "admin")
    assert creds == {"provider": "chatgpt-plan", "base_url": RESP_URL, "api_key": "at-2"}
    url, form = app.oa.posts[-1]
    assert form == {"grant_type": "refresh_token", "client_id": CLIENT, "refresh_token": "rt-1",
                    "resource": "https://api.openai.com/v1"}
    s = app.db.Session()
    try:
        auth = s.query(app.db.cdb.ProviderAuthSession).one()
        assert (auth.access_token, auth.refresh_token) == ("at-2", "rt-2")
        assert s.query(app.db.Link).one().expires_at > time.time() + 3000
    finally:
        s.close()


def test_unusable_refresh_token_asks_for_a_new_sign_in(app):
    _sign_in(app)
    s = app.db.Session()
    link = s.query(app.db.Link).one()
    auth_id = link.auth_id
    link.expires_at = int(time.time()) - 5
    s.commit()
    s.close()
    app.oa.refresh_error = "refresh_token_reused"
    with pytest.raises(app.cp.PlanReauthRequired):
        app.cp.resolve_runtime_credentials(auth_id, "admin")
    st = app.cp.status_for("admin")
    assert st["state"] == "needs_signin"
    raw = sqlite3.connect(app.db.path)
    assert raw.execute("select is_enabled from model_endpoints").fetchone()[0] == 0
    assert raw.execute("select refresh_token from provider_auth_sessions").fetchone()[0] is None


def test_sign_out_revokes_and_keeps_the_client_link(app):
    _sign_in(app)
    r = app.client.post("/api/wings/connect/chatgpt/disconnect", headers={"x-test-user": "admin"})
    assert r.json()["revoked"] is True
    assert app.oa.posts[-1] == ("https://auth.openai.com/oauth/revoke",
                                {"token": "rt-1", "token_type_hint": "refresh_token", "client_id": CLIENT})
    st = app.cp.status_for("admin")
    assert st["state"] == "signed_out" and not st["revoke_unconfirmed"]
    raw = sqlite3.connect(app.db.path)
    assert raw.execute("select client_id, id_token from wings_chatgpt_plan").fetchone() == (CLIENT, None)
    _, q = _qs(_start(app).json()["authorize_url"])
    assert q["client_id"] == CLIENT and "id_token_hint" not in q


def test_unconfirmed_revocation_is_reported(app):
    _sign_in(app)
    app.oa.revoke_status = 400
    r = app.client.post("/api/wings/connect/chatgpt/disconnect", headers={"x-test-user": "admin"})
    assert r.json()["revoked"] is False
    assert app.cp.status_for("admin")["revoke_unconfirmed"] is True


def test_forget_starts_a_new_registration(app):
    _sign_in(app)
    assert app.client.post("/api/wings/connect/chatgpt/forget", headers={"x-test-user": "admin"}).json() == {"ok": True}
    raw = sqlite3.connect(app.db.path)
    assert raw.execute("select count(*) from wings_chatgpt_plan").fetchone()[0] == 0
    assert raw.execute("select count(*) from model_endpoints").fetchone()[0] == 0
    _, q = _qs(_start(app).json()["authorize_url"])
    assert q["client_id"] == "dynamic_agent_client"


def test_deleting_the_endpoint_signs_out(app):
    _sign_in(app)
    s = app.db.Session()
    try:
        auth = s.query(app.db.cdb.ProviderAuthSession).one()
        app.cp.on_auth_deleted(s, auth)
        s.delete(auth)
        s.commit()
    finally:
        s.close()
    assert app.oa.posts[-1][0] == "https://auth.openai.com/oauth/revoke"
    assert app.cp.status_for("admin")["state"] == "signed_out"


def test_moving_a_sign_in_to_a_server(app, monkeypatch, tmp_path):
    """OpenAI's self-hosted steps: sign in on your own computer, move the
    credential record over SSH, the server takes over refreshes and keeps its
    own host id."""
    _sign_in(app)
    server_host = app.cp.host_id()
    record = app.cp.export_credentials("admin")
    assert record["format"] == "openwings-chatgpt-plan/1" and record["refresh_token"] == "rt-1"
    assert "ext_agent_host_id" not in record and server_host not in json.dumps(record)
    assert app.cp.status_for("admin")["state"] == "signed_out"      # this install stopped using it
    app.cp.forget("admin")
    import jwt
    result = app.cp.import_credentials("admin2", record, jwks=jwt.PyJWKSet.from_dict(app.keys.jwks), models_get=app.oa.get)
    assert result["plan_enabled"] and result["models"] == 2
    st = app.cp.status_for("admin2")
    assert st["state"] == "connected" and st["transferred"] is True
    assert app.cp.host_id() == server_host
    tampered = dict(record, subject="someone-else")
    with pytest.raises(app.cp.PlanError):
        app.cp.import_credentials("admin", tampered, jwks=jwt.PyJWKSet.from_dict(app.keys.jwks))


# ------------------------------------------------------------------ OpenWings call path

def test_endpoint_resolver_routes_plan_endpoints(app):
    _sign_in(app)
    from src import endpoint_resolver as er
    assert er.normalize_base(RESP_URL) == RESP_URL
    assert er.normalize_base("https://api.openai.com/v1/chat/completions") == "https://api.openai.com/v1"
    assert er.build_chat_url(RESP_URL) == RESP_URL
    assert er.build_models_url(RESP_URL) is None
    assert er.endpoint_cost_tracked(RESP_URL) is False
    h = er.build_headers("at-1", RESP_URL)
    assert h["Authorization"] == "Bearer at-1" and h["User-Agent"].startswith("OpenWings/")
    assert not any("chatgpt.com" in str(v) or "codex" in str(v).lower() for v in h.values())
    s = app.db.Session()
    ep = s.query(app.db.cdb.ModelEndpoint).one()
    s.close()
    assert er.resolve_endpoint_runtime(ep, owner="admin") == (RESP_URL, "at-1")
    with pytest.raises(Exception):
        er.resolve_endpoint_runtime(ep, owner="admin2")
    from src.chatgpt_subscription import uses_session_token
    assert uses_session_token(RESP_URL) and not uses_session_token("https://api.openai.com/v1")


class _Stream:
    def __init__(self, status, lines, headers=None, body=b""):
        self.status_code = status
        self._lines = lines
        self.headers = headers or {}
        self._body = body

    async def aread(self):
        return self._body

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Client:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def stream(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append((method, url, json, headers))
        return self.resp


def _run_stream(monkeypatch, resp, headers=None):
    from src import llm_core
    client = _Client(resp)
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda url: False)

    async def collect():
        out = []
        async for chunk in llm_core._stream_llm_inner(
            RESP_URL, "gpt-6.1-sol",
            [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
            temperature=0.7, max_tokens=500,
            headers=headers if headers is not None else {"Authorization": "Bearer at-1", "User-Agent": "OpenWings/1.0"},
        ):
            out.append(chunk)
        return out
    return client, asyncio.run(collect())


def _events(chunks):
    out = []
    for c in chunks:
        for line in c.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                out.append(json.loads(line[6:]))
            elif line == "data: [DONE]":
                out.append("DONE")
    return out


def test_stream_sends_the_documented_request(monkeypatch):
    from src import llm_core
    assert llm_core._detect_provider(RESP_URL) == "chatgpt-plan"
    assert llm_core._detect_provider("https://api.openai.com/v1") == "openai"
    lines = [
        "event: response.output_text.delta", 'data: {"type":"response.output_text.delta","delta":"Hel"}',
        'data: {"type":"response.output_text.delta","delta":"lo"}',
        'data: {"type":"response.completed","response":{"usage":{"input_tokens":3,"output_tokens":2}}}',
    ]
    client, chunks = _run_stream(monkeypatch, _Stream(200, lines))
    method, url, payload, headers = client.calls[0]
    assert (method, url) == ("POST", RESP_URL)
    assert payload["store"] is False and payload["stream"] is True and payload["instructions"] == "sys"
    assert "temperature" not in payload and "max_output_tokens" not in payload and "max_tokens" not in payload
    assert all(item["role"] != "system" for item in payload["input"])
    assert headers["Authorization"] == "Bearer at-1" and headers["Accept"] == "text/event-stream"
    ev = _events(chunks)
    assert [e["delta"] for e in ev if isinstance(e, dict) and "delta" in e] == ["Hel", "lo"]
    assert ev[-1] == "DONE"


def test_stream_without_completed_is_an_error(monkeypatch):
    _, chunks = _run_stream(monkeypatch, _Stream(200, ['data: {"type":"response.output_text.delta","delta":"Hel"}']))
    last = _events(chunks)[-1]
    assert last["fallback_eligible"] is False and "cut off" in last["text"]


def test_plan_errors_never_fall_back_to_another_way_of_paying(monkeypatch):
    body = json.dumps({"error": {"code": "subscription_sharing_usage_limit_exceeded", "message": "limit"}}).encode()
    _, chunks = _run_stream(monkeypatch, _Stream(429, [], {"x-request-id": "req_1"}, body))
    err = _events(chunks)[-1]
    assert err["status"] == 429 and err["fallback_eligible"] is False and "settings/usage" in err["text"]
    failed = 'data: {"type":"response.failed","response":{"error":{"code":"subscription_sharing_usage_limit_exceeded"}}}'
    _, chunks = _run_stream(monkeypatch, _Stream(200, [failed]))
    err = _events(chunks)[-1]
    assert err["status"] == 429 and err["fallback_eligible"] is False


def test_no_sign_in_means_no_request(monkeypatch):
    client, chunks = _run_stream(monkeypatch, _Stream(200, []), headers={})
    assert client.calls == []
    err = _events(chunks)[-1]
    assert err["status"] == 401 and "Continue with ChatGPT" in err["text"]


def test_switched_off_message_points_to_the_official_sign_in(monkeypatch):
    monkeypatch.delenv("WINGS_ALLOW_UNOFFICIAL_SIGNIN", raising=False)
    from wings.unofficial import provider_blocked
    assert "Continue with ChatGPT" in provider_blocked("chatgpt-subscription")
    assert "Continue with ChatGPT" not in provider_blocked("copilot")
    assert provider_blocked("chatgpt-plan") is None


def test_access_log_keeps_only_the_callback_path():
    import logging
    from wings.connect_routes import CallbackQueryFilter
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:5", "GET", "/api/wings/connect/chatgpt/callback?code=secret&state=s", "1.1", 303), None)
    assert CallbackQueryFilter().filter(rec)
    assert "secret" not in rec.getMessage() and "/api/wings/connect/chatgpt/callback?[redacted]" in rec.getMessage()
    other = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                              ("1.2.3.4:5", "GET", "/api/models?refresh=true", "1.1", 200), None)
    CallbackQueryFilter().filter(other)
    assert "/api/models?refresh=true" in other.getMessage()

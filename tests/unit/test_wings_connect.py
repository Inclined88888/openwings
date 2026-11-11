"""Unit tests for Connect (patch 0021): OpenRouter PKCE, state and callback
handling with the key exchange stubbed, the origin a sign-in returns to, the
MCP OAuth owner check and client identity, the switch that turns off the
sign-ins that pose as another company's app (no request of any kind while
off), the Google MCP sign-in's state and PKCE, sign-ins that only a person's
own browser can finish, and a cancelled app sign-in.

Run with ./tests/unit/run.sh (inside the built image, network off)."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import json
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

sys.path.insert(0, "/app")

from fastapi import FastAPI, Form, HTTPException, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402


# ------------------------------------------------------------------ helpers

class _Auth:
    is_configured = True
    admins = {"admin", "admin2"}

    def is_admin(self, user):
        return user in self.admins


class _FakeSession(BaseHTTPMiddleware):
    """Stands in for upstream's AuthMiddleware: X-Test-User is the signed-in
    person (cookie session); no header = not signed in."""

    async def dispatch(self, request, call_next):
        request.state.current_user = request.headers.get("x-test-user")
        request.state.api_token = request.headers.get("x-test-token") == "1"
        return await call_next(request)


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


@pytest.fixture
def connect(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("ALLOWED_ORIGINS", "http://localhost:7013,http://127.0.0.1:7013,http://192.168.1.5:7013")
    monkeypatch.setenv("OAUTH_REDIRECT_BASE_URL", "http://127.0.0.1:7013")
    monkeypatch.delenv("APP_PUBLIC_URL", raising=False)
    import wings.connect_routes as cr
    cr = importlib.reload(cr)
    monkeypatch.setattr(cr, "_model_endpoint_count", lambda: 0)
    monkeypatch.setattr(cr, "_usable_endpoint_count", lambda *a, **k: 0)

    created = []
    exchanges = []

    def fake_post(url, payload, timeout=20.0):
        exchanges.append((url, dict(payload)))
        if payload.get("code") == "bad":
            return _Resp(403, {"error": "Invalid code or code_verifier"})
        return _Resp(200, {"key": "sk-or-v1-secretkey", "user_id": "u1"})

    monkeypatch.setattr(cr, "_post_json", fake_post)

    app = FastAPI()
    app.state.auth_manager = _Auth()
    app.add_middleware(_FakeSession)

    # Stand-in for upstream's POST /api/model-endpoints: the connect code must
    # find and call this route's own function, not a copy of it.
    @app.post("/api/model-endpoints")
    def create_model_endpoint(request: Request, name: str = Form(""), base_url: str = Form(...),
                              api_key: str = Form(""), skip_probe: str = Form("false"),
                              require_models: str = Form("false"), model_type: str = Form("llm"),
                              endpoint_kind: str = Form("auto"), model_refresh_mode: str = Form(""),
                              model_refresh_interval: str = Form(""), model_refresh_timeout: str = Form(""),
                              supports_tools: str = Form(""), pinned_models: str = Form(""),
                              container_local: str = Form("false"), shared: str = Form("true")):
        from core.middleware import require_admin
        require_admin(request)
        created.append({"name": name, "base_url": base_url, "api_key": api_key,
                        "endpoint_kind": endpoint_kind, "user": request.state.current_user})
        return {"id": "ep1", "name": name, "models": ["a/b", "c/d"], "online": True}

    app.include_router(cr.setup_connect_routes(None))
    client = TestClient(app, base_url="http://127.0.0.1:7013")
    yield SimpleNamespace(cr=cr, client=client, created=created, exchanges=exchanges)
    cr._flows.clear()


def _start(ctx, user="admin", host=None):
    headers = {"x-test-user": user}
    if host:
        headers["host"] = host
    r = ctx.client.post("/api/wings/connect/openrouter/start", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _qs(url):
    parts = urlsplit(url)
    return parts, {k: v[0] for k, v in parse_qs(parts.query).items()}


# ------------------------------------------------------------------ PKCE

def test_s256_matches_rfc7636_example():
    from wings.connect_routes import s256_challenge
    assert s256_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_verifier_is_long_random_and_url_safe():
    from wings.connect_routes import new_verifier
    a, b = new_verifier(), new_verifier()
    assert a != b
    assert 43 <= len(a) <= 128
    assert set(a) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


# ------------------------------------------------------------------ OpenRouter

def test_start_builds_openrouters_documented_authorize_url(connect):
    d = _start(connect)
    assert d["mode"] == "callback"
    parts, q = _qs(d["authorize_url"])
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "openrouter.ai", "/auth")
    assert q["callback_url"] == "http://127.0.0.1:7013/api/wings/connect/openrouter/callback"
    assert q["code_challenge_method"] == "S256"
    assert len(q["code_challenge"]) == 43 and "=" not in q["code_challenge"]
    assert len(q["state"]) >= 32
    assert q["key_label"] == "OpenWings"
    flow = connect.cr._flows[q["state"]]
    assert flow.user == "admin"
    expected = base64.urlsafe_b64encode(hashlib.sha256(flow.verifier.encode()).digest()).rstrip(b"=").decode()
    assert q["code_challenge"] == expected
    assert "verifier" not in json.dumps(d)


def test_callback_url_follows_the_browsers_trusted_origin(connect):
    _, q = _qs(_start(connect, host="localhost:7013")["authorize_url"])
    assert q["callback_url"] == "http://localhost:7013/api/wings/connect/openrouter/callback"
    # A Host header that is not trusted is never echoed; a callback to the
    # configured address would not reach that browser, so the code is shown
    # on OpenRouter's page instead.
    d = _start(connect, host="evil.example:7013")
    assert d["mode"] == "paste"
    _, q = _qs(d["authorize_url"])
    assert "callback_url" not in q and "evil" not in d["authorize_url"]


def test_callback_exchanges_code_and_creates_the_endpoint(connect):
    _, q = _qs(_start(connect)["authorize_url"])
    verifier = connect.cr._flows[q["state"]].verifier
    r = connect.client.get(
        "/api/wings/connect/openrouter/callback",
        params={"code": "abc123", "state": q["state"]},
        headers={"x-test-user": "admin"}, follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/?ow_connect=openrouter"
    url, payload = connect.exchanges[-1]
    assert url == "https://openrouter.ai/api/v1/auth/keys"
    assert payload == {"code": "abc123", "code_verifier": verifier, "code_challenge_method": "S256"}
    assert connect.created == [{
        "name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1",
        "api_key": "sk-or-v1-secretkey", "endpoint_kind": "api", "user": "admin",
    }]
    assert "sk-or" not in r.headers["location"] and "sk-or" not in r.text


def test_callback_is_single_use(connect):
    _, q = _qs(_start(connect)["authorize_url"])
    args = dict(params={"code": "abc", "state": q["state"]}, headers={"x-test-user": "admin"}, follow_redirects=False)
    assert connect.client.get("/api/wings/connect/openrouter/callback", **args).headers["location"] == "/?ow_connect=openrouter"
    again = connect.client.get("/api/wings/connect/openrouter/callback", **args)
    assert again.headers["location"] == "/?ow_connect_error=expired"
    assert len(connect.created) == 1


def test_callback_refuses_another_signed_in_user(connect):
    _, q = _qs(_start(connect, user="admin")["authorize_url"])
    r = connect.client.get(
        "/api/wings/connect/openrouter/callback",
        params={"code": "abc", "state": q["state"]},
        headers={"x-test-user": "admin2"}, follow_redirects=False,
    )
    assert r.headers["location"] == "/?ow_connect_error=other_user"
    assert connect.created == [] and connect.exchanges == []
    # The flow is still there for the person who started it.
    r = connect.client.get(
        "/api/wings/connect/openrouter/callback",
        params={"code": "abc", "state": q["state"]},
        headers={"x-test-user": "admin"}, follow_redirects=False,
    )
    assert r.headers["location"] == "/?ow_connect=openrouter"


def test_callback_unknown_state_missing_code_and_refused_code(connect):
    hdr = {"x-test-user": "admin"}
    r = connect.client.get("/api/wings/connect/openrouter/callback", params={"code": "abc", "state": "forged"},
                           headers=hdr, follow_redirects=False)
    assert r.headers["location"] == "/?ow_connect_error=expired"
    r = connect.client.get("/api/wings/connect/openrouter/callback", headers=hdr, follow_redirects=False)
    assert r.headers["location"] == "/?ow_connect_error=denied"
    _, q = _qs(_start(connect)["authorize_url"])
    r = connect.client.get("/api/wings/connect/openrouter/callback", params={"code": "bad", "state": q["state"]},
                           headers=hdr, follow_redirects=False)
    assert r.headers["location"] == "/?ow_connect_error=expired"
    assert connect.created == []


def test_only_a_signed_in_admin_person_may_connect(connect):
    assert connect.client.post("/api/wings/connect/openrouter/start").status_code == 403
    assert connect.client.post("/api/wings/connect/openrouter/start", headers={"x-test-user": "bob"}).status_code == 403
    assert connect.client.post("/api/wings/connect/openrouter/start",
                               headers={"x-test-user": "admin", "x-test-token": "1"}).status_code == 403
    _, q = _qs(_start(connect)["authorize_url"])
    r = connect.client.get("/api/wings/connect/openrouter/callback", params={"code": "abc", "state": q["state"]},
                           headers={"x-test-user": "bob"}, follow_redirects=False)
    assert r.headers["location"] == "/?ow_connect_error=not_admin"
    # A member gets no Connect data, only whether there is an AI to use.
    assert connect.client.get("/api/wings/connect/status", headers={"x-test-user": "bob"}).json() == {"admin": False, "ai_ready": False}


def test_flows_expire_after_ten_minutes(connect, monkeypatch):
    _, q = _qs(_start(connect)["authorize_url"])
    flow = connect.cr._flows[q["state"]]
    flow.created -= connect.cr.FLOW_TTL_SECONDS + 1
    r = connect.client.get("/api/wings/connect/openrouter/callback", params={"code": "abc", "state": q["state"]},
                           headers={"x-test-user": "admin"}, follow_redirects=False)
    assert r.headers["location"] == "/?ow_connect_error=expired"
    assert connect.exchanges == []


def test_plain_http_lan_address_uses_the_paste_mode(connect):
    d = _start(connect, host="192.168.1.5:7013")
    assert d["mode"] == "paste"
    parts, q = _qs(d["authorize_url"])
    assert parts.netloc == "openrouter.ai" and "callback_url" not in q and "state" not in q
    assert q["code_challenge_method"] == "S256"
    # The callback route will not accept a paste-mode flow, and vice versa.
    r = connect.client.post("/api/wings/connect/openrouter/code", json={"flow": d["flow"], "code": "xyz"},
                            headers={"x-test-user": "admin2"})
    assert r.status_code == 400 and r.json()["error"] == "other_user"
    r = connect.client.post("/api/wings/connect/openrouter/code", json={"flow": d["flow"], "code": "xyz"},
                            headers={"x-test-user": "admin"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert "sk-or" not in r.text
    assert connect.created[-1]["api_key"] == "sk-or-v1-secretkey"


def test_status_reports_mode_switch_and_apps(connect):
    st = connect.client.get("/api/wings/connect/status", headers={"x-test-user": "admin"}).json()
    assert st["admin"] is True and st["model_endpoints"] == 0 and st["usable_endpoints"] == 0
    assert st["unofficial_signin"] is False and st["openrouter_mode"] == "callback"
    urls = {a["slug"]: a["url"] for a in st["apps"]}
    assert urls == {
        "notion": "https://mcp.notion.com/mcp",
        "linear": "https://mcp.linear.app/mcp",
        "atlassian": "https://mcp.atlassian.com/v2/mcp",
    }


# ------------------------------------------------------------------ origin

def test_origin_rules(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGINS", "http://localhost:7011, https://ai.example.com")
    monkeypatch.setenv("OAUTH_REDIRECT_BASE_URL", "http://127.0.0.1:7011/")
    from wings import origin

    def req(host, scheme="http"):
        return SimpleNamespace(headers={"host": host}, url=SimpleNamespace(scheme=scheme))

    assert origin.redirect_base_for(req("localhost:7011")) == "http://localhost:7011"
    assert origin.redirect_base_for(req("ai.example.com", "https")) == "https://ai.example.com"
    assert origin.redirect_base_for(req("127.0.0.1:7011")) == "http://127.0.0.1:7011"
    assert origin.redirect_base_for(req("attacker.test")) == "http://127.0.0.1:7011"
    assert origin.redirect_base_for(req("localhost:7011@attacker.test")) == "http://127.0.0.1:7011"
    assert origin.is_loopback_or_https("http://localhost:7011")
    assert origin.is_loopback_or_https("http://127.0.0.1:9")
    assert origin.is_loopback_or_https("https://ai.example.com")
    assert not origin.is_loopback_or_https("http://192.168.1.5:7011")
    monkeypatch.delenv("OAUTH_REDIRECT_BASE_URL")
    monkeypatch.setenv("APP_PORT", "7860")
    assert origin.configured_base() == "http://localhost:7860"


# ------------------------------------------------------------------ MCP OAuth

def test_mcp_callback_completes_only_for_the_person_who_started_it():
    from src import mcp_oauth

    async def run():
        mcp_oauth.register_pending("s-owned", owner="alice")
        mcp_oauth.register_pending("s-app", owner=None)
        assert mcp_oauth.resolve_pending("s-owned", "c", user="mallory", is_admin=True) is False
        assert mcp_oauth.pending_owner("s-owned") == (True, "alice")
        assert mcp_oauth.resolve_pending("s-owned", "c", user="alice") is True
        assert mcp_oauth._pending["s-owned"].result() == ("c", "s-owned")
        assert mcp_oauth.resolve_pending("s-app", "c", user="bob", is_admin=False) is False
        assert mcp_oauth.resolve_pending("s-app", "c", user="admin", is_admin=True) is True
        for s in ("s-owned", "s-app"):
            mcp_oauth._discard_pending(s)

    asyncio.run(run())


def test_mcp_client_registers_as_openwings_with_the_browsers_callback(monkeypatch):
    from src import mcp_oauth
    monkeypatch.setattr(mcp_oauth.DbTokenStorage, "_load", lambda self: {})
    mcp_oauth.set_flow_context("srv1", redirect_uri="http://localhost:7013/api/mcp/oauth/callback", owner="alice")
    provider = mcp_oauth.build_provider("srv1", "https://mcp.notion.com/mcp")
    meta = provider.context.client_metadata
    assert meta.client_name == "OpenWings"
    assert [str(u) for u in meta.redirect_uris] == ["http://localhost:7013/api/mcp/oauth/callback"]
    assert meta.token_endpoint_auth_method == "none"
    assert "srv1" not in mcp_oauth._flow_ctx  # consumed


def test_background_reconnect_reuses_the_registered_callback(monkeypatch):
    from src import mcp_oauth
    stored = {"client_info": {"client_id": "abc", "redirect_uris": ["http://localhost:7011/api/mcp/oauth/callback"]}}
    monkeypatch.setattr(mcp_oauth.DbTokenStorage, "_load", lambda self: stored)
    provider = mcp_oauth.build_provider("srv2", "https://mcp.linear.app/mcp")
    assert [str(u) for u in provider.context.client_metadata.redirect_uris] == ["http://localhost:7011/api/mcp/oauth/callback"]


def test_registration_for_another_callback_is_not_reused(monkeypatch):
    from src import mcp_oauth
    info = {"client_id": "abc", "redirect_uris": ["http://localhost:7000/api/mcp/oauth/callback"]}
    monkeypatch.setattr(mcp_oauth.DbTokenStorage, "_load", lambda self: {"client_info": info})
    st = mcp_oauth.DbTokenStorage("srv3", session_factory=object)
    assert asyncio.run(st.get_client_info()).client_id == "abc"
    st.require_redirect_uri("http://127.0.0.1:7013/api/mcp/oauth/callback")
    assert asyncio.run(st.get_client_info()) is None
    st.require_redirect_uri("http://localhost:7000/api/mcp/oauth/callback")
    assert asyncio.run(st.get_client_info()).client_id == "abc"


def test_mcp_redirect_uri_for_request(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGINS", "http://localhost:7013")
    monkeypatch.setenv("OAUTH_REDIRECT_BASE_URL", "http://127.0.0.1:7013")
    from src import mcp_oauth
    req = SimpleNamespace(headers={"host": "localhost:7013"}, url=SimpleNamespace(scheme="http"))
    assert mcp_oauth.redirect_uri_for_request(req) == "http://localhost:7013/api/mcp/oauth/callback"


# ------------------------------------------------------------------ unofficial sign-ins

@pytest.fixture
def switch_off(monkeypatch):
    monkeypatch.delenv("WINGS_ALLOW_UNOFFICIAL_SIGNIN", raising=False)


def test_switch_defaults_off_and_names_the_setting(switch_off, monkeypatch):
    from wings import unofficial
    assert unofficial.allowed() is False
    msg = unofficial.provider_blocked("chatgpt-subscription")
    assert "WINGS_ALLOW_UNOFFICIAL_SIGNIN=1" in msg and "Codex" in msg
    assert "Visual Studio Code" in unofficial.provider_blocked("copilot")
    assert unofficial.provider_blocked("openrouter") is None
    with pytest.raises(HTTPException) as e:
        unofficial.require_allowed("copilot")
    assert e.value.status_code == 403
    monkeypatch.setenv("WINGS_ALLOW_UNOFFICIAL_SIGNIN", "1")
    assert unofficial.provider_blocked("copilot") is None


def test_kimi_gets_no_borrowed_user_agent_unless_allowed(switch_off, monkeypatch):
    from src import llm_core
    from src.endpoint_resolver import build_headers
    kimi = "https://api.kimi.com/coding/v1"
    assert llm_core._kimi_code_disguise(kimi) is False
    assert "User-Agent" not in build_headers("k", kimi)
    assert llm_core.apply_kimi_code_headers({"Authorization": "Bearer k"}, kimi + "/chat/completions") == {"Authorization": "Bearer k"}
    assert llm_core._kimi_code_ua_candidates(kimi) == []
    monkeypatch.setenv("WINGS_ALLOW_UNOFFICIAL_SIGNIN", "1")
    assert llm_core._kimi_code_disguise(kimi) is True
    assert build_headers("k", kimi)["User-Agent"].startswith("claude-code/")


def test_switched_off_providers_answer_with_the_reason(switch_off):
    from src import llm_core
    for url in ("https://chatgpt.com/backend-api/codex", "https://api.githubcopilot.com"):
        with pytest.raises(HTTPException) as e:
            llm_core.llm_call(url, "m", [{"role": "user", "content": "hi"}])
        assert e.value.status_code == 403 and "WINGS_ALLOW_UNOFFICIAL_SIGNIN" in e.value.detail
        with pytest.raises(HTTPException):
            asyncio.run(llm_core.llm_call_async(url, "m", [{"role": "user", "content": "hi"}]))

        async def first_chunk():
            async for chunk in llm_core._stream_llm_inner(url, "m", [{"role": "user", "content": "hi"}]):
                return chunk
        chunk = asyncio.run(first_chunk())
        assert chunk.startswith("event: error\n")
        data = json.loads(chunk.split("data: ", 1)[1])
        assert data["status"] == 403 and "switched off" in data["text"]


def test_switched_off_sign_ins_make_no_requests(switch_off, monkeypatch):
    import httpx
    from src import chatgpt_subscription, copilot

    def no_network(*a, **k):
        raise AssertionError("no request may be made while switched off")

    monkeypatch.setattr(httpx, "post", no_network)
    monkeypatch.setattr(httpx, "get", no_network)
    with pytest.raises(chatgpt_subscription.ChatGPTSubscriptionError):
        chatgpt_subscription.request_device_code()
    with pytest.raises(chatgpt_subscription.ChatGPTSubscriptionError):
        chatgpt_subscription.refresh_oauth_tokens("a", "r")
    assert chatgpt_subscription.fetch_available_models("tok") == []
    with pytest.raises(chatgpt_subscription.ChatGPTUsageUnavailable):
        chatgpt_subscription.fetch_usage_payload("tok")
    with pytest.raises(HTTPException):
        copilot.request_device_code()
    with pytest.raises(HTTPException):
        copilot.poll_access_token("github.com", "dc")
    assert copilot.fetch_models("https://api.githubcopilot.com", "tok") == []


def test_stored_chatgpt_tokens_are_not_refreshed_while_off(switch_off, monkeypatch):
    from src import endpoint_resolver
    import src.chatgpt_subscription as cs
    monkeypatch.setattr(cs, "resolve_runtime_credentials",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not refresh")))
    ep = SimpleNamespace(base_url="https://chatgpt.com/backend-api/codex", api_key=None, provider_auth_id="auth1")
    base, key = endpoint_resolver.resolve_endpoint_runtime(ep, owner="admin")
    assert base.startswith("https://chatgpt.com/backend-api/codex") and key is None


def test_device_flow_start_is_refused_with_the_reason(switch_off):
    from routes import copilot_routes, chatgpt_subscription_routes
    req = SimpleNamespace(state=SimpleNamespace(current_user="admin", api_token=False), headers={})
    for mod in (copilot_routes, chatgpt_subscription_routes):
        with pytest.raises(HTTPException) as e:
            mod._start_device_flow(req, {})
        assert e.value.status_code == 403 and "WINGS_ALLOW_UNOFFICIAL_SIGNIN" in e.value.detail


def test_failed_http_connect_reports_instead_of_cancelling_the_request():
    """Upstream: when discovery fails inside the SDK transport, its task group
    cancels the connect task and task.result() re-raised that cancellation into
    the web request (a bare HTTP 500). Now it is reported as an error state."""
    from src import mcp_manager
    mgr = mcp_manager.McpManager.__new__(mcp_manager.McpManager)
    mgr._connections = {}
    mgr._connect_tasks = {}

    async def failing(server_id, name, url):
        raise asyncio.CancelledError()

    mgr._connect_http = failing

    async def run():
        return await mgr._start_http_connect("s1", "Jira", "https://mcp.example.test/mcp", wait=2)

    assert asyncio.run(run()) is False
    assert mgr._connections["s1"]["status"] == "error"
    assert "sign-in service" in mgr._connections["s1"]["error"]


def test_abandoned_sign_in_shows_an_error_not_a_stale_wait(monkeypatch):
    from src import mcp_manager, mcp_oauth
    import mcp.client.streamable_http as sh

    class _Cancelled:
        async def __aenter__(self):
            raise asyncio.CancelledError()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(mcp_oauth, "build_provider", lambda *a, **k: object())
    monkeypatch.setattr(sh, "streamablehttp_client", lambda *a, **k: _Cancelled())
    mgr = mcp_manager.McpManager.__new__(mcp_manager.McpManager)
    mgr._connections = {}
    mgr._connect_tasks = {}

    async def run():
        t = asyncio.create_task(mgr._connect_http("s2", "Notion", "https://mcp.example.test/mcp"))
        mgr._connect_tasks["s2"] = t
        with pytest.raises(asyncio.CancelledError):
            await t
        # A task disconnect_server already forgot leaves no status behind.
        t2 = asyncio.create_task(mgr._connect_http("s3", "Linear", "https://mcp.example.test/mcp"))
        with pytest.raises(asyncio.CancelledError):
            await t2

    asyncio.run(run())
    assert mgr._connections["s2"]["status"] == "error"
    assert "Reconnect" in mgr._connections["s2"]["error"]
    assert "s3" not in mgr._connections


# ------------------------------------------------------------------ review fixes: helpers

class _Recorder:
    """Stands in for every way httpx can send a request; records and refuses."""

    def __init__(self):
        self.calls = []

    def install(self, monkeypatch):
        import httpx
        rec = self

        def _sync(method):
            def fn(url, *a, **k):
                rec.calls.append((method, str(url), dict(k.get("headers") or {})))
                raise httpx.ConnectError("blocked by test")
            return fn

        for name in ("get", "post", "put", "delete", "request", "stream"):
            monkeypatch.setattr(httpx, name, _sync(name))

        def client_send(self_, request, *a, **k):
            rec.calls.append(("send", str(request.url), dict(request.headers)))
            raise httpx.ConnectError("blocked by test")

        async def async_send(self_, request, *a, **k):
            rec.calls.append(("send", str(request.url), dict(request.headers)))
            raise httpx.ConnectError("blocked by test")

        monkeypatch.setattr(httpx.Client, "send", client_send)
        monkeypatch.setattr(httpx.AsyncClient, "send", async_send)
        return self

    def to(self, host):
        return [c for c in self.calls if host in c[1]]


def _mem_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from core.database import Base
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False)


COPILOT = "https://api.githubcopilot.com"
CODEX = "https://chatgpt.com/backend-api/codex"


# ------------------------------------------------------------------ switch off: nothing goes out

def test_switched_off_copilot_and_codex_send_no_request_from_any_helper(switch_off, monkeypatch):
    """Security review: the model-list, probe and ping helpers built their own
    requests with build_headers() -> copilot_headers(), presenting OpenWings
    as VS Code with the stored token, while the sign-in was switched off."""
    from routes import model_routes as mr
    from src import copilot, chatgpt_subscription, endpoint_resolver, llm_core
    rec = _Recorder().install(monkeypatch)
    ep = SimpleNamespace(id="cp1", base_url=COPILOT, api_key="gho_stored_token", provider_auth_id=None, owner=None)

    assert endpoint_resolver.resolve_endpoint_runtime(ep) == (COPILOT, None)
    assert mr._resolve_probe_key(ep) is None
    assert endpoint_resolver.build_headers("gho_stored_token", COPILOT) == {}
    assert copilot.copilot_headers("gho_stored_token") == {}
    assert chatgpt_subscription.chatgpt_headers("tok") == {}
    for base in (COPILOT, CODEX):
        assert mr._probe_endpoint(base, "gho_stored_token") == []
        single = mr._probe_single_model(base, "gho_stored_token", "gpt-4o")
        assert single.get("skipped") is True
        ping = mr._ping_endpoint(base, "gho_stored_token")
        assert ping["reachable"] is False and "switched off" in ping["error"]
    monkeypatch.setattr(llm_core, "_configured_cached_model_ids", lambda *a, **k: [])
    assert llm_core.list_model_ids(COPILOT + "/chat/completions", headers={"Authorization": "Bearer gho_x"}) == []
    assert rec.calls == []

    # The same helper does send once the owner opts in, so the test above
    # is not passing for some unrelated reason.
    monkeypatch.setenv("WINGS_ALLOW_UNOFFICIAL_SIGNIN", "1")
    mr._probe_endpoint(COPILOT, "gho_stored_token")
    sent = rec.to("githubcopilot.com")
    assert sent and sent[0][2].get("Copilot-Integration-Id") == "vscode-chat"


@pytest.fixture
def model_app(monkeypatch, switch_off):
    """The real model routes on an in-memory database holding a Copilot
    endpoint (token in api_key, auto refresh, as copilot_routes saves it)."""
    from core.database import ModelEndpoint
    from routes import model_routes as mr
    Session = _mem_db()
    monkeypatch.setattr(mr, "SessionLocal", Session)
    monkeypatch.setattr(mr, "_disable_stale_cookbook_local_endpoints", lambda db: 0)
    db = Session()
    db.add(ModelEndpoint(id="cp1", name="GitHub Copilot", base_url=COPILOT, api_key="gho_stored_token",
                         is_enabled=True, cached_models=json.dumps(["gpt-4o"]), model_refresh_mode="auto",
                         endpoint_kind="api"))
    db.commit()
    db.close()
    monkeypatch.setenv("AUTH_ENABLED", "true")
    app = FastAPI()
    app.state.auth_manager = _Auth()
    app.add_middleware(_FakeSession)
    app.include_router(mr.setup_model_routes(SimpleNamespace(get_providers=lambda: [], discover_models=lambda: [])))
    rec = _Recorder().install(monkeypatch)
    return SimpleNamespace(client=TestClient(app), rec=rec, Session=Session, mr=mr)


def test_switched_off_copilot_endpoint_is_never_called_by_the_routes(model_app):
    c, hdr = model_app.client, {"x-test-user": "admin"}
    # Background refresh (forced) of every endpoint.
    r = c.get("/api/models", params={"refresh": "true", "wait_refresh": "true"}, headers=hdr)
    assert r.status_code == 200
    # Manual refresh of the endpoint's list keeps what it had.
    r = c.get("/api/model-endpoints/cp1/models", params={"refresh": "true"}, headers=hdr)
    assert r.status_code == 200 and r.headers.get("X-Model-Refresh-Status") == "failed"
    # Ping lists it with the reason.
    r = c.get("/api/ping", headers=hdr)
    entry = [e for e in r.json()["endpoints"] if e["id"] == "cp1"][0]
    assert "switched off" in entry["error"]
    # Probing every model ("Say OK" completions) is refused.
    r = c.get("/api/model-endpoints/cp1/probe", headers=hdr)
    assert r.status_code == 403 and "WINGS_ALLOW_UNOFFICIAL_SIGNIN" in r.json()["detail"]
    # New endpoints for these services, and tests of them, are refused.
    for base in (COPILOT, CODEX):
        r = c.post("/api/model-endpoints", data={"base_url": base, "api_key": "x"}, headers=hdr)
        assert r.status_code == 403 and "switched off" in r.json()["detail"]
        r = c.post("/api/model-endpoints/test", data={"base_url": base, "api_key": "x"}, headers=hdr)
        assert r.status_code == 403
    assert model_app.rec.calls == []
    # The stored data is kept.
    from core.database import ModelEndpoint
    db = model_app.Session()
    try:
        ep = db.query(ModelEndpoint).filter_by(id="cp1").one()
        assert json.loads(ep.cached_models) == ["gpt-4o"] and ep.api_key == "gho_stored_token"
    finally:
        db.close()


def test_agent_model_lookup_skips_a_switched_off_endpoint(switch_off, monkeypatch):
    """The agent's model tools (src/ai_interaction.py) probed GET /models on
    every endpoint, Copilot's included, with the VS Code identity."""
    import src.database as sdb
    from core.database import ModelEndpoint
    from src import ai_interaction
    rec = _Recorder().install(monkeypatch)
    Session = _mem_db()
    monkeypatch.setattr(sdb, "SessionLocal", Session)
    db = Session()
    db.add(ModelEndpoint(id="cp1", name="GitHub Copilot", base_url=COPILOT, api_key="gho_stored_token",
                         is_enabled=True, cached_models=json.dumps(["gpt-4o"]), model_type="llm"))
    db.commit()
    db.close()
    with pytest.raises(ValueError):
        ai_interaction._resolve_model("gpt-4o")
    assert rec.calls == []


# ------------------------------------------------------------------ MCP sign-ins: only a person's own browser

def _caller_request(user=None, token=False, internal=False):
    headers = {"X-Odysseus-Internal-Token": "x"} if internal else {}
    app = SimpleNamespace(state=SimpleNamespace(auth_manager=_Auth()))
    return SimpleNamespace(headers=headers, app=app,
                           state=SimpleNamespace(current_user=user, api_token=token))


def test_mcp_sign_in_counts_only_a_signed_in_person(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    from routes.mcp import mcp_routes
    assert mcp_routes._wings_caller(_caller_request("admin")) == ("admin", True)
    assert mcp_routes._wings_caller(_caller_request("bob")) == ("bob", False)
    # An admin API token, and the agent's in-process calls, are not a person.
    assert mcp_routes._wings_caller(_caller_request("admin", token=True)) == (None, False)
    assert mcp_routes._wings_caller(_caller_request("internal-tool", internal=True)) == (None, False)
    assert mcp_routes._wings_caller(None) == (None, False)
    monkeypatch.setenv("AUTH_ENABLED", "false")
    assert mcp_routes._wings_caller(_caller_request(None)) == (None, True)


@pytest.fixture
def mcp_app(monkeypatch, tmp_path):
    """routes/mcp/mcp_routes.py on an in-memory database, with a fresh router
    (the module keeps one) and a stand-in MCP manager."""
    from unittest.mock import AsyncMock, MagicMock
    monkeypatch.setenv("AUTH_ENABLED", "true")
    import routes.mcp.mcp_routes as mcp_routes
    mcp_routes = importlib.reload(mcp_routes)
    Session = _mem_db()
    monkeypatch.setattr(mcp_routes, "SessionLocal", Session)
    monkeypatch.setattr(mcp_routes, "_sanitize_mcp_oauth_config", lambda cfg: cfg)
    manager = MagicMock()
    manager.connect_server = AsyncMock(return_value=True)
    manager.disconnect_server = AsyncMock(return_value=None)
    manager.get_server_status = MagicMock(return_value={"status": "connected", "tool_count": 3})
    app = FastAPI()
    app.state.auth_manager = _Auth()
    app.add_middleware(_FakeSession)
    app.include_router(mcp_routes.setup_mcp_routes(manager))
    return SimpleNamespace(client=TestClient(app, base_url="http://localhost:7013"), routes=mcp_routes,
                           manager=manager, Session=Session, tmp=tmp_path)


def _google_server(ctx):
    from core.database import McpServer
    keys = ctx.tmp / "keys.json"
    keys.write_text(json.dumps({"installed": {"client_id": "cid.apps.googleusercontent.com", "client_secret": "csecret"}}))
    token_file = ctx.tmp / "tokens" / "token.json"
    db = ctx.Session()
    db.add(McpServer(id="g1", name="Google Workspace", transport="stdio", command="google-mcp", args="[]",
                     oauth_config=json.dumps({"provider": "google", "keys_file": str(keys),
                                              "token_file": str(token_file), "scopes": ["openid"]})))
    db.commit()
    db.close()
    return token_file


class _GoogleTokenClient:
    posts = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, data=None, **k):
        _GoogleTokenClient.posts.append((url, dict(data or {})))
        return SimpleNamespace(status_code=200, text="", json=lambda: {"access_token": "ya29", "refresh_token": "1//r"})


def test_google_mcp_sign_in_uses_a_random_state_pkce_and_its_owner(mcp_app, monkeypatch):
    """Security review: upstream used the server id as the state (static, no
    PKCE, no owner), so any admin opening the callback finished it."""
    token_file = _google_server(mcp_app)
    _GoogleTokenClient.posts = []
    monkeypatch.setattr(mcp_app.routes.httpx, "AsyncClient", _GoogleTokenClient)
    c = mcp_app.client
    r = c.get("/api/mcp/oauth/authorize/g1", headers={"x-test-user": "admin"}, follow_redirects=False)
    assert r.status_code in (302, 307)
    parts, q = _qs(r.headers["location"])
    assert parts.netloc == "accounts.google.com"
    state = q["state"]
    assert state != "g1" and len(state) >= 32
    assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43

    # The old static state no longer finishes anything.
    r = c.get("/api/mcp/oauth/callback", params={"code": "c1", "state": "g1"}, headers={"x-test-user": "admin"})
    assert r.status_code == 400 and _GoogleTokenClient.posts == []
    # Another admin cannot finish it; the attempt is kept for its owner.
    r = c.get("/api/mcp/oauth/callback", params={"code": "c1", "state": state}, headers={"x-test-user": "admin2"})
    assert r.status_code == 403 and _GoogleTokenClient.posts == []
    # Nor can an admin API token.
    r = c.get("/api/mcp/oauth/callback", params={"code": "c1", "state": state},
              headers={"x-test-user": "admin", "x-test-token": "1"})
    assert r.status_code == 403 and _GoogleTokenClient.posts == []
    # The person who started it can, once, and the verifier goes to Google.
    r = c.get("/api/mcp/oauth/callback", params={"code": "c1", "state": state}, headers={"x-test-user": "admin"})
    assert r.status_code == 200, r.text
    url, data = _GoogleTokenClient.posts[-1]
    assert url == "https://oauth2.googleapis.com/token"
    verifier = data["code_verifier"]
    assert base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode() == q["code_challenge"]
    assert json.loads(token_file.read_text())["access_token"] == "ya29"
    mcp_app.manager.connect_server.assert_awaited()
    r = c.get("/api/mcp/oauth/callback", params={"code": "c1", "state": state}, headers={"x-test-user": "admin"})
    assert r.status_code == 400 and len(_GoogleTokenClient.posts) == 1


def test_google_paste_back_needs_the_same_attempt_and_server(mcp_app, monkeypatch):
    _google_server(mcp_app)
    _GoogleTokenClient.posts = []
    monkeypatch.setattr(mcp_app.routes.httpx, "AsyncClient", _GoogleTokenClient)
    from wings import mcp_google
    state, _ = mcp_google.start("g1", "admin", "http://localhost:7000/api/mcp/oauth/callback")
    pasted = f"http://localhost:7000/api/mcp/oauth/callback?code=c2&state={state}"
    c = mcp_app.client
    r = c.post("/api/mcp/oauth/exchange/g1", data={"callback_url": "http://localhost:7000/api/mcp/oauth/callback?code=c2&state=g1"},
               headers={"x-test-user": "admin"})
    assert r.status_code == 400 and _GoogleTokenClient.posts == []
    r = c.post("/api/mcp/oauth/exchange/other", data={"callback_url": pasted}, headers={"x-test-user": "admin"})
    assert r.status_code == 400 and _GoogleTokenClient.posts == []
    r = c.post("/api/mcp/oauth/exchange/g1", data={"callback_url": pasted}, headers={"x-test-user": "admin"})
    assert r.status_code == 200 and _GoogleTokenClient.posts[-1][1]["code_verifier"]


def test_google_attempts_expire(monkeypatch):
    from wings import mcp_google
    state, _ = mcp_google.start("g1", "admin", "http://x/cb")
    mcp_google._flows[state].created -= mcp_google.FLOW_TTL_SECONDS + 1
    assert mcp_google.take(state, "admin", True) is None


def test_token_started_mcp_flow_has_no_owner_and_tokens_cannot_finish(mcp_app):
    from src import mcp_oauth
    from routes.mcp import mcp_routes
    # A connect started through an admin API token records no owner ...
    mcp_routes._wings_set_flow("s-tok", _caller_request("admin", token=True))
    assert mcp_oauth._flow_ctx.pop("s-tok")["owner"] is None

    async def mk():
        mcp_oauth.register_pending("st-app", owner=None)
    asyncio.run(mk())
    c = mcp_app.client
    # ... and neither a token nor a member can finish an ownerless flow.
    r = c.get("/api/mcp/oauth/callback", params={"code": "c", "state": "st-app"},
              headers={"x-test-user": "admin", "x-test-token": "1"})
    assert r.status_code == 403
    r = c.get("/api/mcp/oauth/callback", params={"code": "c", "state": "st-app"}, headers={"x-test-user": "bob"})
    assert r.status_code == 403
    assert not mcp_oauth._pending["st-app"].done()
    mcp_oauth._discard_pending("st-app")


def test_cancel_on_the_apps_page_ends_the_sign_in_at_once(mcp_app):
    """Functional review: Cancel on Linear's page showed "No authorization
    code found ... copied the full URL" and left the sign-in waiting 5 min."""
    from core.database import McpServer
    from src import mcp_oauth
    db = mcp_app.Session()
    db.add(McpServer(id="n1", name="Linear", transport="http", url="https://mcp.linear.app/mcp", is_enabled=True))
    db.commit()
    db.close()

    async def mk():
        mcp_oauth.register_pending("st-cancel", owner="admin")
    asyncio.run(mk())
    mcp_oauth._auth_urls["n1"] = "https://mcp.linear.app/authorize?client_id=x&state=st-cancel"
    c = mcp_app.client
    # Someone else's cancel changes nothing.
    r = c.get("/api/mcp/oauth/callback", params={"error": "access_denied", "state": "st-cancel"},
              headers={"x-test-user": "admin2"})
    assert r.status_code == 200 and "Sign-in cancelled" in r.text
    mcp_app.manager.disconnect_server.assert_not_awaited()
    # The person who started it: the waiting sign-in ends now.
    r = c.get("/api/mcp/oauth/callback", params={"error": "access_denied", "state": "st-cancel"},
              headers={"x-test-user": "admin"})
    assert r.status_code == 200
    assert "Sign-in cancelled" in r.text and "did not connect to Linear" in r.text
    assert "authorization code" not in r.text and "Odysseus" not in r.text
    mcp_app.manager.disconnect_server.assert_awaited_once_with("n1")
    db = mcp_app.Session()
    try:
        assert db.query(McpServer).filter_by(id="n1").one().is_enabled is False
    finally:
        db.close()
    mcp_oauth._discard_pending("st-cancel")
    mcp_oauth._auth_urls.pop("n1", None)


def test_server_list_says_whether_an_app_was_ever_signed_in(mcp_app):
    from core.database import McpServer
    db = mcp_app.Session()
    db.add(McpServer(id="a1", name="Notion", transport="http", url="https://mcp.notion.com/mcp",
                     oauth_tokens=json.dumps({"tokens": {"access_token": "secret-at"}})))
    db.add(McpServer(id="a2", name="Linear", transport="http", url="https://mcp.linear.app/mcp"))
    db.commit()
    db.close()
    mcp_app.manager.get_server_status = lambda sid: {"status": "disconnected"}
    rows = {s["id"]: s for s in mcp_app.client.get("/api/mcp/servers", headers={"x-test-user": "admin"}).json()}
    assert rows["a1"]["signed_in"] is True and rows["a2"]["signed_in"] is False
    assert "secret-at" not in json.dumps(rows)


# ------------------------------------------------------------------ first run counts only endpoints with models

def test_usable_endpoints_need_models_and_member_view(monkeypatch, switch_off):
    import core.database as cdb
    from core.database import ModelEndpoint
    import wings.connect_routes as cr
    Session = _mem_db()
    monkeypatch.setattr(cdb, "SessionLocal", Session)
    db = Session()
    db.add(ModelEndpoint(id="bad", name="Anthropic", base_url="https://api.anthropic.com", api_key="sk-ant-wrong",
                         is_enabled=True, cached_models=None))
    db.add(ModelEndpoint(id="cp", name="Copilot", base_url=COPILOT, api_key="gho", is_enabled=True,
                         cached_models=json.dumps(["gpt-4o"])))
    db.commit()
    assert cr._usable_endpoint_count() == 0   # a refused key and a switched-off sign-in
    db.add(ModelEndpoint(id="mine", name="OpenRouter", base_url="https://openrouter.ai/api/v1", api_key="k",
                         is_enabled=True, cached_models=json.dumps(["a/b"]), owner="admin"))
    db.commit()
    db.close()
    assert cr._usable_endpoint_count() == 1
    assert cr._usable_endpoint_count("bob", admin=False) == 0   # admin's own endpoint
    assert cr._usable_endpoint_count("admin", admin=False) == 1

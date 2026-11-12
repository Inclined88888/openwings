"""Unit tests for Connect your accounts (patch 0025): the shared plumbing in
wings/accounts.py (install-wide client store, per-person tokens with refresh
and revoke, sign-in flows bound to a person, the origin bounce, assistant
tools), mail with an app password, calendar by link, the own Google key
(sign-in, callback, CalDAV bearer, Meet, Drive) and XOAUTH2 for the
assistant's mail tools.

Every outside service is stubbed: no network.
Run with ./tests/unit/run.sh (inside the built image, network off)."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import imaplib
import json
import smtplib
import socket
import sys
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

sys.path.insert(0, "/app")

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402


# ------------------------------------------------------------------ helpers

class _Resp:
    def __init__(self, status, body=None, raw=b""):
        self.status_code = status
        self._body = body
        self._raw = raw

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    def iter_bytes(self):
        yield self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Auth:
    is_configured = True
    admins = {"admin"}
    users = {"admin": {}, "pat": {}, "sam": {}}

    def is_admin(self, user):
        return user in self.admins


class _FakeSession(BaseHTTPMiddleware):
    """X-Test-User is the signed-in person; X-Test-Token marks an API token."""

    async def dispatch(self, request, call_next):
        request.state.current_user = request.headers.get("x-test-user")
        request.state.api_token = request.headers.get("x-test-token") == "1"
        return await call_next(request)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("ALLOWED_ORIGINS", "http://localhost:7013,http://127.0.0.1:7013")
    monkeypatch.setenv("OAUTH_REDIRECT_BASE_URL", "http://127.0.0.1:7013")
    for k in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REDIRECT_URI",
              "WINGS_TEST_ENDPOINTS", "WINGS_TEST_ALLOW_HOSTS", "WINGS_TEST_MODE", "ODYSSEUS_ALLOW_PRIVATE_CALDAV",
              "APP_PUBLIC_URL"):
        monkeypatch.delenv(k, raising=False)
    import core.database as cdb
    import src.secret_storage as ss
    from cryptography.fernet import Fernet
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    eng = create_engine(f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(cdb, "engine", eng)
    monkeypatch.setattr(cdb, "SessionLocal", sessionmaker(bind=eng, autoflush=False, autocommit=False))
    monkeypatch.setattr(ss, "_fernet", Fernet(Fernet.generate_key()))
    cdb.Base.metadata.create_all(eng, tables=[cdb.EmailAccount.__table__, cdb.CalendarCal.__table__,
                                              cdb.CalendarEvent.__table__])
    prefs = {}
    import routes.prefs_routes as pr
    monkeypatch.setattr(pr, "_load_for_user", lambda user=None: json.loads(json.dumps(prefs.get(user, {}))))
    monkeypatch.setattr(pr, "_save_for_user", lambda user, p: prefs.__setitem__(user, json.loads(json.dumps(p))))
    import wings.accounts as A
    import wings.accounts_google as G
    A._flows.clear()
    yield SimpleNamespace(A=A, G=G, cdb=cdb, prefs=prefs, tmp=tmp_path)
    A._flows.clear()


def _req(host="127.0.0.1:7013", scheme="http", path="/x", query="", user=None):
    url = SimpleNamespace(scheme=scheme, path=path, query=query)
    return SimpleNamespace(headers={"host": host}, url=url, state=SimpleNamespace(current_user=user, api_token=False),
                           query_params=dict(parse_qs(query) and {k: v[0] for k, v in parse_qs(query).items()}))


# ------------------------------------------------------------------ plumbing

def test_test_url_and_mail_target_only_follow_the_test_map(env, monkeypatch):
    A = env.A
    assert A.test_url("https://oauth2.googleapis.com/token") == "https://oauth2.googleapis.com/token"
    monkeypatch.setenv("WINGS_TEST_ENDPOINTS", json.dumps({
        "https://oauth2.googleapis.com": "http://stub:9100/oauth2", "imap.gmail.com:993": "stub:1143:none"}))
    monkeypatch.setenv("WINGS_TEST_ALLOW_HOSTS", "stub")
    # Set without WINGS_TEST_MODE=1 (a stray variable in a real install): ignored.
    assert A.test_url("https://oauth2.googleapis.com/token") == "https://oauth2.googleapis.com/token"
    assert A.mail_target("imap.gmail.com", 993, "ssl") == ("imap.gmail.com", 993, "ssl")
    assert A.test_hosts_allowed() == set()
    assert not A.test_hooks_active()
    monkeypatch.setenv("WINGS_TEST_MODE", "1")
    assert A.test_hooks_active()
    assert A.test_hosts_allowed() == {"stub"}
    assert A.test_url("https://oauth2.googleapis.com/token") == "http://stub:9100/oauth2/token"
    assert A.mail_target("imap.gmail.com", 993, "ssl") == ("stub", 1143, "none")
    assert A.mail_target("smtp.gmail.com", 465, "ssl") == ("smtp.gmail.com", 465, "ssl")


def test_client_store_is_encrypted_masked_and_prefers_saved_over_env(env, monkeypatch):
    A = env.A
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "envid-123456.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "env-secret")
    c = A.get_client("google")
    assert c.source == "env" and c.client_secret == "env-secret"
    A.save_client("google", "1234567890-abc.apps.googleusercontent.com", "GOCSPX-topsecret",
                  "http://127.0.0.1:7013/api/email/oauth/google/callback", by="admin")
    c = A.get_client("google")
    assert c.source == "stored" and c.client_secret == "GOCSPX-topsecret"
    raw = env.cdb.engine.connect().exec_driver_sql("SELECT client_secret FROM wings_oauth_clients").fetchone()[0]
    assert "topsecret" not in raw
    st = A.client_status("google")
    assert "topsecret" not in json.dumps(st) and st["client_id"].startswith("••••")
    assert A.delete_client("google") is True
    assert A.get_client("google").source == "env"


@pytest.mark.parametrize("cid,secret,code", [
    ("bad id with spaces", "x", "bad_client_id"),
    ("1234567890-abc.apps.googleusercontent.com", "", "no_secret"),
    ("1234567890-abc.apps.googleusercontent.com", "has space", "bad_secret"),
])
def test_client_store_refuses_bad_input(env, cid, secret, code):
    with pytest.raises(env.A.AccountError) as e:
        env.A.save_client("google", cid, secret, None, by="admin")
    assert e.value.code == code


def test_flows_are_single_use_bound_to_the_person_and_expire(env):
    A = env.A
    f = A.start_flow("pat", "google", "connect", ["openid"], redirect_uri="http://127.0.0.1:7013/cb",
                     origin="http://127.0.0.1:7013")
    with pytest.raises(A.AccountError) as e:
        A.take_flow(f.state, "sam")
    assert e.value.code == "other_user"
    assert A.take_flow(f.state, "pat").owner == "pat"
    with pytest.raises(A.AccountError) as e:
        A.take_flow(f.state, "pat")
    assert e.value.code == "expired"
    g = A.start_flow("pat", "google", "connect", ["openid"], redirect_uri="x", origin="y")
    g.created -= A.FLOW_TTL_SECONDS + 1
    with pytest.raises(A.AccountError):
        A.take_flow(g.state, "pat")


def test_authorize_url_has_pkce_state_and_googles_parameters(env):
    A, G = env.A, env.G
    f = A.start_flow("pat", "google", "connect", ["openid", "email", G.SCOPE_DRIVE],
                     redirect_uri="http://127.0.0.1:7013/api/email/oauth/google/callback", origin="http://127.0.0.1:7013")
    url = A.authorize_url(f, A.Client("cid-123456", "sec", None, "stored"))
    parts = urlsplit(url)
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == G.AUTHORIZE_URL
    assert q["code_challenge_method"] == "S256"
    assert q["code_challenge"] == base64.urlsafe_b64encode(hashlib.sha256(f.verifier.encode()).digest()).rstrip(b"=").decode()
    assert q["state"] == f.state and q["response_type"] == "code"
    assert q["access_type"] == "offline" and q["include_granted_scopes"] == "true"
    assert q["scope"] == f"openid email {G.SCOPE_DRIVE}"
    assert "client_secret" not in q


def test_bounce_goes_only_to_the_stored_trusted_origin(env):
    A = env.A
    f = A.start_flow("pat", "google", "connect", ["openid"], redirect_uri="x", origin="http://127.0.0.1:7013")
    r = A.bounce(_req(host="localhost:7013", path="/api/email/oauth/google/callback", query=f"code=c&state={f.state}"), f)
    assert r.status_code == 303
    assert r.headers["location"] == f"http://127.0.0.1:7013/api/email/oauth/google/callback?code=c&state={f.state}"
    assert A.bounce(_req(host="127.0.0.1:7013"), f) is None            # already there
    f2 = A.start_flow("pat", "google", "connect", ["openid"], redirect_uri="x", origin="http://evil.example")
    assert A.bounce(_req(host="localhost:7013"), f2) is None             # not a trusted origin
    # The middleware hook bounces only provider callbacks with a known state.
    assert A.bounce_unauthenticated(_req(host="localhost:7013", path="/api/other", query=f"state={f.state}")) is None
    hit = A.bounce_unauthenticated(_req(host="localhost:7013", path="/api/email/oauth/google/callback",
                                        query=f"code=c&state={f.state}"))
    assert hit is not None and hit.headers["location"].startswith("http://127.0.0.1:7013/")
    assert A.bounce_unauthenticated(_req(host="localhost:7013", path="/api/email/oauth/google/callback",
                                         query="code=c&state=unknown")) is None


@pytest.mark.parametrize("uri,ok", [
    ("http://127.0.0.1:7011/api/email/oauth/google/callback", True),
    ("http://localhost:7011/api/email/oauth/google/callback", True),
    ("https://wings.example.com/api/email/oauth/google/callback", True),
    ("http://192.168.1.5:7011/api/email/oauth/google/callback", False),
    ("http://wings.lan:7011/api/email/oauth/google/callback", False),
    ("https://user:pw@wings.example.com/cb", False),
    ("https://wings.example.com/cb#frag", False),
    ("https://wings.example.com/a/../cb", False),
])
def test_redirect_uri_rules_follow_googles_docs(env, uri, ok):
    assert env.A.safe_redirect_uri(uri) is ok


def test_tokens_refresh_near_expiry_and_only_for_their_owner(env, monkeypatch):
    A = env.A
    A.save_client("google", "cid-1234567", "sec", None, by="admin")
    link = A.save_link("pat", "google", "Pat@Gmail.com", {"access_token": "a1", "refresh_token": "r1", "expires_in": 3600},
                       scopes=["openid", "email"])
    assert A.access_token(link, "pat") == "a1"
    with pytest.raises(A.AccountError) as e:
        A.access_token(link, "admin")
    assert e.value.code == "not_yours"
    calls = []

    def post(url, data, **k):
        calls.append((url, dict(data)))
        return _Resp(200, {"access_token": "a2", "expires_in": 3600})
    assert A.access_token(link, "pat", post=post, now=time.time() + 4000) == "a2"
    assert calls[0][1]["grant_type"] == "refresh_token" and calls[0][1]["refresh_token"] == "r1"
    assert calls[0][1]["client_secret"] == "sec"
    # Scopes add up on a later sign-in; the old refresh token stays when none comes back.
    A.save_link("pat", "google", "pat@gmail.com", {"access_token": "a3", "expires_in": 3600},
                scopes=["https://www.googleapis.com/auth/drive.readonly"])
    l = A.get_link("pat", link)
    assert "openid" in l["scopes"] and "https://www.googleapis.com/auth/drive.readonly" in l["scopes"]
    raw = env.cdb.engine.connect().exec_driver_sql("SELECT access_token, refresh_token FROM wings_account_links").fetchone()
    assert "a3" not in raw[0] and "r1" not in raw[1]


def test_a_refused_refresh_asks_for_a_new_sign_in(env):
    A = env.A
    A.save_client("google", "cid-1234567", "sec", None, by="admin")
    link = A.save_link("pat", "google", "pat@gmail.com", {"access_token": "a1", "refresh_token": "r1", "expires_in": 1})
    with pytest.raises(A.NeedsSignIn):
        A.access_token(link, "pat", post=lambda *a, **k: _Resp(400, {"error": "invalid_grant"}), now=time.time() + 10)
    assert A.get_link("pat", link)["needs_signin"] is True
    with pytest.raises(A.NeedsSignIn):
        A.access_token(link, "pat")


def test_remove_link_revokes_then_forgets(env):
    A = env.A
    link = A.save_link("pat", "google", "pat@gmail.com", {"access_token": "a1", "refresh_token": "r1", "expires_in": 3600})
    seen = []
    assert A.remove_link("sam", link) is False
    assert A.remove_link("pat", link, post=lambda url, data, **k: seen.append((url, data)) or _Resp(200, {}))
    assert seen == [(env.G.REVOKE_URL, {"token": "r1"})]
    assert A.links_for("pat") == []


def test_assistant_tools_join_upstreams_registries_and_act_for_the_chat_owner(env, monkeypatch):
    A = env.A
    from src import tool_schemas, turn_contract
    import src.agent_tools as agent_tools
    seen = []

    async def handler(args, owner):
        seen.append((args, owner))
        return {"response": "ok", "exit_code": 0}
    tool = A.AccountTool(name="wings_test_tool", family="wings_test_family", family_label="Test",
                         words=r"\bzebra files\b", description="test", parameters={"type": "object", "properties": {}},
                         handler=handler)
    monkeypatch.setitem(A.TOOLS, tool.name, tool)
    A._install_one(tool)
    try:
        assert any(s["function"]["name"] == "wings_test_tool" for s in tool_schemas.FUNCTION_TOOL_SCHEMAS)
        assert "wings_test_tool" in turn_contract.FAMILY_TOOLS["wings_test_family"]
        assert "wings_test_family" in turn_contract.requested_capabilities("open my zebra files please")
        run = agent_tools.TOOL_HANDLERS["wings_test_tool"]
        assert asyncio.run(run('{"q": 1}', {"owner": "pat"}))["response"] == "ok"
        assert seen == [({"q": 1}, "pat")]
        assert asyncio.run(run("{}", {"owner": ""}))["exit_code"] == 1
        assert asyncio.run(run("[1]", {"owner": "pat"}))["exit_code"] == 1
    finally:
        tool_schemas.FUNCTION_TOOL_SCHEMAS[:] = [s for s in tool_schemas.FUNCTION_TOOL_SCHEMAS if s["function"]["name"] != "wings_test_tool"]
        agent_tools.TOOL_HANDLERS.pop("wings_test_tool", None)
        turn_contract.FAMILY_TOOLS.pop("wings_test_family", None)
        turn_contract._FAMILY_WORDS.pop("wings_test_family", None)


def test_drive_tools_are_registered_for_drive_requests(env):
    A = env.A
    A.install_tools()
    from src import turn_contract
    from src.agent_runtime.authority import create_request_authority
    assert {"google_drive_search", "google_drive_read"} <= turn_contract.FAMILY_TOOLS["cloud_files"]
    grants = {g.tool for g in create_request_authority("find the budget sheet in my google drive", owner="pat").grants}
    assert {"google_drive_search", "google_drive_read"} <= grants
    assert "cloud_files" not in turn_contract.requested_capabilities("drive to the shop and buy milk")


# ------------------------------------------------------------------ app passwords

class _FakeIMAP:
    accept = {"pat@gmail.com": "abcdefghijklmnop"}
    error = "[AUTHENTICATIONFAILED] Invalid credentials (Failure)"
    logins = []

    def __init__(self, host, port):
        self.host, self.port = host, port

    def login(self, user, password):
        _FakeIMAP.logins.append((self.host, user))
        if self.accept.get(user) != password:
            raise imaplib.IMAP4.error(self.error)
        return ("OK", [b"done"])

    def logout(self):
        pass


class _FakeSMTP:
    accept = {"pat@gmail.com": "abcdefghijklmnop"}
    made = []

    def __init__(self, host, port, timeout=None, block_private=None, **k):
        self.host, self.port = host, port
        _FakeSMTP.made.append((type(self).__name__, host, port))

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        if self.accept.get(user) != password:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    def quit(self):
        pass


@pytest.fixture
def mail(env, monkeypatch):
    import routes.email.email_helpers as eh
    _FakeIMAP.logins = []
    _FakeSMTP.made = []
    monkeypatch.setattr(eh, "_open_imap_connection", lambda host, port, **k: _FakeIMAP(host, port))
    monkeypatch.setattr(eh, "_PolicySMTP_SSL", type("_SSL", (_FakeSMTP,), {}))
    monkeypatch.setattr(eh, "_PolicySMTP", type("_PLAIN", (_FakeSMTP,), {}))
    monkeypatch.setattr(eh, "_mail_private_blocked", lambda owner: False)
    import wings.accounts_mail as M
    monkeypatch.setattr(M, "start_sync", lambda owner: None)
    return M


def test_app_password_login_tests_imap_and_smtp_and_strips_spaces(mail):
    out = mail.test_login("gmail", "pat@gmail.com", "abcd efgh ijkl mnop", "pat")
    assert out["imap_host"] == "imap.gmail.com" and out["imap_port"] == 993 and out["imap_starttls"] is False
    assert out["smtp_host"] == "smtp.gmail.com" and out["smtp_port"] == 465 and out["smtp_security"] == "ssl"
    assert out["imap_password"] == "abcdefghijklmnop"
    assert ("_SSL", "smtp.gmail.com", 465) in _FakeSMTP.made


def test_wrong_app_password_says_so_plainly(mail):
    with pytest.raises(mail.LoginFailed) as e:
        mail.test_login("gmail", "pat@gmail.com", "wrong", "pat")
    assert e.value.code == "wrong_password"
    assert "did not accept that password" in e.value.message and "app password" in e.value.message


def test_normal_google_password_gets_the_app_password_hint(mail, monkeypatch):
    monkeypatch.setattr(_FakeIMAP, "error", "[ALERT] Application-specific password required: https://support.google.com/accounts/answer/185833")
    with pytest.raises(mail.LoginFailed) as e:
        mail.test_login("gmail", "pat@gmail.com", "mynormalpassword", "pat")
    assert "wants an app password" in e.value.message


def test_icloud_tries_the_name_before_the_at_when_the_address_fails(mail, monkeypatch):
    monkeypatch.setattr(_FakeIMAP, "accept", {"pat": "xxxx-yyyy-zzzz-wwww"})
    monkeypatch.setattr(_FakeSMTP, "accept", {"pat@icloud.com": "xxxx-yyyy-zzzz-wwww"})
    out = mail.test_login("icloud", "pat@icloud.com", "xxxx-yyyy-zzzz-wwww", "pat")
    assert out["imap_user"] == "pat" and out["smtp_user"] == "pat@icloud.com"
    assert out["smtp_port"] == 587 and out["smtp_security"] == "starttls"
    assert [u for _h, u in _FakeIMAP.logins] == ["pat@icloud.com", "pat"]


@pytest.mark.parametrize("addr", ["", "pat", "pat@", "a b@c.com"])
def test_bad_address_is_refused_before_any_login(mail, addr):
    with pytest.raises(mail.AccountError) as e:
        mail.test_login("gmail", addr, "x", "pat")
    assert e.value.code == "bad_address"
    assert _FakeIMAP.logins == []


def _fake_email_app(env, monkeypatch, calls):
    """A request whose app has upstream's account routes (stand-ins that
    record the call and write the row, like upstream's)."""
    from core.database import EmailAccount, SessionLocal
    import uuid

    async def create(data, owner, request=None):
        calls.append(("create", owner, dict(data)))
        db = SessionLocal()
        try:
            row = EmailAccount(id=uuid.uuid4().hex, owner=owner, name=data["name"], imap_host=data.get("imap_host", ""),
                               imap_port=data.get("imap_port", 993), imap_user=data.get("imap_user", ""),
                               smtp_host=data.get("smtp_host", ""), smtp_port=data.get("smtp_port", 465),
                               smtp_user=data.get("smtp_user", ""), from_address=data.get("from_address", ""),
                               imap_password="", smtp_password="")
            db.add(row)
            db.commit()
            return {"ok": True, "id": row.id}
        finally:
            db.close()

    async def update(account_id, data, owner, request=None):
        calls.append(("update", owner, account_id))
        return {"ok": True, "id": account_id}

    async def delete(account_id, owner):
        calls.append(("delete", owner, account_id))
        db = SessionLocal()
        try:
            db.query(EmailAccount).filter(EmailAccount.id == account_id).delete()
            db.commit()
        finally:
            db.close()
        return {"ok": True}

    routes = [SimpleNamespace(path="/api/email/accounts", methods={"POST"}, endpoint=create),
              SimpleNamespace(path="/api/email/accounts/{account_id}", methods={"PUT"}, endpoint=update),
              SimpleNamespace(path="/api/email/accounts/{account_id}", methods={"DELETE"}, endpoint=delete)]
    return SimpleNamespace(app=SimpleNamespace(router=SimpleNamespace(routes=routes)), headers={"host": "127.0.0.1:7013"},
                           url=SimpleNamespace(scheme="http", path="/", query=""), state=SimpleNamespace(current_user="pat", api_token=False))


def test_connect_creates_the_account_with_upstreams_route_and_icloud_calendar(mail, env, monkeypatch):
    monkeypatch.setattr(_FakeIMAP, "accept", {"pat@icloud.com": "pw-1"})
    monkeypatch.setattr(_FakeSMTP, "accept", {"pat@icloud.com": "pw-1"})
    import src.caldav_sync as cs
    monkeypatch.setattr(cs, "validate_caldav_url", lambda u: u.rstrip("/"))
    calls = []
    req = _fake_email_app(env, monkeypatch, calls)
    out = asyncio.run(mail.connect(req, "pat", "icloud", "pat@icloud.com", "pw-1", calendar=True))
    assert out["ok"] and calls[0][0] == "create" and calls[0][1] == "pat"
    assert calls[0][2]["imap_password"] == "pw-1" and calls[0][2]["imap_host"] == "imap.mail.me.com"
    accs = env.prefs["pat"]["caldav_accounts"]
    assert accs[0]["url"] == "https://caldav.icloud.com" and accs[0]["username"] == "pat@icloud.com"
    from src.secret_storage import decrypt
    assert accs[0]["password"] != "pw-1" and decrypt(accs[0]["password"]) == "pw-1"
    # Connecting the same address again updates, never duplicates.
    asyncio.run(mail.connect(req, "pat", "icloud", "pat@icloud.com", "pw-1"))
    assert [c[0] for c in calls] == ["create", "update"]


def test_a_failed_login_saves_nothing(mail, env, monkeypatch):
    calls = []
    req = _fake_email_app(env, monkeypatch, calls)
    with pytest.raises(mail.LoginFailed):
        asyncio.run(mail.connect(req, "pat", "gmail", "pat@gmail.com", "nope"))
    assert calls == []


# ------------------------------------------------------------------ calendar by link

# Dates a month ahead, so the feed is inside the window OpenWings keeps.
_D0 = (datetime.now(timezone.utc) + timedelta(days=30)).date()


def _d(days, fmt="%Y%m%d"):
    return (_D0 + timedelta(days=days)).strftime(fmt)


ICS = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
X-WR-CALNAME:Family
BEGIN:VEVENT
UID:one@test
DTSTART:{_d(0)}T100000Z
DTEND:{_d(0)}T110000Z
SUMMARY:Dentist
LOCATION:Main St
END:VEVENT
BEGIN:VEVENT
UID:weekly@test
DTSTART:{_d(1)}T090000Z
DURATION:PT30M
RRULE:FREQ=WEEKLY;COUNT=5
EXDATE:{_d(8)}T090000Z
SUMMARY:Standup
END:VEVENT
BEGIN:VEVENT
UID:weekly@test
RECURRENCE-ID:{_d(15)}T090000Z
DTSTART:{_d(15)}T100000Z
DTEND:{_d(15)}T103000Z
SUMMARY:Standup (moved)
END:VEVENT
BEGIN:VEVENT
UID:gone@test
DTSTART:{_d(2)}T090000Z
DTEND:{_d(2)}T100000Z
STATUS:CANCELLED
SUMMARY:Cancelled thing
END:VEVENT
BEGIN:VEVENT
UID:allday@test
DTSTART;VALUE=DATE:{_d(23)}
SUMMARY:Holiday
END:VEVENT
END:VCALENDAR
""".encode()


def test_feed_parsing_expands_series_hides_moved_and_cancelled(env):
    from wings import ics_feeds as F
    name, events = F.parse_events(ICS, "feed1")
    assert name == "Family"
    names = sorted(e["summary"] for e in events)
    # 5 weekly, one deleted (EXDATE), one moved: 3 + the moved one.
    assert names == ["Dentist", "Holiday", "Standup", "Standup", "Standup", "Standup (moved)"]
    standups = sorted(e["dtstart"] for e in events if e["summary"] == "Standup")
    assert [d.strftime("%Y%m%d") for d in standups] == [_d(1), _d(22), _d(29)]
    assert all(e["rrule"] == "" and e["exdates"] == [] for e in events)
    by = {e["summary"]: e for e in events}
    assert by["Standup"]["dtend"] - by["Standup"]["dtstart"] == timedelta(minutes=30)
    assert by["Standup (moved)"]["dtstart"].strftime("%Y%m%dT%H%M") == f"{_d(15)}T1000"
    assert by["Holiday"]["all_day"] and by["Holiday"]["dtend"] - by["Holiday"]["dtstart"] == timedelta(days=1)
    assert by["Dentist"]["is_utc"] and all(e["uid"].startswith("ics-") for e in events)
    assert len({e["uid"] for e in events}) == len(events)
    # Another feed with the same event uids gets its own rows.
    _n, other = F.parse_events(ICS, "feed2")
    assert not ({e["uid"] for e in events} & {e["uid"] for e in other})
    # The same feed keeps the same uids on the next refresh.
    _n, again = F.parse_events(ICS, "feed1")
    assert {e["uid"] for e in again} == {e["uid"] for e in events}


BERLIN = b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:s1
SUMMARY:Weekly
DTSTART;TZID=Europe/Berlin:20261005T090000
DTEND;TZID=Europe/Berlin:20261005T100000
RRULE:FREQ=WEEKLY;COUNT=8
EXDATE;TZID=Europe/Berlin:20261012T090000
EXDATE;TZID=Europe/Berlin:20261109T090000
END:VEVENT
BEGIN:VEVENT
UID:s1
RECURRENCE-ID;TZID=Europe/Berlin:20261019T090000
DTSTART;TZID=Europe/Berlin:20261019T150000
DTEND;TZID=Europe/Berlin:20261019T160000
SUMMARY:MOVED
END:VEVENT
BEGIN:VEVENT
UID:s1
RECURRENCE-ID;TZID=Europe/Berlin:20261116T090000
DTSTART;TZID=Europe/Berlin:20261116T140000
DTEND;TZID=Europe/Berlin:20261116T150000
SUMMARY:MOVED
END:VEVENT
BEGIN:VEVENT
UID:s1
RECURRENCE-ID;TZID=Europe/Berlin:20261102T090000
DTSTART;TZID=Europe/Berlin:20261102T090000
DTEND;TZID=Europe/Berlin:20261102T100000
STATUS:CANCELLED
SUMMARY:Weekly
END:VEVENT
END:VCALENDAR
""".replace(b"\n", b"\r\n")


def test_feed_series_keep_their_local_time_across_daylight_saving(env):
    """Europe/Berlin weekly 09:00 across the October change: 07:00 UTC
    before, 08:00 UTC after; EXDATE and moved occurrences on both sides of
    the change; a cancelled occurrence (organiser cancelled it) is gone."""
    from wings import ics_feeds as F
    _n, events = F.parse_events(BERLIN, "f", now=datetime(2026, 10, 1, tzinfo=timezone.utc))
    got = sorted((e["dtstart"].strftime("%m-%dT%H:%M"), e["summary"]) for e in events)
    assert got == [
        ("10-05T07:00", "Weekly"),            # 09:00 CEST
        ("10-19T13:00", "MOVED"),             # moved before the change
        ("10-26T08:00", "Weekly"),            # 09:00 CET, after the change
        ("11-16T13:00", "MOVED"),             # moved after the change, shown once
        ("11-23T08:00", "Weekly"),
    ]
    assert all(e["is_utc"] for e in events)


def test_feed_fast_repeating_series_show_once(env):
    from wings import ics_feeds as F
    body = (b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:m\r\nDTSTART:" + _d(0).encode() + b"T000000Z\r\n"
            b"RRULE:FREQ=MINUTELY\r\nSUMMARY:Spam\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    _n, events = F.parse_events(body, "f")
    assert [e["summary"] for e in events] == ["Spam"]


@pytest.mark.parametrize("raw,out", [
    ("webcal://example.com/cal.ics", "https://example.com/cal.ics"),
    ("WEBCAL://example.com/a?b=1#x", "https://example.com/a?b=1"),
    ("https://calendar.google.com/calendar/ical/x%40gmail.com/private-abc/basic.ics",
     "https://calendar.google.com/calendar/ical/x%40gmail.com/private-abc/basic.ics"),
])
def test_feed_addresses_are_normalised(env, raw, out):
    from wings import ics_feeds as F
    assert F.normalize_url(raw) == out


@pytest.mark.parametrize("raw,code", [
    ("ftp://example.com/a.ics", "bad_scheme"),
    ("file:///etc/passwd", "bad_scheme"),
    ("https://user:pw@example.com/a.ics", "bad_url"),
    ("", "no_url"),
])
def test_feed_addresses_refused(env, raw, code):
    from wings import ics_feeds as F
    with pytest.raises(F.FeedError) as e:
        F.normalize_url(raw)
    assert e.value.code == code


def _resolver(table):
    def resolve(host, port, *a):
        ip = table[host]
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(fam, socket.SOCK_STREAM, 6, "", (ip, port))]
    return resolve


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.9", "169.254.169.254", "100.64.0.1", "::1", "0.0.0.0"])
def test_feed_hosts_in_private_space_are_refused(env, ip):
    from wings import ics_feeds as F
    with pytest.raises(F.FeedError) as e:
        F.resolve_checked("cal.example", 443, resolver=_resolver({"cal.example": ip}))
    assert e.value.code == "blocked_address"


def test_private_feed_hosts_follow_the_existing_private_calendar_setting(env, monkeypatch):
    from wings import ics_feeds as F
    monkeypatch.setenv("ODYSSEUS_ALLOW_PRIVATE_CALDAV", "1")
    assert F.resolve_checked("nas.lan", 80, resolver=_resolver({"nas.lan": "192.168.1.9"}))
    with pytest.raises(F.FeedError):   # loopback never
        F.resolve_checked("me", 80, resolver=_resolver({"me": "127.0.0.1"}))


class _FakeConn:
    """http.client stand-in: answers by path."""
    pages = {}
    seen = []

    def __init__(self, host, port, infos, timeout, https):
        self.host, self.port = host, port

    def request(self, method, path, headers=None):
        _FakeConn.seen.append((self.host, path))
        self.resp = _FakeConn.pages[(self.host, path)]

    def getresponse(self):
        status, headers, body = self.resp
        chunks = [body[i:i + 65536] for i in range(0, len(body), 65536)] or [b""]

        class R:
            def __init__(s):
                s.status = status
                s._c = list(chunks)

            def getheader(s, k, d=None):
                return headers.get(k, d)

            def read(s, n=-1):
                return s._c.pop(0) if s._c else b""
        return R()

    def close(self):
        pass


def test_feed_fetch_follows_redirects_and_rechecks_each_address(env):
    from wings import ics_feeds as F
    _FakeConn.seen = []
    _FakeConn.pages = {
        ("cal.example", "/a.ics"): (302, {"Location": "https://cdn.example/b.ics"}, b""),
        ("cdn.example", "/b.ics"): (200, {}, ICS),
        ("cal.example", "/evil.ics"): (302, {"Location": "http://internal.example/secret"}, b""),
    }
    res = _resolver({"cal.example": "93.184.216.34", "cdn.example": "93.184.216.35", "internal.example": "10.1.2.3"})
    body, final = F.fetch("webcal://cal.example/a.ics", resolver=res, connection_factory=_FakeConn)
    assert body == ICS and final == "https://cdn.example/b.ics"
    with pytest.raises(F.FeedError) as e:
        F.fetch("https://cal.example/evil.ics", resolver=res, connection_factory=_FakeConn)
    assert e.value.code == "blocked_address"
    assert ("internal.example", "/secret") not in _FakeConn.seen


def test_feed_fetch_caps_size_and_redirect_count(env):
    from wings import ics_feeds as F
    _FakeConn.pages = {("big.example", "/x.ics"): (200, {}, b"x" * (F.MAX_BYTES + 10))}
    res = _resolver({"big.example": "93.184.216.34", "loop.example": "93.184.216.34"})
    with pytest.raises(F.FeedError) as e:
        F.fetch("https://big.example/x.ics", resolver=res, connection_factory=_FakeConn)
    assert e.value.code == "too_big"
    _FakeConn.pages = {("loop.example", "/x.ics"): (302, {"Location": "/x.ics"}, b"")}
    with pytest.raises(F.FeedError) as e:
        F.fetch("https://loop.example/x.ics", resolver=res, connection_factory=_FakeConn)
    assert e.value.code == "too_many_redirects"


def test_subscribe_refresh_and_remove_a_feed(env):
    from wings import ics_feeds as F
    from core.database import CalendarCal, CalendarEvent, SessionLocal
    feed = F.subscribe("pat", "https://cal.example/family.ics", fetcher=lambda u: (ICS, u))
    assert feed["name"] == "Family" and feed["events"] == 6
    db = SessionLocal()
    cal = db.query(CalendarCal).filter(CalendarCal.id == feed["calendar_id"]).one()
    assert cal.owner == "pat" and cal.source == "ics"
    assert db.query(CalendarEvent).filter(CalendarEvent.calendar_id == cal.id).count() == 6
    db.close()
    raw = env.cdb.engine.connect().exec_driver_sql("SELECT url FROM wings_ics_feeds").fetchone()[0]
    assert "cal.example" not in raw            # the (secret) link is stored encrypted
    smaller = ICS.replace(b"BEGIN:VEVENT\nUID:one@test", b"BEGIN:VEVENT\nUID:one@test\nSTATUS:CANCELLED")
    assert F.refresh_feed("pat", feed["id"], fetcher=lambda u: (smaller, u))["events"] == 5
    with pytest.raises(F.FeedError):
        F.refresh_feed("sam", feed["id"], fetcher=lambda u: (ICS, u))
    # A failed refresh keeps the last good events and says why.
    with pytest.raises(F.FeedError):
        F.refresh_feed("pat", feed["id"], fetcher=lambda u: (b"<html>nope</html>", u))
    assert F.feeds_for("pat")[0]["error"] and F.feeds_for("pat")[0]["events"] == 5
    assert F.unsubscribe("sam", feed["id"]) is False
    assert F.unsubscribe("pat", feed["id"]) is True
    db = SessionLocal()
    assert db.query(CalendarEvent).count() == 0 and db.query(CalendarCal).count() == 0
    db.close()


def test_refresh_all_due_only_touches_stale_feeds(env, monkeypatch):
    from wings import ics_feeds as F
    feed = F.subscribe("pat", "https://cal.example/family.ics", fetcher=lambda u: (ICS, u))
    done = []
    monkeypatch.setattr(F, "refresh_feed", lambda owner, fid, **k: done.append((owner, fid)))
    assert asyncio.run(F.refresh_all_due()) == 0
    assert asyncio.run(F.refresh_all_due(now=time.time() + F.REFRESH_SECONDS + 5)) == 1
    assert done == [("pat", feed["id"])]


# ------------------------------------------------------------------ Google own key

class _Google:
    """Stand-in for accounts.google.com, oauth2.googleapis.com, userinfo."""

    def __init__(self, scope=None, email="pat@gmail.com"):
        self.scope = scope
        self.email = email
        self.posts = []

    def post(self, url, data, **k):
        self.posts.append((url, dict(data)))
        if data.get("grant_type") == "authorization_code":
            body = {"access_token": "ya29.a", "refresh_token": "1//r", "expires_in": 3599, "token_type": "Bearer"}
            if self.scope:
                body["scope"] = self.scope
            return _Resp(200, body)
        if url.endswith("/revoke"):
            return _Resp(200, {})
        return _Resp(200, {"access_token": "ya29.b", "expires_in": 3599})

    def get_json(self, url, token, **k):
        assert url == "https://openidconnect.googleapis.com/v1/userinfo"
        return _Resp(200, {"email": self.email, "name": "Pat", "email_verified": True})


@pytest.fixture
def google(env, monkeypatch):
    A, G = env.A, env.G
    A.save_client("google", "1234567890-abc.apps.googleusercontent.com", "GOCSPX-sec",
                  "http://127.0.0.1:7013/api/email/oauth/google/callback", by="admin")
    g = _Google()
    monkeypatch.setattr(A, "post_form", g.post)
    monkeypatch.setattr(A, "get_json", g.get_json)
    import wings.accounts_mail as M
    monkeypatch.setattr(M, "start_sync", lambda owner: None)
    return g


def test_sign_in_asks_only_for_ticked_scopes(env, google):
    G = env.G
    url = G.start_sign_in(_req(), "pat", mail=False, calendar=True, drive=False)
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert q["scope"] == f"openid email {G.SCOPE_CALENDAR}"
    assert q["redirect_uri"] == "http://127.0.0.1:7013/api/email/oauth/google/callback"
    assert q["client_id"] == "1234567890-abc.apps.googleusercontent.com"
    with pytest.raises(env.A.AccountError):
        G.start_sign_in(_req(), "pat", mail=False, calendar=False, drive=False)


def _callback(env, monkeypatch, url, user="pat", host="127.0.0.1:7013"):
    calls = []
    req = _fake_email_app(env, monkeypatch, calls)
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    req.headers = {"host": host}
    req.state = SimpleNamespace(current_user=user, api_token=False)
    req.url = SimpleNamespace(scheme="http", path="/api/email/oauth/google/callback", query=f"code=4%2Fcode&state={q['state']}")
    resp = asyncio.run(env.G.callback(req, "4/code", q["state"], None))
    return resp, calls, q


def test_google_sign_in_end_to_end_with_stubs(env, google, monkeypatch):
    G, A = env.G, env.A
    google.scope = f"openid email {G.SCOPE_MAIL} {G.SCOPE_CALENDAR} {G.SCOPE_DRIVE}"
    url = G.start_sign_in(_req(), "pat", mail=True, calendar=True, drive=True)
    resp, calls, q = _callback(env, monkeypatch, url)
    assert resp.status_code == 303 and resp.headers["location"] == "/?ow_accounts=google"
    exch = google.posts[0][1]
    assert exch["grant_type"] == "authorization_code" and exch["code"] == "4/code"
    assert exch["client_secret"] == "GOCSPX-sec" and exch["redirect_uri"] == q["redirect_uri"]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(exch["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    assert challenge == q["code_challenge"]
    link = A.find_link("pat", "google")
    assert link["account"] == "pat@gmail.com" and link["features"]["mail"] and link["features"]["drive"]
    # Mail: an upstream account with XOAUTH2.
    from core.database import EmailAccount, SessionLocal
    db = SessionLocal()
    row = db.query(EmailAccount).filter(EmailAccount.owner == "pat").one()
    assert row.oauth_provider == "google" and row.imap_host == "imap.gmail.com" and row.smtp_port == 587
    from src.secret_storage import decrypt
    assert decrypt(row.oauth_refresh_token) == "1//r" and "1//r" not in row.oauth_refresh_token
    db.close()
    # Calendar: a CalDAV account on Google's endpoint, signed in with the link.
    acc = env.prefs["pat"]["caldav_accounts"][0]
    assert acc["url"] == "https://apidata.googleusercontent.com/caldav/v2/pat@gmail.com/events"
    assert acc["wings_oauth"] == {"provider": "google", "link": link["id"]} and acc["password"] == ""
    url2, token = G.caldav_bearer("pat", acc)
    assert token == "ya29.a" and url2 == acc["url"]
    with pytest.raises(A.AccountError):
        G.caldav_bearer("sam", acc)


def test_google_sign_in_reports_unticked_scopes_on_googles_page(env, google, monkeypatch):
    G = env.G
    google.scope = f"openid email {G.SCOPE_MAIL}"
    url = G.start_sign_in(_req(), "pat", mail=True, calendar=True, drive=False)
    resp, _calls, _q = _callback(env, monkeypatch, url)
    assert resp.headers["location"] == "/?ow_accounts=google&ow_accounts_note=missing_calendar"
    assert "caldav_accounts" not in env.prefs.get("pat", {})


def test_google_callback_for_another_person_or_on_another_address(env, google, monkeypatch):
    G = env.G
    url = G.start_sign_in(_req(), "pat", mail=True, calendar=False, drive=False)
    resp, calls, q = _callback(env, monkeypatch, url, user="sam")
    assert resp.headers["location"] == "/?ow_accounts_error=other_user" and calls == []
    # The registered redirect came back on localhost: bounce to 127.0.0.1 first.
    resp, calls, q = _callback(env, monkeypatch, url, user=None, host="localhost:7013")
    assert resp.status_code == 303 and resp.headers["location"].startswith("http://127.0.0.1:7013/api/email/oauth/google/callback?")
    resp, calls, q = _callback(env, monkeypatch, url, user="pat")
    assert resp.headers["location"] == "/?ow_accounts=google"
    resp, calls, q = _callback(env, monkeypatch, url, user="pat")       # used once only
    assert resp.headers["location"].endswith("email_oauth_error=invalid_state")


def test_upstreams_connect_with_google_gains_pkce_and_checks_the_mailbox(env, google, monkeypatch):
    from core.database import EmailAccount, SessionLocal
    db = SessionLocal()
    db.add(EmailAccount(id="acc1", owner="pat", name="Work", imap_user="pat@gmail.com", smtp_user="pat@gmail.com"))
    db.commit()
    db.close()
    G = env.G
    url = G.email_account_authorize_url(_req(), "pat", "acc1")
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert q["code_challenge_method"] == "S256" and q["scope"] == f"openid email {G.SCOPE_MAIL}"
    resp, _c, _q = _callback(env, monkeypatch, url)
    assert resp.headers["location"] == "/?section=integrations&email_oauth_success=1"
    db = SessionLocal()
    assert db.get(EmailAccount, "acc1").oauth_provider == "google"
    db.close()
    # A different Google account than the mailbox is refused.
    google.email = "someone@gmail.com"
    url = G.email_account_authorize_url(_req(), "pat", "acc1")
    resp, _c, _q = _callback(env, monkeypatch, url)
    assert resp.headers["location"].endswith("email_oauth_error=identity_verification_failed")


def test_env_client_keeps_working_without_a_saved_one(env, monkeypatch):
    A, G = env.A, env.G
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "envid-123456.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "env-secret")
    monkeypatch.setenv("GOOGLE_OAUTH_REDIRECT_URI", "https://mail.example.com/api/email/oauth/google/callback")
    assert G.client_credentials() == ("envid-123456.apps.googleusercontent.com", "env-secret")
    url = G.start_sign_in(_req(), "pat", mail=True, calendar=False, drive=False)
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert q["redirect_uri"] == "https://mail.example.com/api/email/oauth/google/callback"
    assert G.setup_info(_req())["client"]["source"] == "env"


def _drive_link(env):
    env.A.save_client("google", "1234567890-abc.apps.googleusercontent.com", "s", None, by="admin")
    return env.A.save_link("pat", "google", "pat@gmail.com", {"access_token": "ya29.d", "refresh_token": "r", "expires_in": 3600},
                           scopes=[env.G.SCOPE_DRIVE])


class _DriveHTTP:
    def __init__(self):
        self.gets = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.gets.append((url, params, headers))
        assert headers["Authorization"] == "Bearer ya29.d"
        if url.endswith("/files"):
            return _Resp(200, {"files": [{"id": "doc1234567", "name": "Budget 2026", "mimeType": "application/vnd.google-apps.spreadsheet"}]})
        if url.endswith("/doc1234567"):
            return _Resp(200, {"id": "doc1234567", "name": "Budget 2026", "mimeType": "application/vnd.google-apps.spreadsheet"})
        if url.endswith("/pdf12345678"):
            return _Resp(200, {"id": "pdf12345678", "name": "Big.pdf", "mimeType": "application/pdf", "size": str(50 * 1024 * 1024)})
        return _Resp(404, {})

    def stream(self, method, url, params=None, headers=None, timeout=None):
        self.gets.append((url, params, headers))
        assert url.endswith("/doc1234567/export") and params == {"mimeType": "text/csv"}
        return _Resp(200, raw=b"item,amount\nrent,1200\n")


def test_drive_search_and_read_for_the_person_only(env):
    G = env.G
    _drive_link(env)
    http = _DriveHTTP()
    out = G.drive_search("pat", "budget 'x'", http=http)
    assert out["files"][0]["id"] == "doc1234567"
    assert "name contains 'budget \\'x\\''" in http.gets[0][1]["q"] and "trashed = false" in http.gets[0][1]["q"]
    got = G.drive_read("pat", "doc1234567", http=http)
    assert got["content"].startswith("item,amount") and got["content_status"] == "read"
    with pytest.raises(env.A.AccountError) as e:
        G.drive_search("sam", "budget", http=http)
    assert e.value.code == "drive_not_connected"
    with pytest.raises(env.A.AccountError) as e:
        G.drive_read("pat", "pdf12345678", http=http)
    assert e.value.code == "too_big"
    with pytest.raises(env.A.AccountError) as e:
        G.drive_read("pat", "../../etc", http=http)
    assert e.value.code == "bad_id"


def test_drive_tool_handler_formats_results(env, monkeypatch):
    G = env.G
    monkeypatch.setattr(G, "drive_search", lambda owner, q, **k: {"account": "pat@gmail.com", "files": [
        {"id": "f1abcdefgh", "name": "Notes", "type": "text/plain", "modified": "2026-01-01"}]})
    out = asyncio.run(G._tool_search({"query": "notes"}, "pat"))
    assert "Notes | id: f1abcdefgh" in out["response"] and out["exit_code"] == 0


class _CalHTTP:
    def __init__(self, link_now=False):
        self.calls = []
        self.link_now = link_now

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url, params))
        if url.endswith("/events"):
            return _Resp(200, {"items": [{"id": "gevt1", "iCalUID": params["iCalUID"]}]})
        return _Resp(200, {"id": "gevt1", "hangoutLink": "https://meet.google.com/abc-defg-hij"})

    def patch(self, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append(("PATCH", url, params, json))
        body = {"id": "gevt1", "conferenceData": {"createRequest": {"status": {"statusCode": "pending"}}}}
        if self.link_now:
            body["hangoutLink"] = "https://meet.google.com/abc-defg-hij"
        return _Resp(200, body)


def test_meet_link_is_added_with_the_calendar_api_and_saved_on_the_event(env, google):
    G, A = env.G, env.A
    from core.database import CalendarCal, CalendarEvent, SessionLocal
    link = A.save_link("pat", "google", "pat@gmail.com", {"access_token": "ya29.c", "refresh_token": "r", "expires_in": 3600},
                       scopes=[G.SCOPE_CALENDAR])
    env.prefs["pat"] = {"caldav_accounts": [{"id": "cd1", "url": "x", "username": "pat@gmail.com", "password": "",
                                             "wings_oauth": {"provider": "google", "link": link}}]}
    db = SessionLocal()
    db.add(CalendarCal(id="caldav-1", owner="pat", name="pat@gmail.com", source="caldav", account_id="cd1",
                       caldav_base_url="https://apidata.googleusercontent.com/caldav/v2/pat%40gmail.com/events/"))
    db.add(CalendarEvent(uid="u-1", calendar_id="caldav-1", summary="Sync", dtstart=datetime(2099, 1, 1, 9),
                         dtend=datetime(2099, 1, 1, 10), remote_href="https://x/u-1.ics"))
    db.commit()
    db.close()
    http = _CalHTTP()
    out = G.add_meet("pat", "u-1", http=http, sleep=lambda s: None)
    assert out["meet_link"] == "https://meet.google.com/abc-defg-hij"
    patch = [c for c in http.calls if c[0] == "PATCH"][0]
    assert patch[1].endswith("/calendars/pat%40gmail.com/events/gevt1")
    assert patch[2] == {"conferenceDataVersion": 1}
    assert patch[3]["conferenceData"]["createRequest"]["conferenceSolutionKey"] == {"type": "hangoutsMeet"}
    db = SessionLocal()
    ev = db.query(CalendarEvent).filter(CalendarEvent.uid == "u-1").one()
    assert ev.location == "https://meet.google.com/abc-defg-hij" and "Google Meet: https://meet.google.com/" in ev.description
    db.close()
    with pytest.raises(A.AccountError):
        G.add_meet("sam", "u-1", http=http, sleep=lambda s: None)


def test_meet_is_refused_for_non_google_calendars(env, google):
    from core.database import CalendarCal, CalendarEvent, SessionLocal
    db = SessionLocal()
    db.add(CalendarCal(id="local-1", owner="pat", name="Home", source="local"))
    db.add(CalendarEvent(uid="u-2", calendar_id="local-1", summary="x", dtstart=datetime(2099, 1, 1), dtend=datetime(2099, 1, 2)))
    db.commit()
    db.close()
    with pytest.raises(env.A.AccountError) as e:
        env.G.add_meet("pat", "u-2", http=_CalHTTP(), sleep=lambda s: None)
    assert e.value.code == "not_google"


# ------------------------------------------------------------------ XOAUTH2 for the assistant

def test_mail_token_dispatch_and_xoauth2_frame(env, monkeypatch):
    A = env.A
    monkeypatch.setitem(A.MAIL_TOKEN_PROVIDERS, "google", lambda account_id, cfg: f"tok-{account_id}")
    assert A.mail_token({"oauth_provider": "google", "account_id": "a1"}) == "tok-a1"
    assert A.mail_token({"oauth_provider": ""}) is None
    assert A.xoauth2("pat@gmail.com", "tok") == "user=pat@gmail.com\x01auth=Bearer tok\x01\x01"


def test_email_mcp_server_signs_in_with_xoauth2(env, monkeypatch, tmp_path):
    import mcp_servers.email_server as es
    from core.database import EmailAccount, SessionLocal
    from src.secret_storage import encrypt
    db = SessionLocal()
    db.add(EmailAccount(id="g1", owner="pat", name="pat@gmail.com", is_default=True, enabled=True,
                        imap_host="imap.gmail.com", imap_port=993, imap_user="pat@gmail.com", imap_starttls=False,
                        imap_password=encrypt(""), smtp_host="smtp.gmail.com", smtp_port=587, smtp_user="pat@gmail.com",
                        smtp_password=encrypt(""), from_address="pat@gmail.com", oauth_provider="google",
                        oauth_access_token=encrypt("ya29.mail"), oauth_refresh_token=encrypt("1//r"),
                        oauth_token_expiry=str(int(time.time()) + 3600)))
    db.commit()
    db.close()
    monkeypatch.setattr(es, "_db_path", lambda: tmp_path / "t.db")
    es._ACCOUNT_CACHE.clear()
    seen = {}

    class FakeIMAP:
        def __init__(self, host, port, timeout=None):
            seen["host"] = (host, port)
            self.sock = None

        def authenticate(self, mech, cb):
            seen["auth"] = (mech, cb(b""))
            return ("OK", [b""])

        def login(self, *a):
            raise AssertionError("password login used for an OAuth account")

    monkeypatch.setattr(es.imaplib, "IMAP4_SSL", FakeIMAP)
    token = es._CURRENT_OWNER.set("pat")
    try:
        es._imap_connect(None)
        cfg = es._load_config(None)
        assert es._smtp_ready(cfg)
    finally:
        es._CURRENT_OWNER.reset(token)
    assert seen["host"] == ("imap.gmail.com", 993)
    assert seen["auth"] == ("XOAUTH2", b"user=pat@gmail.com\x01auth=Bearer ya29.mail\x01\x01")


# ------------------------------------------------------------------ routes

@pytest.fixture
def client(env, monkeypatch, google):
    from wings.accounts_routes import setup_accounts_routes
    app = FastAPI()
    app.state.auth_manager = _Auth()
    app.add_middleware(_FakeSession)
    app.include_router(setup_accounts_routes())
    return TestClient(app, base_url="http://127.0.0.1:7013")


def test_routes_need_a_person_in_their_own_browser(client):
    assert client.get("/api/wings/accounts/status").status_code == 403
    assert client.get("/api/wings/accounts/status", headers={"x-test-user": "pat", "x-test-token": "1"}).status_code == 403
    st = client.get("/api/wings/accounts/status", headers={"x-test-user": "pat"}).json()
    assert st["admin"] is False and st["google"]["configured"] is True
    assert {p["key"] for p in st["mail_providers"]} == {"gmail", "icloud", "yahoo"}
    assert {a["slug"] for a in st["apps"]} >= {"notion", "linear", "atlassian"}


def test_google_key_setup_is_admin_only_and_never_returns_the_secret(client):
    assert client.get("/api/wings/accounts/google/setup", headers={"x-test-user": "pat"}).status_code == 403
    r = client.post("/api/wings/accounts/google/setup", headers={"x-test-user": "pat"},
                    json={"client_id": "1234567890-x.apps.googleusercontent.com", "client_secret": "GOCSPX-new"})
    assert r.status_code == 403
    r = client.post("/api/wings/accounts/google/setup", headers={"x-test-user": "admin"},
                    json={"client_id": "1234567890-x.apps.googleusercontent.com", "client_secret": "GOCSPX-new"})
    assert r.status_code == 200 and "GOCSPX-new" not in r.text
    info = client.get("/api/wings/accounts/google/setup", headers={"x-test-user": "admin"}).json()
    assert info["redirect_uri"] == "http://127.0.0.1:7013/api/email/oauth/google/callback" and info["redirect_ok"]
    assert info["console"]["clients"] == "https://console.cloud.google.com/auth/clients"


def test_google_start_route_returns_googles_page(client):
    r = client.post("/api/wings/accounts/google/start", headers={"x-test-user": "pat"},
                    json={"mail": True, "calendar": True, "drive": True})
    url = r.json()["authorize_url"]
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    r = client.post("/api/wings/accounts/google/start", headers={"x-test-user": "pat"}, json={})
    assert r.status_code == 400 and r.json()["error"] == "nothing_ticked"


def test_calendar_link_routes(client, monkeypatch):
    from wings import ics_feeds as F
    monkeypatch.setattr(F, "fetch", lambda url, **k: (ICS, url))
    r = client.post("/api/wings/accounts/calendar-link", headers={"x-test-user": "pat"},
                    json={"url": "webcal://cal.example/family.ics", "name": "Home"})
    assert r.status_code == 200 and r.json()["name"] == "Home"
    fid = r.json()["id"]
    assert client.post(f"/api/wings/accounts/calendar-link/{fid}/refresh", headers={"x-test-user": "pat"}).json()["events"] == 6
    assert client.delete(f"/api/wings/accounts/calendar-link/{fid}", headers={"x-test-user": "sam"}).status_code == 404
    assert client.delete(f"/api/wings/accounts/calendar-link/{fid}", headers={"x-test-user": "pat"}).json()["ok"]
    r = client.post("/api/wings/accounts/calendar-link", headers={"x-test-user": "pat"}, json={"url": "ftp://x/y.ics"})
    assert r.status_code == 400 and r.json()["error"] == "bad_scheme"


def test_route_endpoint_finds_upstream_routes_inside_included_routers(env):
    """FastAPI 0.16x keeps included routers nested; upstream's own route
    function must still be found (the account and model routes are)."""
    from fastapi import APIRouter
    app = FastAPI()
    outer = APIRouter(prefix="/api/email")
    inner = APIRouter(prefix="/accounts")

    @inner.post("")
    async def create_email_account():
        return {"ok": True}

    outer.include_router(inner)
    app.include_router(outer)
    req = SimpleNamespace(app=app)
    assert env.A.route_endpoint(req, "/api/email/accounts", "POST") is create_email_account
    assert env.A.route_endpoint(req, "/api/email/accounts", "GET") is None
    assert env.A.route_endpoint(req, "/api/nothing", "POST") is None


def test_drive_tools_convert_and_are_classified_for_the_safety_gate(env):
    env.A.install_tools()
    from src.tool_schemas import function_call_to_tool_block
    from src.tool_capabilities import capabilities_for_action, ToolEffect, ResultIntegrity
    block = function_call_to_tool_block("google_drive_search", '{"query": "budget"}')
    assert block is not None and block.tool_type == "google_drive_search" and json.loads(block.content) == {"query": "budget"}
    caps = capabilities_for_action("google_drive_read", '{"file_id": "x"}')
    assert caps.known and caps.effects == frozenset({ToolEffect.READ_PRIVATE})
    assert caps.result_integrity == ResultIntegrity.EXTERNAL_UNTRUSTED


@pytest.mark.parametrize("host,addr,key", [
    ("imap.gmail.com", "x@example.com", "gmail"),
    ("imap.mail.me.com", "", "icloud"),
    ("stubs", "pat@icloud.com", "icloud"),
    ("mail.example.net", "pat@yahoo.co.uk", "yahoo"),
    ("mail.example.net", "pat@example.net", "other"),
])
def test_mail_accounts_land_on_the_right_card(env, host, addr, key):
    from wings.accounts_mail import provider_for_host
    assert provider_for_host(host, addr) == key


# ------------------------------------------------------------------ fix round (0025 review)

def test_feed_fetch_has_an_absolute_deadline_against_slow_servers(env, monkeypatch):
    """A server that drips one byte at a time (headers or body) cannot hold a
    worker past TIMEOUT_SECONDS: the watchdog closes the socket."""
    import threading as _t
    from wings import ics_feeds as F
    monkeypatch.setattr(F, "TIMEOUT_SECONDS", 1.5)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]
    stop = _t.Event()

    def drip():
        while not stop.is_set():
            try:
                srv.settimeout(0.5)
                c, _a = srv.accept()
            except OSError:
                continue
            try:
                c.recv(4096)
                for ch in b"HTTP/1.1 200 OK\r\nContent-Type: text/calendar\r\n\r\nBEGIN:VCALENDAR\r\n" * 50:
                    if stop.is_set():
                        break
                    c.sendall(bytes([ch]))
                    time.sleep(0.2)
            except OSError:
                pass
            finally:
                c.close()
    th = _t.Thread(target=drip, daemon=True)
    th.start()

    def factory(host, p, infos, timeout, https):
        return F._PinnedHTTP(host, port, [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))], timeout)
    began = time.monotonic()
    try:
        with pytest.raises(F.FeedError) as e:
            F.fetch("http://slow.example/a.ics", resolver=_resolver({"slow.example": "93.184.216.34"}),
                    connection_factory=factory)
        assert e.value.code == "timeout"
        assert time.monotonic() - began < 4.0
    finally:
        stop.set()
        srv.close()


def test_feed_fetches_are_limited_per_person_and_in_all(env, monkeypatch):
    from wings import ics_feeds as F
    with F._Slot("pat", 0), F._Slot("pat", 0):
        with pytest.raises(F.FeedError) as e:
            with F._Slot("pat", 0):
                pass
        assert e.value.code == "busy"
        with F._Slot("sam", 0), F._Slot("kim", 0):        # 4 in all now
            with pytest.raises(F.FeedError):
                with F._Slot("lee", 0):
                    pass
    with F._Slot("pat", 0):                                 # all released again
        pass
    assert F._per_person == {}


def test_a_person_can_add_only_so_many_calendar_links(env, monkeypatch):
    from wings import ics_feeds as F
    monkeypatch.setattr(F, "MAX_FEEDS_PER_PERSON", 1)
    F.subscribe("pat", "https://cal.example/a.ics", fetcher=lambda u: (ICS, u))
    with pytest.raises(F.FeedError) as e:
        F.subscribe("pat", "https://cal.example/b.ics", fetcher=lambda u: (ICS, u))
    assert e.value.code == "too_many"
    assert F.subscribe("sam", "https://cal.example/b.ics", fetcher=lambda u: (ICS, u))["ok"]


def test_a_link_calendar_never_becomes_the_default_calendar(env):
    from core.database import CalendarCal, SessionLocal
    from routes.calendar_routes import _ensure_default_calendar
    from wings import ics_feeds as F
    F.subscribe("pat", "https://cal.example/a.ics", fetcher=lambda u: (ICS, u))
    db = SessionLocal()
    try:
        cal = _ensure_default_calendar(db, "pat")
        db.commit()
        assert cal.source == "local" and cal.name == "Personal"
        assert {c.source for c in db.query(CalendarCal).filter(CalendarCal.owner == "pat")} == {"ics", "local"}
        assert _ensure_default_calendar(db, "pat").id == cal.id
    finally:
        db.close()


def test_one_person_cannot_push_out_anothers_pending_sign_in(env):
    A = env.A
    mine = A.start_flow("pat", "google", "connect", ["openid"], redirect_uri="x", origin="y")
    for _ in range(80):
        A.start_flow("sam", "google", "connect", ["openid"], redirect_uri="x", origin="y")
    assert A.take_flow(mine.state, "pat").owner == "pat"
    assert sum(1 for f in A._flows.values() if f.owner == "sam") == A.MAX_PENDING_PER_OWNER


def test_a_google_token_only_goes_to_googles_mail_servers(env, monkeypatch):
    A = env.A
    calls = []
    monkeypatch.setitem(A.MAIL_TOKEN_PROVIDERS, "google", lambda account_id, cfg: calls.append(account_id) or "tok")
    ok = {"oauth_provider": "google", "account_id": "a1", "imap_host": "imap.gmail.com", "smtp_host": "smtp.gmail.com"}
    assert A.mail_token(ok) == "tok"
    for bad in ({**ok, "imap_host": "relay.example"}, {**ok, "smtp_host": "smtp.evil.example"}):
        assert A.mail_token(bad) is None
    assert calls == ["a1"]


def test_google_sign_in_never_takes_over_another_mail_account(env, google, monkeypatch):
    """A relay account with the same From address keeps its password and
    hosts; the Google sign-in gets an account of its own on Google's
    servers."""
    from core.database import EmailAccount, SessionLocal
    from src.secret_storage import decrypt, encrypt
    db = SessionLocal()
    db.add(EmailAccount(id="relay1", owner="pat", name="relay", is_default=True, enabled=True,
                        imap_host="relay.example", imap_port=1143, imap_user="pat-relay", imap_starttls=False,
                        imap_password=encrypt("relaypw"), smtp_host="relay.example", smtp_port=25,
                        smtp_user="pat-relay", smtp_password=encrypt("relaypw"), from_address="pat@gmail.com"))
    db.commit()
    db.close()
    G = env.G
    google.scope = f"openid email {G.SCOPE_MAIL}"
    url = G.start_sign_in(_req(), "pat", mail=True, calendar=False, drive=False)
    resp, _calls, _q = _callback(env, monkeypatch, url)
    assert resp.headers["location"] == "/?ow_accounts=google"
    db = SessionLocal()
    relay = db.get(EmailAccount, "relay1")
    assert relay.oauth_provider in (None, "") and decrypt(relay.imap_password) == "relaypw"
    assert relay.imap_host == "relay.example"
    gm = [r for r in db.query(EmailAccount).filter(EmailAccount.owner == "pat") if r.oauth_provider == "google"]
    assert len(gm) == 1 and gm[0].id != "relay1" and gm[0].imap_host == "imap.gmail.com"
    db.close()


def test_signing_in_again_with_fewer_ticks_keeps_what_is_connected(env, google, monkeypatch):
    G, A = env.G, env.A
    google.scope = f"openid email {G.SCOPE_MAIL} {G.SCOPE_CALENDAR}"
    url = G.start_sign_in(_req(), "pat", mail=True, calendar=True, drive=False)
    _callback(env, monkeypatch, url)
    google.scope = f"openid email {G.SCOPE_MAIL} {G.SCOPE_CALENDAR} {G.SCOPE_DRIVE}"
    url = G.start_sign_in(_req(), "pat", mail=False, calendar=False, drive=True)
    resp, _c, _q = _callback(env, monkeypatch, url)
    assert resp.headers["location"] == "/?ow_accounts=google"
    st = G.status_for(_req(), "pat")["links"][0]
    assert st["mail"] and st["calendar"] and st["drive"]


@pytest.mark.parametrize("uri,ok", [
    ("https://openwings.local/api/email/oauth/google/callback", False),
    ("https://wings.lan/x", False),
    ("https://nas.home.arpa/x", False),
    ("https://wings/x", False),
    ("https://10.0.0.5/x", False),
    ("https://[::1]:7011/x", True),
    ("https://wings.example.co.uk/x", True),
    ("http://wings.example.com/x", False),
])
def test_redirect_uri_rules_refuse_private_names_and_raw_ips(env, uri, ok):
    assert env.A.safe_redirect_uri(uri) is ok


def test_wizard_warns_when_sign_in_works_only_on_the_openwings_computer(env, monkeypatch):
    G = env.G
    monkeypatch.setenv("ALLOWED_ORIGINS", "http://localhost:7013,http://127.0.0.1:7013,http://192.168.1.20:7013")
    lan = G.setup_info(_req(host="192.168.1.20:7013"))
    assert lan["redirect_uri"] == "http://127.0.0.1:7013/api/email/oauth/google/callback"
    assert lan["only_this_computer"] is True and "public domain" in lan["redirect_rules"]
    assert G.setup_info(_req(host="127.0.0.1:7013"))["only_this_computer"] is False


def test_saved_key_can_move_to_the_address_openwings_is_opened_at_now(env, monkeypatch):
    G, A = env.G, env.A
    monkeypatch.setenv("ALLOWED_ORIGINS", "http://localhost:7013,http://127.0.0.1:7013,https://wings.example.com")
    A.save_client("google", "1234567890-abc.apps.googleusercontent.com", "GOCSPX-sec",
                  "http://127.0.0.1:7013/api/email/oauth/google/callback", by="admin")
    here = _req(host="wings.example.com", scheme="https")
    info = G.setup_info(here)
    assert info["redirect_uri"].startswith("http://127.0.0.1:7013/")
    assert info["current_uri"] == "https://wings.example.com/api/email/oauth/google/callback"
    after = G.use_current_redirect(here, "admin")
    assert after["redirect_uri"] == "https://wings.example.com/api/email/oauth/google/callback"
    assert after["current_uri"] == ""
    assert A.get_client("google").client_secret == "GOCSPX-sec"


def test_drive_tools_only_for_people_who_connected_drive(env, google, monkeypatch):
    G, A = env.G, env.A
    A.install_tools()
    assert {"google_drive_search", "google_drive_read"} <= A.unavailable_tools("pat")
    google.scope = f"openid email {G.SCOPE_DRIVE}"
    url = G.start_sign_in(_req(), "pat", mail=False, calendar=False, drive=True)
    _callback(env, monkeypatch, url)
    assert not ({"google_drive_search", "google_drive_read"} & A.unavailable_tools("pat"))
    assert {"google_drive_search", "google_drive_read"} <= A.unavailable_tools("sam")
    from src import tool_capabilities as caps
    assert {"google_drive_search", "google_drive_read"} <= caps.KNOWN_CAPABILITY_TOOLS


def test_changes_must_come_from_openwings_own_page(client):
    h = {"x-test-user": "pat"}
    r = client.post("/api/wings/accounts/calendar-link", headers={**h, "content-type": "text/plain"},
                    content=b'{"url":"https://cal.example/a.ics","x":"="}')
    assert r.status_code == 415
    r = client.post("/api/wings/accounts/calendar-link", headers={**h, "sec-fetch-site": "same-site"},
                    json={"url": "https://cal.example/a.ics"})
    assert r.status_code == 403
    r = client.post("/api/wings/accounts/google/start", headers={**h, "origin": "http://127.0.0.1:8080"},
                    json={"mail": True})
    assert r.status_code == 403
    r = client.post("/api/wings/accounts/google/start",
                    headers={**h, "origin": "http://127.0.0.1:7013", "sec-fetch-site": "same-origin"},
                    json={"mail": True})
    assert r.status_code == 200
    # Reads are not affected.
    assert client.get("/api/wings/accounts/status", headers={**h, "sec-fetch-site": "cross-site"}).status_code == 200


def test_members_see_a_work_app_as_connected_only_when_it_is(env, monkeypatch):
    from core.database import Base, McpServer, SessionLocal
    from wings.accounts_routes import app_states
    Base.metadata.create_all(env.cdb.engine, tables=[McpServer.__table__])
    db = SessionLocal()
    db.add(McpServer(id="n1", name="Notion", transport="http", url="https://mcp.notion.com/mcp", is_enabled=True))
    db.add(McpServer(id="l1", name="Linear", transport="http", url="https://mcp.linear.app/mcp", is_enabled=True))
    db.commit()
    db.close()

    class _Mgr:
        def get_server_status(self, sid):
            return {"status": "connected"} if sid == "n1" else {"status": "error"}
    st = app_states(_Mgr())
    assert st["notion"] is True and st["linear"] is False and st["atlassian"] is False
    assert app_states(None) == {k: False for k in st}


def test_mail_dialogs_get_the_no_app_passwords_note(env):
    from wings import accounts_mail as M
    cards = {c["key"]: c for c in M.provider_cards()}
    assert "work or school" in cards["gmail"]["unavailable"]
    assert cards["icloud"]["unavailable"] and cards["yahoo"]["unavailable"]

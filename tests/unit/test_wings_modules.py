"""Unit tests for the Wings modules shipped in overlay/app/wings.

Run inside the built image (no network, no running app needed):

    docker run --rm --network none --entrypoint python \
      -v "$PWD/tests/unit:/wings-tests:ro" wings/odysseus:<tag> \
      -m pytest -q -p no:cacheprovider /wings-tests

or simply: ./tests/unit/run.sh
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, "/app")

from cryptography.fernet import Fernet  # noqa: E402


# ---------------------------------------------------------------- email gate

@pytest.fixture
def gate(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("WINGS_FORCE_EMAIL_CONFIRM", "1")
    monkeypatch.delenv("WINGS_EMAIL_SELF_ADDRESSES", raising=False)
    import wings.email_gate as g
    return importlib.reload(g)


def _request(user="admin", api_token=False, headers=None):
    return SimpleNamespace(
        state=SimpleNamespace(current_user=user, api_token=api_token),
        headers=headers or {},
    )


def test_cookie_session_is_human(gate):
    assert gate.is_human_request(_request())


def test_agent_loopback_is_not_human_even_when_impersonating(gate):
    # The agent's app_api calls carry the internal token header and may set
    # current_user to the owner; they must never count as a person.
    req = _request(user="admin", headers={"X-Odysseus-Internal-Token": "whatever"})
    assert not gate.is_human_request(req)


def test_api_token_is_not_human(gate):
    assert not gate.is_human_request(_request(user="api", api_token=True))


def test_missing_request_is_not_human(gate):
    assert not gate.is_human_request(None)


def test_auth_disabled_means_nobody_is_verified_human(gate, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    assert not gate.is_human_request(_request())


def test_require_human_raises_403_for_agent(gate):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        gate.require_human(_request(headers={"X-Odysseus-Internal-Token": "t"}))
    assert exc.value.status_code == 403


def test_require_human_allows_person(gate):
    gate.require_human(_request())


def test_check_send_blocks_unapproved(gate):
    with pytest.raises(gate.EmailSendBlocked):
        gate.check_send(["friend@example.com"], human_approved=False)


def test_check_send_allows_human_approved(gate):
    gate.check_send(["friend@example.com"], human_approved=True)


def test_self_allowlist_permits_only_listed_recipients(gate, monkeypatch):
    monkeypatch.setenv("WINGS_EMAIL_SELF_ADDRESSES", "Me@Example.com; alt@example.com")
    gate.check_send(["me@example.com"], human_approved=False)
    gate.check_send(['"Me" <ME@example.com>', "alt@example.com"], human_approved=False)
    with pytest.raises(gate.EmailSendBlocked):
        gate.check_send(["me@example.com", "friend@example.com"], human_approved=False)


def test_empty_recipient_list_is_blocked(gate, monkeypatch):
    monkeypatch.setenv("WINGS_EMAIL_SELF_ADDRESSES", "me@example.com")
    with pytest.raises(gate.EmailSendBlocked):
        gate.check_send([], human_approved=False)


def test_gate_off_restores_upstream_behaviour(gate, monkeypatch):
    monkeypatch.setenv("WINGS_FORCE_EMAIL_CONFIRM", "0")
    gate.check_send(["friend@example.com"], human_approved=False)
    gate.require_human(None)


def test_scheduled_flag_roundtrip(gate, tmp_path):
    import sqlite3
    conn = sqlite3.connect(tmp_path / "s.db")
    conn.execute("CREATE TABLE scheduled_emails (id TEXT PRIMARY KEY, status TEXT)")
    conn.execute("INSERT INTO scheduled_emails VALUES ('a','pending'),('b','pending')")
    conn.commit()
    assert not gate.scheduled_row_approved(conn, "a")
    gate.mark_scheduled_approved(conn, "a")
    assert gate.scheduled_row_approved(conn, "a")
    assert not gate.scheduled_row_approved(conn, "b")
    gate.ensure_scheduled_flag(conn)  # idempotent


def test_smtp_chokepoint_refuses_without_approval(gate):
    from routes.email_helpers import _send_smtp_message
    cfg = {"smtp_host": "192.0.2.1", "smtp_port": 25, "smtp_security": "none"}
    with pytest.raises(gate.EmailSendBlocked):
        _send_smtp_message(cfg, "me@example.com", ["friend@example.com"], "Subject: x\n\nx")


# ---------------------------------------------------------------- env scrub

def test_scrubbed_environ_drops_secrets_keeps_the_rest():
    from wings.envscrub import scrubbed_environ
    base = {
        "PATH": "/usr/bin", "HOME": "/app", "TZ": "UTC",
        "ODYSSEUS_APP_KEY": "k", "ODYSSEUS_ADMIN_PASSWORD": "p", "OPENAI_API_KEY": "sk",
        "GOOGLE_OAUTH_CLIENT_SECRET": "s", "DATABASE_URL": "sqlite://", "HF_TOKEN": "hf",
        "SEARXNG_SECRET": "x", "SOME_TOKEN": "t",
    }
    out = scrubbed_environ(base)
    assert out["PATH"] == "/usr/bin" and out["TZ"] == "UTC"
    for gone in ("ODYSSEUS_APP_KEY", "ODYSSEUS_ADMIN_PASSWORD", "OPENAI_API_KEY",
                 "GOOGLE_OAUTH_CLIENT_SECRET", "DATABASE_URL", "SEARXNG_SECRET", "SOME_TOKEN"):
        assert gone not in out
    assert out["HF_TOKEN"] == "hf"  # Cookbook downloads need it
    assert "HF_TOKEN" not in scrubbed_environ(base, keep=())


# ---------------------------------------------------------------- agent tools

def _write_presets(tmp_path, custom):
    (tmp_path / "presets.json").write_text(json.dumps({"custom": custom}))


def test_allowlist_none_means_no_restriction(tmp_path, monkeypatch):
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path))
    from wings import agent_tools
    _write_presets(tmp_path, {"enabled": True, "enabled_tools": None})
    assert agent_tools.agent_denied_tools("custom") == set()
    assert agent_tools.agent_denied_tools(None) == set()


def test_disabled_custom_preset_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path))
    from wings import agent_tools
    _write_presets(tmp_path, {"enabled": False, "enabled_tools": []})
    assert agent_tools.agent_denied_tools("custom") == set()


def test_calendar_only_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path))
    from wings import agent_tools
    _write_presets(tmp_path, {"enabled": True, "enabled_tools": ["calendar"]})
    denied = agent_tools.agent_denied_tools("custom")
    assert "manage_calendar" not in denied
    for t in ("web_search", "bash", "python", "manage_notes", "send_email", "app_api", "manage_settings"):
        assert t in denied, t
    assert "ask_user" not in denied


def test_empty_list_denies_everything_but_the_core(tmp_path, monkeypatch):
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path))
    from wings import agent_tools
    _write_presets(tmp_path, {"enabled": True, "enabled_tools": []})
    denied = agent_tools.agent_denied_tools("custom")
    assert {"web_search", "manage_calendar", "bash"} <= denied
    assert "ask_user" not in denied and "update_plan" not in denied


def test_malformed_allowlist_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path))
    from wings import agent_tools
    _write_presets(tmp_path, {"enabled": True, "enabled_tools": "everything"})
    assert "web_search" in agent_tools.agent_denied_tools("custom")


def test_mcp_tools_follow_their_family(monkeypatch):
    from wings import agent_tools
    mcp = [("email", "send_email"), ("email", "list_emails"), ("memory", "manage_memory"),
           ("rag", "manage_rag"), ("thirdparty", "do_thing")]
    contract = agent_tools._contract_families()
    email_only = agent_tools.allowed_tool_names(["email"], contract, mcp)
    assert "mcp__email__send_email" in email_only and "mcp__email__list_emails" in email_only
    assert "mcp__thirdparty__do_thing" not in email_only
    assert "mcp__memory__manage_memory" not in email_only
    ext = agent_tools.allowed_tool_names(["external_mcp"], contract, mcp)
    assert "mcp__thirdparty__do_thing" in ext and "mcp__email__send_email" not in ext
    files = agent_tools.allowed_tool_names(["files"], contract, mcp)
    assert "mcp__rag__manage_rag" in files


# ---------------------------------------------------------------- encryption key

def _fresh_secret_storage(monkeypatch, tmp_path, key):
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path))
    if key is None:
        monkeypatch.delenv("ODYSSEUS_APP_KEY", raising=False)
    else:
        monkeypatch.setenv("ODYSSEUS_APP_KEY", key)
    import src.secret_storage as ss
    ss = importlib.reload(ss)
    monkeypatch.setattr(ss, "_KEY_PATH", tmp_path / ".app_key")
    monkeypatch.setattr(ss, "_fernet", None)
    return ss


def test_env_key_is_used_and_no_file_written(monkeypatch, tmp_path):
    key = Fernet.generate_key().decode()
    ss = _fresh_secret_storage(monkeypatch, tmp_path, key)
    token = ss.encrypt("secret")
    assert token.startswith("enc:")
    assert Fernet(key.encode()).decrypt(token[4:].encode()) == b"secret"
    assert not (tmp_path / ".app_key").exists()


def test_malformed_env_key_raises(monkeypatch, tmp_path):
    ss = _fresh_secret_storage(monkeypatch, tmp_path, "not-a-key")
    with pytest.raises(RuntimeError):
        ss.encrypt("secret")


def test_mismatched_key_file_raises(monkeypatch, tmp_path):
    (tmp_path / ".app_key").write_bytes(Fernet.generate_key())
    ss = _fresh_secret_storage(monkeypatch, tmp_path, Fernet.generate_key().decode())
    with pytest.raises(RuntimeError):
        ss.encrypt("secret")


def test_matching_key_file_is_accepted(monkeypatch, tmp_path):
    key = Fernet.generate_key()
    (tmp_path / ".app_key").write_bytes(key)
    ss = _fresh_secret_storage(monkeypatch, tmp_path, key.decode())
    assert ss.decrypt(ss.encrypt("x")) == "x"


def test_without_env_upstream_file_behaviour(monkeypatch, tmp_path):
    ss = _fresh_secret_storage(monkeypatch, tmp_path, None)
    ss.encrypt("x")
    assert (tmp_path / ".app_key").exists()


def test_api_key_manager_uses_env_key(monkeypatch, tmp_path):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("ODYSSEUS_APP_KEY", key)
    from src.api_key_manager import APIKeyManager
    mgr = APIKeyManager(str(tmp_path))
    mgr.save("brave", "abc")
    assert mgr.load() == {"brave": "abc"}
    assert not (tmp_path / ".key").exists()


# ---------------------------------------------------------------- preflight

def _preflight(env, data_dir):
    e = {k: v for k, v in os.environ.items() if k != "ODYSSEUS_APP_KEY"}
    e.update(env)
    e["ODYSSEUS_DATA_DIR"] = str(data_dir)
    return subprocess.run([sys.executable, "/app/wings/preflight.py"], env=e,
                          capture_output=True, text=True)


def test_preflight_ok_with_valid_key(tmp_path):
    r = _preflight({"ODYSSEUS_APP_KEY": Fernet.generate_key().decode()}, tmp_path)
    assert r.returncode == 0, r.stderr


def test_preflight_rejects_bad_key(tmp_path):
    r = _preflight({"ODYSSEUS_APP_KEY": "nope"}, tmp_path)
    assert r.returncode == 78 and "not a valid Fernet key" in r.stderr


def test_preflight_rejects_mismatched_file(tmp_path):
    (tmp_path / ".app_key").write_bytes(Fernet.generate_key())
    r = _preflight({"ODYSSEUS_APP_KEY": Fernet.generate_key().decode()}, tmp_path)
    assert r.returncode == 78 and "adopt-key.sh" in r.stderr


def test_preflight_imports_nothing_from_the_app(tmp_path):
    # Importing the upstream `src` package runs DB migrations; the preflight
    # runs as root before privileges drop and must stay side-effect free.
    code = (
        "import runpy,sys;sys.argv=['x'];"
        "\ntry:\n runpy.run_path('/app/wings/preflight.py', run_name='__main__')\nexcept SystemExit:\n pass\n"
        "print('src' in sys.modules, 'core.database' in sys.modules)"
    )
    e = dict(os.environ, ODYSSEUS_APP_KEY=Fernet.generate_key().decode(), ODYSSEUS_DATA_DIR=str(tmp_path))
    out = subprocess.run([sys.executable, "-c", code], env=e, capture_output=True, text=True).stdout
    assert out.strip().endswith("False False"), out


# ---------------------------------------------------------------- review fixes

def test_key_from_keyholder_when_env_was_moved(monkeypatch, tmp_path):
    key = Fernet.generate_key().decode()
    ss = _fresh_secret_storage(monkeypatch, tmp_path, None)
    from wings import keyholder
    monkeypatch.setattr(keyholder, "APP_KEY", key)
    token = ss.encrypt("x")
    assert Fernet(key.encode()).decrypt(token[4:].encode()) == b"x"
    assert not (tmp_path / ".app_key").exists()


def test_marker_without_key_refuses_to_mint_a_key_file(monkeypatch, tmp_path):
    ss = _fresh_secret_storage(monkeypatch, tmp_path, None)
    from wings import keyholder
    monkeypatch.setattr(keyholder, "APP_KEY", None)
    monkeypatch.setenv("WINGS_KEY_FROM_ENV", "1")
    with pytest.raises(RuntimeError):
        ss.encrypt("x")
    assert ss.decrypt("enc:whatever") == ""  # degrades, never creates a key
    assert not (tmp_path / ".app_key").exists()


def test_keyholder_with_key_only_adds_when_held(monkeypatch):
    from wings import keyholder
    monkeypatch.setattr(keyholder, "APP_KEY", None)
    assert keyholder.with_key({"A": "1"}) == {"A": "1"}
    monkeypatch.setattr(keyholder, "APP_KEY", "k")
    assert keyholder.with_key({"A": "1"}) == {"A": "1", "ODYSSEUS_APP_KEY": "k"}


def _db_with_secret(tmp_path, key: bytes):
    import sqlite3
    conn = sqlite3.connect(tmp_path / "app.db")
    conn.execute("CREATE TABLE model_endpoints (id TEXT, api_key TEXT)")
    conn.execute("INSERT INTO model_endpoints VALUES ('e', ?)", ("enc:" + Fernet(key).encrypt(b"sk").decode(),))
    conn.commit()
    conn.close()


def test_preflight_rejects_key_that_does_not_decrypt_stored_data(tmp_path):
    _db_with_secret(tmp_path, Fernet.generate_key())
    r = _preflight({"ODYSSEUS_APP_KEY": Fernet.generate_key().decode()}, tmp_path)
    assert r.returncode == 78 and "does not decrypt data already stored" in r.stderr


def test_preflight_accepts_key_that_decrypts_stored_data(tmp_path):
    key = Fernet.generate_key()
    _db_with_secret(tmp_path, key)
    r = _preflight({"ODYSSEUS_APP_KEY": key.decode()}, tmp_path)
    assert r.returncode == 0, r.stderr


def test_preflight_keeps_pending_upstream_schedules_once(tmp_path):
    import sqlite3
    db = tmp_path / "scheduled_emails.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE scheduled_emails (id TEXT, status TEXT, odysseus_kind TEXT)")
    conn.execute("INSERT INTO scheduled_emails VALUES ('h','pending','scheduled'),('a','agent_draft','agent_draft'),('s','sent','scheduled')")
    conn.commit(); conn.close()
    r = _preflight({"ODYSSEUS_APP_KEY": Fernet.generate_key().decode()}, tmp_path)
    assert r.returncode == 0, r.stderr
    conn = sqlite3.connect(db)
    flags = dict(conn.execute("SELECT id, wings_human_approved FROM scheduled_emails"))
    assert flags == {"h": 1, "a": 0, "s": 0}


def test_runtime_flag_column_approves_nothing(gate, tmp_path):
    import sqlite3
    conn = sqlite3.connect(tmp_path / "s.db")
    conn.execute("CREATE TABLE scheduled_emails (id TEXT, status TEXT, odysseus_kind TEXT)")
    conn.execute("INSERT INTO scheduled_emails VALUES ('x','pending','scheduled')")
    conn.commit()
    gate.ensure_scheduled_flag(conn)
    assert not gate.scheduled_row_approved(conn, "x")


def test_scrub_keeps_the_key_marker():
    from wings.envscrub import scrubbed_environ
    out = scrubbed_environ({"WINGS_KEY_FROM_ENV": "1", "OPENAI_API_KEY": "x", "PATH": "/b"}, keep=())
    assert out == {"WINGS_KEY_FROM_ENV": "1", "PATH": "/b"}


def test_preflight_quarantines_a_stray_key_file(tmp_path):
    key = Fernet.generate_key()
    _db_with_secret(tmp_path, key)
    (tmp_path / ".app_key").write_bytes(Fernet.generate_key())  # minted by a key-less child
    r = _preflight({"ODYSSEUS_APP_KEY": key.decode()}, tmp_path)
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / ".app_key").exists()
    assert any(p.name.startswith(".app_key.stray-") for p in tmp_path.iterdir())


def test_preflight_still_refuses_a_legacy_key_file(tmp_path):
    legacy = Fernet.generate_key()
    _db_with_secret(tmp_path, legacy)
    (tmp_path / ".app_key").write_bytes(legacy)
    r = _preflight({"ODYSSEUS_APP_KEY": Fernet.generate_key().decode()}, tmp_path)
    assert r.returncode == 78 and "adopt-key.sh" in r.stderr
    assert (tmp_path / ".app_key").exists()


def test_preflight_accept_key_loss_override(tmp_path):
    _db_with_secret(tmp_path, Fernet.generate_key())
    r = _preflight({"ODYSSEUS_APP_KEY": Fernet.generate_key().decode(), "WINGS_ACCEPT_KEY_LOSS": "1"}, tmp_path)
    assert r.returncode == 0 and "WINGS_ACCEPT_KEY_LOSS" in r.stderr


def test_group_participant_template_limits_apply(tmp_path, monkeypatch):
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path))
    from wings import agent_tools
    (tmp_path / "presets.json").write_text(json.dumps({
        "custom": {"enabled": False},
        "user_templates": [{"id": "t1", "name": "Reader", "enabled_tools": []}],
    }))
    denied = agent_tools.agent_denied_tools(None, "t1")
    assert {"web_search", "bash", "manage_calendar"} <= denied
    assert "ui_control" not in denied and "ask_user" not in denied
    assert agent_tools.agent_denied_tools(None, "unknown") == set()


def test_email_family_does_not_grant_contact_editing():
    from wings import agent_tools
    contract = agent_tools._contract_families()
    email_only = agent_tools.allowed_tool_names(["email"], contract, [])
    assert "manage_contact" not in email_only
    assert "manage_contact" in agent_tools.allowed_tool_names(["email", "contacts"], contract, [])

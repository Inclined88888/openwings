"""OPENWINGS_* setting names: the startup shim (wings/names.py), the preflight
and secret handling under the new names, and the .env migration in
scripts/lib.sh (migrate_env, setting)."""
from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from wings import names

OLD = names.OLD  # the internal prefix, spelled once in wings/names.py
LIB = Path("/wings-scripts/lib.sh")


# ------------------------------------------------------------------ shim

def test_apply_moves_openwings_names_to_internal_names():
    env = {"OPENWINGS_APP_KEY": "k", "OPENWINGS_ADMIN_USER": "admin", "PATH": "/bin"}
    moved = names.apply(env)
    assert env == {OLD + "APP_KEY": "k", OLD + "ADMIN_USER": "admin", "PATH": "/bin"}
    assert sorted(moved) == [OLD + "ADMIN_USER", OLD + "APP_KEY"]


def test_apply_prefers_the_openwings_value_and_is_idempotent():
    env = {"OPENWINGS_TOOL_APPROVAL_GATE": "1", OLD + "TOOL_APPROVAL_GATE": "0"}
    names.apply(env)
    assert env == {OLD + "TOOL_APPROVAL_GATE": "1"}
    assert names.apply(env) == [] and env == {OLD + "TOOL_APPROVAL_GATE": "1"}


def test_apply_keeps_values_byte_for_byte():
    value = "abc=def#x'y\"z== \té"
    env = {"OPENWINGS_ADMIN_PASSWORD": value}
    names.apply(env)
    assert env[OLD + "ADMIN_PASSWORD"] == value


def test_apply_never_maps_installer_only_names():
    env = {"OPENWINGS_NO_OPEN": "1", "OPENWINGS_DIR": "/x", "OPENWINGS_REF": "main", "OPENWINGS_VERBOSE": "1"}
    names.apply(env)
    assert env == {"OPENWINGS_NO_OPEN": "1", "OPENWINGS_DIR": "/x", "OPENWINGS_REF": "main", "OPENWINGS_VERBOSE": "1"}


def test_name_helpers_round_trip():
    assert names.internal_name("OPENWINGS_APP_KEY") == OLD + "APP_KEY"
    assert names.public_name(OLD + "APP_KEY") == "OPENWINGS_APP_KEY"
    assert names.internal_name("WINGS_RUNNER_TOKEN") == "WINGS_RUNNER_TOKEN"


def test_entrypoint_runner_execs_with_mapped_environment():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENWINGS_", OLD))}
    env["OPENWINGS_WORKSPACE_DEFAULT"] = "/workspaces/x"
    out = subprocess.run([sys.executable, "/app/wings/names.py", "env"], env=env,
                         capture_output=True, text=True, check=True).stdout
    lines = out.splitlines()
    assert OLD + "WORKSPACE_DEFAULT=/workspaces/x" in lines
    assert not any(line.startswith("OPENWINGS_") for line in lines)


def test_entrypoint_script_runs_preflight_then_base_entrypoint():
    text = Path("/app/wings/entrypoint.sh").read_text()
    assert text.index("preflight.py") < text.index("/usr/local/bin/entrypoint.sh")
    assert OLD not in text and OLD[:-1].lower() not in text.lower()


def _clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENWINGS_", OLD))}
    env.update(extra)
    return env


def test_every_python_process_maps_the_names_itself():
    # sitecustomize (wings/site_names.py): also `docker compose exec openwings python ...`
    out = subprocess.run(
        [sys.executable, "-c", "import os; print(sorted(k for k in os.environ if k.startswith(('OPENWINGS_', %r))))" % OLD],
        env=_clean_env(OPENWINGS_APP_KEY="k", OPENWINGS_NO_OPEN="1"), capture_output=True, text=True, check=True, cwd="/tmp",
    ).stdout
    assert out.strip() == str(sorted([OLD + "APP_KEY", "OPENWINGS_NO_OPEN"]))


def test_wings_package_maps_names_without_sitecustomize():
    # python -S skips sitecustomize; importing wings (python -m wings.<tool>) maps them.
    code = "import os, wings; print(os.environ.get(%r), 'OPENWINGS_APP_KEY' in os.environ)" % (OLD + "APP_KEY")
    out = subprocess.run([sys.executable, "-S", "-c", code], env=_clean_env(OPENWINGS_APP_KEY="k2"),
                         capture_output=True, text=True, check=True, cwd="/app").stdout
    assert out.split() == ["k2", "False"]


def test_site_names_skips_the_same_installer_names():
    env = _clean_env(**{n: "1" for n in names.HOST_ONLY})
    out = subprocess.run([sys.executable, "-c", "import os; print(' '.join(sorted(k for k in os.environ if k.startswith('OPENWINGS_'))))"],
                         env=env, capture_output=True, text=True, check=True, cwd="/tmp").stdout
    assert out.split() == sorted(names.HOST_ONLY)
    assert {"OPENWINGS_DOCKER_WAIT", "OPENWINGS_COLIMA_WAIT", "OPENWINGS_COLIMA_STOP_LIMIT"} <= names.HOST_ONLY


def test_image_names_the_app_user_and_drops_unused_upstream_files():
    low = OLD[:-1].lower()
    assert low not in Path("/etc/passwd").read_text().lower()
    assert low not in Path("/etc/group").read_text().lower()
    users = {line.split(":")[2]: line.split(":")[0] for line in Path("/etc/passwd").read_text().splitlines() if line}
    assert users.get("1000", "openwings") == "openwings"  # or made at start by wings/entrypoint.sh
    app = Path("/app")
    assert not [p.name for p in app.iterdir() if low in p.name.lower()]
    assert not (app / "plans").exists() and not (app / "static/js/MODULE_SUMMARY.md").exists()
    assert (app / "scripts/openwings-backup").resolve().is_file()
    for f in (".env", ".env.example"):
        assert low not in (app / f).read_text().lower()
    assert [p.name for p in (app / "scripts").iterdir() if low in p.name.lower() and "-" not in p.name and p.name != low] == []


def test_public_text_rewrites_internal_names_for_people():
    word = OLD[:-1].capitalize()
    low = OLD[:-1].lower()
    line = (f"Tool index prewarm disabled ({OLD}TOOL_INDEX_PREWARM=0); lane unavailable for "
            f"{low}_memories, {low}_rag and {low}_tool_index; {word} started")
    out = names.public_text(line)
    assert "OPENWINGS_TOOL_INDEX_PREWARM=0" in out
    assert low not in out.lower()
    assert names.public_text("nothing to change") == "nothing to change"


def test_output_filter_rewrites_log_records():
    old_factory = logging.getLogRecordFactory()
    try:
        logging.setLogRecordFactory(logging.LogRecord)
        names.install_output_filter()
        rec = logging.getLogger("x").makeRecord("x", logging.INFO, __file__, 1, "set %s=1", (OLD + "FOO",), None)
        assert rec.getMessage() == "set OPENWINGS_FOO=1"
    finally:
        logging.setLogRecordFactory(old_factory)
        for s in ("stdout", "stderr"):
            stream = getattr(sys, s)
            if isinstance(stream, names._Stream):
                setattr(sys, s, stream._stream)


# ------------------------------------------------------- key and secrets

def _preflight(env, data_dir):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("OPENWINGS_", OLD))}
    e.update(env)
    e["OPENWINGS_DATA_DIR"] = str(data_dir)
    return subprocess.run([sys.executable, "/app/wings/preflight.py"], env=e, capture_output=True, text=True)


def test_preflight_reads_openwings_app_key(tmp_path):
    r = _preflight({"OPENWINGS_APP_KEY": Fernet.generate_key().decode()}, tmp_path)
    assert r.returncode == 0, r.stderr


def test_preflight_messages_name_only_openwings(tmp_path):
    r = _preflight({"OPENWINGS_APP_KEY": "nope"}, tmp_path)
    assert r.returncode == 78 and "OPENWINGS_APP_KEY is not a valid Fernet key" in r.stderr
    assert OLD not in r.stderr
    r = _preflight({}, tmp_path)
    assert "OPENWINGS_APP_KEY is not set" in r.stderr and OLD not in r.stderr


def test_preflight_still_accepts_the_internal_name(tmp_path):
    r = _preflight({OLD + "APP_KEY": Fernet.generate_key().decode()}, tmp_path)
    assert r.returncode == 0, r.stderr


def test_child_processes_see_the_openwings_names():
    from wings import envscrub
    env = {OLD + "TOOL_APPROVAL_GATE": "1", "OPENWINGS_X": "2", "LANG": "C"}
    assert envscrub.scrubbed_environ(env) == {"OPENWINGS_TOOL_APPROVAL_GATE": "1", "OPENWINGS_X": "2", "LANG": "C"}


def test_shells_get_the_openwings_names():
    env = {OLD + "WORKSPACE_DEFAULT": "/w", "PATH": "/bin"}
    assert names.public_environ(env) == {"OPENWINGS_WORKSPACE_DEFAULT": "/w", "PATH": "/bin"}


def test_secrets_are_scrubbed_under_both_names():
    from wings import envscrub
    env = {"OPENWINGS_APP_KEY": "k", "OPENWINGS_ADMIN_PASSWORD": "p",
           OLD + "APP_KEY": "k", OLD + "ADMIN_PASSWORD": "p", "LANG": "C"}
    assert envscrub.scrubbed_environ(env) == {"LANG": "C"}


def test_serve_moves_the_key_out_of_the_environment(monkeypatch):
    from wings import keyholder, serve
    import uvicorn
    monkeypatch.setenv("OPENWINGS_APP_KEY", "the-key")
    monkeypatch.setenv("OPENWINGS_ADMIN_PASSWORD", "pw")
    monkeypatch.setattr(uvicorn, "main", lambda: None)
    monkeypatch.setattr(sys, "argv", ["x"])
    stdout, stderr = sys.stdout, sys.stderr
    try:
        serve.main()
        assert keyholder.APP_KEY == "the-key"
        for k in ("OPENWINGS_APP_KEY", OLD + "APP_KEY", "OPENWINGS_ADMIN_PASSWORD", OLD + "ADMIN_PASSWORD"):
            assert k not in os.environ
    finally:
        sys.stdout, sys.stderr = stdout, stderr
        keyholder.APP_KEY = None
        logging.setLogRecordFactory(logging.LogRecord)


# --------------------------------------------------------- .env migration

needs_lib = pytest.mark.skipif(not LIB.exists(), reason="scripts/ not mounted (run tests/unit/run.sh)")


def _bash(root: Path, script: str):
    return subprocess.run(["bash", "-c", f'. "{root}/scripts/lib.sh"; {script}'],
                          capture_output=True, text=True, cwd=root)


@pytest.fixture
def install(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(LIB, tmp_path / "scripts" / "lib.sh")
    return tmp_path


OLD_ENV = (
    "# settings\n"
    f"{OLD}APP_KEY=abc=def#x'y\"z==\n"
    f"{OLD}ADMIN_USER=admin\n"
    f"  export {OLD}ADMIN_PASSWORD=\"p w=#1\"\n"
    f"# 1 = start even though {OLD}APP_KEY cannot decrypt\n"
    f"# {OLD}INPROCESS_TASKS=1\n"
    f"# {OLD}BASE_IMAGE=ghcr.io/{OLD[:-1].lower()}-dev/{OLD[:-1].lower()}:1@sha256:0\n"
    f"{OLD}ALLOW_PRIVATE_CALDAV=1\n"
    f"# {OLD}APP_KEY replaced by adopt-key.sh on 2026; previous value: oldkey==\n"
    "SEARXNG_SECRET=s\n"
)


@needs_lib
def test_migrate_env_renames_keys_and_keeps_values(install):
    env = install / ".env"
    env.write_text(OLD_ENV)
    r = _bash(install, "migrate_env")
    assert r.returncode == 0, r.stderr
    text = env.read_text()
    assert OLD not in text and OLD[:-1].lower() not in text.lower()
    assert "OPENWINGS_APP_KEY=abc=def#x'y\"z==\n" in text
    assert "  export OPENWINGS_ADMIN_PASSWORD=\"p w=#1\"\n" in text
    assert "OPENWINGS_ADMIN_USER=admin\n" in text and "OPENWINGS_ALLOW_PRIVATE_CALDAV=1\n" in text
    assert "# OPENWINGS_INPROCESS_TASKS=1\n" in text and "# OPENWINGS_BASE_IMAGE=\n" in text
    assert "previous value: oldkey==\n" in text and "SEARXNG_SECRET=s\n" in text
    # every active value is byte for byte the same, under the new name
    old_values = [l.split("=", 1)[1] for l in OLD_ENV.splitlines() if l.lstrip().startswith((OLD, "export"))]
    new_values = [l.split("=", 1)[1] for l in text.splitlines() if "OPENWINGS_" in l and not l.startswith("#")]
    assert old_values == new_values
    bak = install / ".env.bak"
    assert bak.read_text() == OLD_ENV
    assert stat.S_IMODE(bak.stat().st_mode) == 0o600 and stat.S_IMODE(env.stat().st_mode) == 0o600


@needs_lib
def test_migrate_env_is_idempotent_and_keeps_the_first_backup(install):
    env = install / ".env"
    env.write_text(OLD_ENV)
    assert _bash(install, "migrate_env").returncode == 0
    once = env.read_bytes()
    r = _bash(install, "migrate_env")
    assert r.returncode == 0 and r.stdout == ""
    assert env.read_bytes() == once
    assert sorted(p.name for p in install.iterdir() if p.name.startswith(".env")) == [".env", ".env.bak"]


@needs_lib
def test_migrate_env_drops_a_duplicate_with_the_same_value(install):
    env = install / ".env"
    env.write_text(f"OPENWINGS_ADMIN_USER=me\n{OLD}ADMIN_USER=me\n")
    assert _bash(install, "migrate_env").returncode == 0
    assert env.read_text() == "OPENWINGS_ADMIN_USER=me\n"


@needs_lib
def test_migrate_env_refuses_conflicting_values(install):
    env = install / ".env"
    original = f"OPENWINGS_APP_KEY=new\n{OLD}APP_KEY=old\n"
    env.write_text(original)
    r = _bash(install, "migrate_env")
    assert r.returncode != 0 and "OPENWINGS_APP_KEY" in r.stderr and OLD not in r.stderr
    assert env.read_text() == original and not (install / ".env.bak").exists()


def _backups(install):
    return sorted(p.name for p in install.iterdir() if p.name.startswith(".env.bak"))


@needs_lib
def test_migrate_env_renames_setting_names_in_comments_and_leaves_other_words(install):
    low = OLD[:-1].lower()
    env = install / ".env"
    own = (f"# my note: {low} mirror settings\n"
           f"# see https://example.com/{low}/docs and {OLD[:-1]} too\n")
    env.write_text(
        own
        + f"  # {OLD}BASE_IMAGE=ghcr.io/{low}-dev/{low}:1@sha256:0\n"
        + f"# 1 = start even though {OLD}APP_KEY cannot decrypt\n"
        + "OPENWINGS_APP_KEY=k\n"
    )
    r = _bash(install, "migrate_env")
    assert r.returncode == 0, r.stderr
    text = env.read_text()
    assert text.startswith(own), text  # a person's own words are not changed
    assert "# OPENWINGS_BASE_IMAGE=\n" in text and "# 1 = start even though OPENWINGS_APP_KEY cannot decrypt\n" in text
    assert OLD not in text and "OPENWINGS_APP_KEY=k\n" in text
    once = env.read_bytes()
    for _ in range(3):
        r = _bash(install, "migrate_env")
        assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    assert env.read_bytes() == once and _backups(install) == [".env.bak"]


@needs_lib
def test_migrate_env_with_only_own_comments_naming_upstream_changes_nothing(install):
    low = OLD[:-1].lower()
    env = install / ".env"
    original = f"# {low} mirror\nOPENWINGS_APP_KEY=k\n"
    env.write_text(original)
    r = _bash(install, "migrate_env")
    assert r.returncode == 0 and r.stdout == "" and env.read_text() == original and _backups(install) == []


@needs_lib
def test_migrate_env_leaves_values_that_name_upstream_alone_without_a_backup(install):
    low = OLD[:-1].lower()
    env = install / ".env"
    original = f"OPENWINGS_BASE_IMAGE=example/{low}-base:1\n"
    env.write_text(original)
    r = _bash(install, "migrate_env")
    assert r.returncode == 0 and r.stdout == "" and env.read_text() == original and _backups(install) == []


@needs_lib
def test_migrate_env_prefers_a_filled_earlier_value_over_an_empty_new_one(install):
    env = install / ".env"
    env.write_text(f"OPENWINGS_APP_KEY=\n{OLD}APP_KEY=abc==\nOPENWINGS_ADMIN_USER=me\n{OLD}ADMIN_USER=\n")
    r = _bash(install, "migrate_env")
    assert r.returncode == 0, r.stderr
    assert env.read_text() == "OPENWINGS_APP_KEY=abc==\nOPENWINGS_ADMIN_USER=me\n"


@needs_lib
def test_migrate_env_follows_references_inside_values(install):
    env = install / ".env"
    env.write_text(f"{OLD}ADMIN_USER=me\nGREETING=hi ${{{OLD}ADMIN_USER}} ${OLD}ADMIN_USER\nLIT='${{{OLD}ADMIN_USER}}'\n")
    assert _bash(install, "migrate_env").returncode == 0
    assert env.read_text() == ("OPENWINGS_ADMIN_USER=me\nGREETING=hi ${OPENWINGS_ADMIN_USER} $OPENWINGS_ADMIN_USER\n"
                               f"LIT='${{{OLD}ADMIN_USER}}'\n")


@needs_lib
def test_migrate_env_and_env_set_keep_a_symlinked_env_a_link(install):
    real = install / "private" / "real.env"
    real.parent.mkdir()
    real.write_text(f"{OLD}APP_KEY=k\n")
    (install / ".env").symlink_to(Path("private") / "real.env")
    assert _bash(install, "migrate_env && env_set APP_PORT 7099").returncode == 0
    assert (install / ".env").is_symlink()
    assert real.read_text() == "OPENWINGS_APP_KEY=k\nAPP_PORT=7099\n"
    assert stat.S_IMODE(real.stat().st_mode) == 0o600


@needs_lib
def test_scrub_log_rewords_an_existing_install_log(install):
    low = OLD[:-1].lower()
    log = install / "install.log"
    log.write_text(f"Building wings/{low}:1.0.3-w1\nload ghcr.io/{low}-dev/{low}@sha256:1\n{OLD}APP_KEY is not set\n")
    log.chmod(0o600)
    assert _bash(install, f'scrub_log "{log}"').returncode == 0
    text = log.read_text()
    assert low not in text.lower() and "Building openwings:1.0.3-w1" in text and "OPENWINGS_APP_KEY" in text
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


@needs_lib
def test_setting_reads_new_then_earlier_name(install):
    env = install / ".env"
    env.write_text(f"{OLD}ADMIN_PASSWORD=old-pw\n")
    assert _bash(install, "setting ADMIN_PASSWORD").stdout == "old-pw"
    env.write_text(f"OPENWINGS_ADMIN_PASSWORD=new-pw\n{OLD}ADMIN_PASSWORD=old-pw\n")
    assert _bash(install, "setting ADMIN_PASSWORD").stdout == "new-pw"


@needs_lib
def test_migrate_env_without_old_keys_changes_nothing(install):
    env = install / ".env"
    env.write_text("OPENWINGS_APP_KEY=k\n")
    r = _bash(install, "migrate_env")
    assert r.returncode == 0 and r.stdout == "" and not (install / ".env.bak").exists()


@needs_lib
def test_build_output_filter_hides_the_base_image_path(install):
    low = OLD[:-1].lower()
    r = subprocess.run(["bash", "-c", f'. "{install}/scripts/lib.sh"; public_output'],
                       input=f"load metadata for ghcr.io/{low}-dev/{low}:1.0.3@sha256:ab\n{OLD}X\n",
                       capture_output=True, text=True)
    assert low not in r.stdout.lower() and "OPENWINGS_X" in r.stdout


# ------------------------------------------------------------ app names

def test_moved_legacy_session_cookie_never_outlives_the_browser_session():
    from starlette.responses import Response
    from routes.auth_routes import LEGACY_SESSION_COOKIE, SESSION_COOKIE, move_legacy_session_cookie
    resp = Response()
    move_legacy_session_cookie(resp, "tok", secure=True)
    cookies = resp.headers.getlist("set-cookie")
    new = [c for c in cookies if c.startswith(SESSION_COOKIE + "=")]
    gone = [c for c in cookies if c.startswith(LEGACY_SESSION_COOKIE + "=")]
    assert len(new) == 1 and new[0].startswith(f"{SESSION_COOKIE}=tok;")
    low = new[0].lower()
    assert "max-age" not in low and "expires" not in low  # a browser-session cookie
    assert "httponly" in low and "secure" in low and "samesite=lax" in low and "path=/" in low
    assert len(gone) == 1 and "max-age=0" in gone[0].lower()


def test_preflight_reads_changes_still_in_the_write_ahead_log(tmp_path):
    """The only encrypted value sits in app.db-wal (the app was stopped without
    a checkpoint): a key that cannot decrypt it is caught, the right one passes,
    and the data folder is left exactly as it was."""
    import sqlite3
    from wings import preflight
    right, wrong = Fernet.generate_key(), Fernet.generate_key()
    data = tmp_path / "data"
    snap = tmp_path / "snap"
    data.mkdir(); snap.mkdir()
    db = data / "app.db"
    c = sqlite3.connect(db)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA wal_autocheckpoint=0")
    c.execute("CREATE TABLE model_endpoints (id INTEGER, api_key TEXT)")
    c.commit()
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.execute("INSERT INTO model_endpoints VALUES (1, ?)", ("enc:" + Fernet(right).encrypt(b"sk-1").decode(),))
    c.commit()
    for f in ("app.db", "app.db-wal"):
        shutil.copy(data / f, snap / f)
    c.close()
    for f in ("app.db", "app.db-wal"):
        shutil.copy(snap / f, data / f)
    (data / "app.db-shm").unlink(missing_ok=True)
    assert (data / "app.db-wal").stat().st_size > 0
    plain = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    assert plain.execute("SELECT count(*) FROM model_endpoints").fetchone()[0] == 0  # what an immutable read sees
    plain.close()
    before = {p.name: p.read_bytes() for p in data.iterdir()}
    assert preflight._key_fails_existing_data(data, wrong) == "model_endpoints.api_key"
    assert preflight._key_fails_existing_data(data, right) == ""
    assert preflight._key_fails_existing_data(data, right, require_sample=True) == ""
    assert {p.name: p.read_bytes() for p in data.iterdir()} == before


def test_session_cookie_reads_both_names():
    from routes.auth_routes import LEGACY_SESSION_COOKIE, SESSION_COOKIE, session_token
    assert SESSION_COOKIE == "openwings_session"
    assert session_token({SESSION_COOKIE: "a", LEGACY_SESSION_COOKIE: "b"}) == "a"
    assert session_token({LEGACY_SESSION_COOKIE: "b"}) == "b"
    assert session_token({}) is None


def test_agent_bundles_ship_under_openwings_names():
    from routes.codex_routes import _bundle_name
    low = OLD[:-1].lower()
    assert _bundle_name(f"skills/{low}/scripts/{low}_api.py") == "skills/openwings/scripts/openwings_api.py"
    assert _bundle_name(f'os.environ.get("{OLD}URL") # {low.capitalize()}') == 'os.environ.get("OPENWINGS_URL") # OpenWings'

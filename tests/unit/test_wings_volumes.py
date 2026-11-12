"""The data volume copy to the new names (scripts/lib.sh migrate_volumes,
scripts/migrate-volumes.sh), the order of steps in setup.sh, and the checks
that keep an earlier install's data readable. Docker is replaced by
tests/unit/fake_docker.py, which keeps volumes as folders."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from wings import names

OLD = names.OLD
LOW = OLD[:-1].lower()
SCRIPTS = Path("/wings-scripts")
FAKE = Path(__file__).with_name("fake_docker.py")
PROJECT = "t"
REQUIRED = ("data", "ssh", "logs")

pytestmark = pytest.mark.skipif(not (SCRIPTS / "lib.sh").exists(), reason="scripts/ not mounted (run tests/unit/run.sh)")


def old(key):
    return f"{PROJECT}_{LOW}-{key}"


NEW = {"data": "app-data", "ssh": "app-ssh", "logs": "app-logs", "hf-cache": "hf-cache", "local": "app-local"}


def new(key):
    return f"{PROJECT}_{NEW[key]}"


class Stack:
    def __init__(self, root: Path):
        self.root = root
        self.state = root / "docker-state"
        self.vols = self.state / "volumes"
        self.vols.mkdir(parents=True)
        (root / "scripts").mkdir()
        for f in ("lib.sh", "setup.sh", "migrate-volumes.sh"):
            shutil.copy(SCRIPTS / f, root / "scripts" / f)
        bindir = root / "bin"
        bindir.mkdir()
        (bindir / "docker").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        (bindir / "docker").chmod(0o755)
        self.env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", FAKE_DOCKER_STATE=str(self.state))
        (root / ".env").write_text(f"COMPOSE_PROJECT_NAME={PROJECT}\nOPENWINGS_APP_KEY=k\n")

    # -- docker state
    def volume(self, name, labels=None, files=None):
        d = self.vols / name / "data"
        d.mkdir(parents=True)
        (self.vols / name / "labels.json").write_text(json.dumps(labels or {}))
        for rel, content in (files or {}).items():
            p = d / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content) if isinstance(content, bytes) else p.write_text(content)
        return d

    def compose_volume(self, key, files=None, **labels):
        lab = {"com.docker.compose.project": PROJECT, "com.docker.compose.volume": key}
        lab.update(labels)
        return self.volume(f"{PROJECT}_{key}", lab, files)

    def data(self, name):
        return self.vols / name / "data"

    def labels(self, name):
        return json.loads((self.vols / name / "labels.json").read_text())

    def exists(self, name):
        return (self.vols / name).exists()

    def containers(self):
        p = self.state / "containers.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def app(self, volumes, running=True):
        c = self.containers()
        c["app1"] = {"labels": {"com.docker.compose.project": PROJECT, "com.docker.compose.service": "openwings"},
                     "volumes": volumes, "running": running, "image": "openwings:test"}
        (self.state / "containers.json").write_text(json.dumps(c))

    def calls(self):
        p = self.state / "calls.log"
        return p.read_text().splitlines() if p.exists() else []

    def clear_calls(self):
        (self.state / "calls.log").unlink(missing_ok=True)

    # -- running scripts
    def bash(self, script, **env):
        e = dict(self.env, **env)
        return subprocess.run(["bash", "-c", f'set -euo pipefail; export MAIN_PID=$$; . "{self.root}/scripts/lib.sh"; {script}'],
                              capture_output=True, text=True, cwd=self.root, env=e)

    def script(self, name, *args, **env):
        e = dict(self.env, **env)
        return subprocess.run(["bash", str(self.root / "scripts" / name), *args],
                              capture_output=True, text=True, cwd=self.root, env=e)

    def envfile(self):
        return (self.root / ".env").read_text()


def wal_db(path: Path):
    """An SQLite database whose last row is only in its write-ahead log (as
    after a stop without a checkpoint)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA wal_autocheckpoint=0")
    c.execute("CREATE TABLE t (v TEXT)")
    c.execute("INSERT INTO t VALUES ('in-wal')")
    c.commit()
    snap = path.parent / "snap"
    snap.mkdir()
    for suffix in ("", "-wal"):
        shutil.copy(str(path) + suffix, snap / (path.name + suffix))
    c.close()
    for suffix in ("", "-wal"):
        shutil.copy(snap / (path.name + suffix), str(path) + suffix)
    Path(str(path) + "-shm").unlink(missing_ok=True)
    shutil.rmtree(snap)
    assert Path(str(path) + "-wal").stat().st_size > 0


@pytest.fixture
def stack(tmp_path):
    s = Stack(tmp_path)
    s.compose_volume(f"{LOW}-data", {"uploads/a.txt": "a", ".app_key": "x"})
    wal_db(s.data(old("data")) / "app.db")
    s.compose_volume(f"{LOW}-ssh", {"id_ed25519": "key"})
    s.compose_volume(f"{LOW}-logs", {"app.log": "log"})
    s.compose_volume(f"{LOW}-hf-cache", {"models/m.bin": b"\0" * 2048})
    s.compose_volume(f"{LOW}-local", {"engines/e": "e"})
    s.app([old(k) for k in ("data", "ssh", "logs", "hf-cache", "local")])
    return s


def tree(d: Path):
    return {str(p.relative_to(d)): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


def originals(stack):
    return {k: tree(stack.data(old(k))) for k in NEW}


# ------------------------------------------------------------- the copy

def test_copy_marks_volumes_finished_only_after_every_copy(stack):
    before = originals(stack)
    r = stack.bash("migrate_volumes")
    assert r.returncode == 0, r.stderr
    for k in NEW:
        got = tree(stack.data(new(k)))
        record = got.pop(".openwings-migrated").decode().splitlines()
        assert got == before[k] and tree(stack.data(old(k))) == before[k]
        assert record[0] == old(k) and len(record[1].split()) == 3
        assert stack.labels(new(k))["org.openwings.migrated-from"] == old(k)
        assert stack.labels(new(k))["com.docker.compose.project"] == PROJECT
    assert "WINGS_VOLUMES_MIGRATED=1" in stack.envfile()
    assert stack.containers()["app1"]["running"] is False  # the caller starts the new app
    # The WAL-only row was copied and the check read it.
    c = sqlite3.connect(stack.data(new("data")) / "app.db")
    assert c.execute("SELECT v FROM t").fetchall() == [("in-wal",)]
    # Every copy finished before the first was marked.
    calls = stack.calls()
    last_copy = max(i for i, l in enumerate(calls) if "openwings-volume-copy" in l)
    first_mark = min(i for i, l in enumerate(calls) if l.startswith("run") and "-v t_app-data:/to" in l and "openwings-volume-copy" not in l)
    assert last_copy < first_mark


def test_second_run_does_nothing(stack):
    assert stack.bash("migrate_volumes").returncode == 0
    stack.clear_calls()
    r = stack.bash("migrate_volumes")
    assert r.returncode == 0 and r.stdout == ""
    assert not [l for l in stack.calls() if l.startswith(("run", "stop", "volume create", "volume rm"))]


def test_a_failure_late_in_the_copy_removes_every_new_volume_and_restarts_the_app(stack):
    before = originals(stack)
    r = stack.bash("migrate_volumes", FAKE_FAIL_COPY=new("hf-cache"))
    assert r.returncode != 0 and "unchanged in the previous volumes" in r.stderr
    assert not [k for k in NEW if stack.exists(new(k))]
    assert originals(stack) == before
    assert stack.containers()["app1"]["running"] is True
    assert "WINGS_VOLUMES_MIGRATED" not in stack.envfile()
    # The next run copies everything, including what the old app wrote since.
    (stack.data(old("data")) / "uploads" / "new-after.txt").write_text("written after")
    r = stack.bash("migrate_volumes")
    assert r.returncode == 0, r.stderr
    assert (stack.data(new("data")) / "uploads" / "new-after.txt").read_text() == "written after"


def test_an_interrupted_copy_is_undone(stack):
    before = originals(stack)
    r = stack.bash("migrate_volumes", FAKE_SIGNAL_COPY=new("logs"))
    assert r.returncode == 143, (r.returncode, r.stderr)
    assert not [k for k in NEW if stack.exists(new(k))]
    assert originals(stack) == before
    assert stack.containers()["app1"]["running"] is True
    assert "openwings-volume-copy-t" not in stack.containers()


def test_a_copy_cut_off_without_cleanup_is_copied_again(stack):
    # What a power cut or a crashed Docker VM leaves: a labelled, partial copy.
    stack.compose_volume("app-data", {".openwings-copying": "", "uploads/a.txt": "a"},
                         **{"org.openwings.migrated-from": old("data")})
    stack.app([old(k) for k in ("data", "ssh", "logs", "hf-cache", "local")], running=False)
    r = stack.bash("migrate_volumes")
    assert r.returncode == 0, r.stderr
    assert "unfinished copy" in r.stderr
    got = tree(stack.data(new("data")))
    assert ".openwings-copying" not in got and "app.db" in got and ".app_key" in got


def test_an_unfinished_copy_in_use_is_never_removed(stack):
    stack.compose_volume("app-data", {".openwings-copying": "", "notes.txt": "written by an app on the partial copy"},
                         **{"org.openwings.migrated-from": old("data")})
    stack.app([new("data")], running=False)
    r = stack.bash("migrate_volumes")
    assert r.returncode != 0 and "--recopy" in r.stderr
    assert (stack.data(new("data")) / "notes.txt").exists()


def test_remove_old_refuses_an_unfinished_copy(stack):
    stack.compose_volume("app-data", {".openwings-copying": "", "uploads/a.txt": "a"},
                         **{"org.openwings.migrated-from": old("data")})
    for k in ("ssh", "logs", "hf-cache", "local"):
        stack.compose_volume(NEW[k], **{"org.openwings.migrated-from": old(k)})
    stack.app([new("data")], running=False)
    r = stack.script("migrate-volumes.sh", "--remove-old")
    assert stack.exists(old("data")), r.stderr


def test_a_volume_with_the_new_name_but_no_labels_is_refused(stack):
    stack.volume(new("data"))  # docker run -v t_app-data:/d made it
    r = stack.bash("migrate_volumes")
    assert r.returncode != 0 and "no Compose labels" in r.stderr
    assert stack.containers()["app1"]["running"] is True
    assert not stack.exists(new("ssh")) and stack.labels(new("data")) == {}


def test_copies_deleted_on_purpose_are_not_made_again(stack):
    assert stack.bash("migrate_volumes").returncode == 0
    for k in NEW:  # docker compose down -v
        shutil.rmtree(stack.vols / new(k))
    r = stack.bash("migrate_volumes")
    assert r.returncode == 0
    assert not [k for k in NEW if stack.exists(new(k))]
    assert "--recopy" in r.stderr and old("data") in r.stderr
    # ... unless asked.
    stack.app([], running=False)
    r = stack.script("migrate-volumes.sh", "--recopy")
    assert r.returncode == 0, r.stderr
    assert (stack.data(new("data")) / ".app_key").exists()


def test_remove_old_deletes_only_unchanged_finished_copies(stack):
    assert stack.bash("migrate_volumes").returncode == 0
    stack.app([new(k) for k in NEW])
    # An older OpenWings ran on the earlier data volume after the copy.
    (stack.data(old("data")) / "uploads" / "later.txt").write_text("later")
    # A restore replaced the logs volume (its copy record is gone).
    (stack.data(new("logs")) / ".openwings-migrated").unlink()
    r = stack.script("migrate-volumes.sh", "--remove-old")
    assert r.returncode == 0, r.stderr
    assert stack.exists(old("data")) and "changed after it was copied" in r.stderr
    assert stack.exists(old("logs")) and "no record of the copy" in r.stderr
    assert not stack.exists(old("ssh")) and not stack.exists(old("hf-cache")) and not stack.exists(old("local"))


# ------------------------------------------------------------ setup.sh

def setup_stack(tmp_path):
    s = Stack(tmp_path)
    (tmp_path / ".env").unlink()
    (tmp_path / ".env.example").write_text(f"COMPOSE_PROJECT_NAME={PROJECT}\nOPENWINGS_APP_KEY=\nOPENWINGS_ADMIN_PASSWORD=\n")
    return s


def test_setup_stops_before_making_a_key_for_an_earlier_installs_data(tmp_path):
    s = setup_stack(tmp_path)
    s.compose_volume("app-data", {"app.db": "x"})
    s.compose_volume(f"{LOW}-ssh", {"k": "k"})
    r = s.script("setup.sh", "--no-start")
    assert r.returncode != 0
    assert "adopt-key.sh" in r.stderr and f"docker volume rm {new('data')} {old('ssh')}" in r.stderr
    env = s.envfile()
    assert "OPENWINGS_APP_KEY=\n" in env and "OPENWINGS_ADMIN_PASSWORD=\n" in env
    # Rerunning stops the same way (no key was written in between).
    assert s.script("setup.sh", "--no-start").returncode != 0


def test_setup_makes_a_key_when_there_is_no_earlier_data(tmp_path):
    s = setup_stack(tmp_path)
    s.compose_volume(f"{LOW}-hf-cache", {"m": "m"})  # downloads only: no data to protect
    r = s.script("setup.sh", "--no-start")
    assert r.returncode == 0, r.stderr
    assert len(s.bash("env_get OPENWINGS_APP_KEY").stdout.strip()) == 44


def test_setup_builds_before_it_stops_the_app_for_the_copy(stack):
    r = stack.script("setup.sh", "--no-agents", FAKE_FAIL_BUILD="1")
    assert r.returncode != 0 and "left running" in r.stderr
    assert stack.containers()["app1"]["running"] is True
    assert not [k for k in NEW if stack.exists(new(k))]
    assert not [l for l in stack.calls() if l.startswith("stop")]


def test_setup_order_is_build_copy_start(stack):
    stack.script("setup.sh", "--no-agents")  # the fake app never turns healthy; the order is what counts
    calls = stack.calls()
    build = calls.index("compose build")
    stop = calls.index("stop app1")
    up = next(i for i, l in enumerate(calls) if l.startswith("compose up"))
    assert build < stop < up
    assert (stack.data(new("data")) / ".openwings-migrated").exists()


def test_finished_copies_without_the_record_in_env_are_kept(stack):
    # .env from before the record existed (or put back from a backup).
    assert stack.bash("migrate_volumes").returncode == 0
    (stack.root / ".env").write_text(f"COMPOSE_PROJECT_NAME={PROJECT}\nOPENWINGS_APP_KEY=k\n")
    marker = (stack.data(new("data")) / ".openwings-migrated").read_bytes()
    stack.clear_calls()
    r = stack.bash("migrate_volumes")
    assert r.returncode == 0, r.stderr
    assert not [l for l in stack.calls() if l.startswith(("stop", "volume create", "volume rm"))]
    assert (stack.data(new("data")) / ".openwings-migrated").read_bytes() == marker
    assert "WINGS_VOLUMES_MIGRATED=1" in stack.envfile()

#!/usr/bin/env python3
"""A stand-in for the docker CLI, for testing the volume migration in
scripts/lib.sh without Docker. State lives under $FAKE_DOCKER_STATE:
volumes/<name>/data (contents), volumes/<name>/labels.json, containers.json,
and calls.log (one line per call). `docker run` executes the helper script
with sh on this machine, its mount points rewritten to the volume folders.

Fault injection: FAKE_FAIL_COPY=<volume> makes the copy into that volume fail
(disk full); FAKE_SIGNAL_COPY=<volume> sends SIGTERM to $MAIN_PID while that
copy runs (a closed terminal, Ctrl-C); FAKE_FAIL_BUILD=1 fails
`docker compose build`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

STATE = Path(os.environ["FAKE_DOCKER_STATE"])
VOLS = STATE / "volumes"
IMAGE = "openwings:test"


def log(args):
    with open(STATE / "calls.log", "a") as f:
        f.write(" ".join(args).replace("\n", "\\n") + "\n")


def containers():
    p = STATE / "containers.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save_containers(c):
    (STATE / "containers.json").write_text(json.dumps(c))


def labels(name):
    return json.loads((VOLS / name / "labels.json").read_text())


def opts(args, *with_value):
    """Split args into ({flag: [values]}, positional)."""
    out, pos, i = {}, [], 0
    while i < len(args):
        a = args[i]
        if a.startswith("--") and "=" in a:
            k, v = a.split("=", 1)
            out.setdefault(k, []).append(v)
        elif a in with_value:
            out.setdefault(a, []).append(args[i + 1])
            i += 1
        elif a.startswith("-") and a != "-":
            out.setdefault(a, []).append(True)
        else:
            pos.append(a)
        i += 1
    return out, pos


def label_filters(o):
    res = []
    for f in o.get("--filter", []):
        k, v = f.split("=", 1)
        res.append((k, v))
    return res


def match_labels(lab, filters):
    for k, v in filters:
        if k != "label":
            continue
        lk, _, lv = v.partition("=")
        if lab.get(lk) != lv:
            return False
    return True


def volume(args):
    sub, rest = args[0], args[1:]
    if sub == "ls":
        o, _ = opts(rest, "--filter", "--format")
        f = label_filters(o)
        for p in sorted(VOLS.iterdir()) if VOLS.exists() else []:
            if match_labels(labels(p.name), f):
                print(p.name)
        return 0
    if sub == "inspect":
        o, pos = opts(rest, "--format")
        name = pos[0]
        if not (VOLS / name).exists():
            print(f"Error: no such volume: {name}", file=sys.stderr)
            return 1
        fmt = o.get("--format", [None])[0]
        if fmt:
            m = re.search(r'index \.Labels "([^"]+)"', fmt)
            print(labels(name).get(m.group(1), "") if m else "")
        else:
            print(json.dumps([{"Name": name, "Labels": labels(name)}]))
        return 0
    if sub == "create":
        o, pos = opts(rest, "--label")
        name = pos[0]
        d = VOLS / name
        if not d.exists():  # like Docker: an existing volume is reused as is
            (d / "data").mkdir(parents=True)
            lab = dict(x.split("=", 1) for x in o.get("--label", []))
            (d / "labels.json").write_text(json.dumps(lab))
        print(name)
        return 0
    if sub == "rm":
        for name in rest:
            if any(name in c["volumes"] for c in containers().values()):
                print(f"Error: volume is in use - [{name}]", file=sys.stderr)
                return 1
            if not (VOLS / name).exists():
                return 1
            shutil.rmtree(VOLS / name)
        return 0
    return 1


def ps(args):
    o, _ = opts(args, "--filter", "--format")
    show_all = "-a" in o or "-aq" in o
    f = label_filters(o)
    for cid, c in containers().items():
        if not show_all and not c["running"]:
            continue
        if not match_labels(c["labels"], f):
            continue
        if any(k == "volume" and v not in c["volumes"] for k, v in f):
            continue
        if any(k == "ancestor" for k, v in f):
            continue
        print(cid)
    return 0


def run(args):
    ci = len(args) - 1 - args[::-1].index("-c")
    image, script = args[ci - 1], args[ci + 1]
    o, _ = opts(args[: ci - 1], "--name", "--network", "--entrypoint", "-v", "-e", "-w", "-u")
    name = o.get("--name", [None])[0]
    mounts = {}
    for m in o.get("-v", []):
        vol, path = m.split(":")[:2]
        if not (VOLS / vol).exists():
            # docker run -v creates a missing volume, without labels
            (VOLS / vol / "data").mkdir(parents=True)
            (VOLS / vol / "labels.json").write_text("{}")
        mounts[path] = str(VOLS / vol / "data")
    for path, real in mounts.items():
        script = re.sub(r"(?<![\w/.])" + re.escape(path) + r"(?=[/\s\"')]|$)", real, script)
    target = Path(mounts.get("/to", "")).parent.name if "/to" in mounts else ""
    c = containers()
    if name:
        c[name] = {"labels": {}, "volumes": [m.split(":")[0] for m in o.get("-v", [])], "running": True, "image": image}
        save_containers(c)
    try:
        if target and target == os.environ.get("FAKE_FAIL_COPY"):
            print("cp: error writing: No space left on device", file=sys.stderr)
            return 1
        if target and target == os.environ.get("FAKE_SIGNAL_COPY") and "cp -a" in script:
            # Half the files arrive, then the session is interrupted.
            src = Path(mounts["/from"])
            files = sorted(p for p in src.rglob("*") if p.is_file())
            for p in files[: len(files) // 2]:
                dst = Path(mounts["/to"]) / p.relative_to(src)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dst)
            os.kill(int(os.environ["MAIN_PID"]), signal.SIGTERM)
            return 1
        # Like the docker CLI, the output is passed on as it comes, and a
        # reader that stops early (| head -n 1) makes the command fail.
        r = subprocess.run(["sh", "-c", script], stdout=subprocess.PIPE)
        try:
            for line in r.stdout.splitlines(keepends=True):
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
                time.sleep(0.05)
        except BrokenPipeError:
            print("write /dev/stdout: broken pipe", file=sys.stderr)
            os._exit(1)
        return r.returncode
    finally:
        if name:
            c = containers()
            c.pop(name, None)
            save_containers(c)


def compose(args):
    o, pos = opts(args, "--filter", "--policy")
    if pos[:1] == ["version"]:
        print("2.30.0" if "--short" in o else "Docker Compose version v2.30.0")
        return 0
    if pos[:1] == ["config"]:
        print(IMAGE if "--images" in o else "openwings")
        return 0
    if pos[:1] == ["ps"]:
        print("app1")
        return 0
    if pos[:1] == ["build"] and os.environ.get("FAKE_FAIL_BUILD"):
        print("ERROR: failed to solve: network unreachable", file=sys.stderr)
        return 17
    return 0


def main(argv):
    log(argv)
    cmd, rest = argv[0], argv[1:]
    if cmd == "volume":
        return volume(rest)
    if cmd == "ps":
        return ps(rest)
    if cmd == "run":
        return run(rest)
    if cmd == "compose":
        return compose(rest)
    if cmd == "info":
        print("8589934592" if rest else "ok")
        return 0
    if cmd == "image":
        return 0
    if cmd in ("stop", "start"):
        c = containers()
        if rest[-1] not in c:
            return 1
        c[rest[-1]]["running"] = cmd == "start"
        save_containers(c)
        print(rest[-1])
        return 0
    if cmd == "rm":
        c = containers()
        for n in rest:
            c.pop(n, None)
        save_containers(c)
        return 0
    if cmd == "inspect":
        c = containers()
        cid = rest[-1]
        if cid not in c:
            return 1
        fmt = " ".join(rest[:-1])
        if "Config.Image" in fmt:
            print(c[cid]["image"])
        elif "RestartCount" in fmt:
            print(0)
        elif "Restarting" in fmt:
            print("false")
        else:  # health or status: the stand-in app never becomes healthy
            print("exited")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

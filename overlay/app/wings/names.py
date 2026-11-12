"""OpenWings setting names.

The container is configured only with OPENWINGS_* variables (docker-compose.yml
passes no other names of this family). The upstream code underneath reads its
settings under its own internal names. Every Python process maps them at
start-up (wings/site_names.py, installed as sitecustomize); `apply()` here is
the same mapping for code that runs without it, and is idempotent (importing
the wings package, wings.serve and wings.preflight call it).

    python /app/wings/names.py PROGRAM [ARGS...]
        map the names, then exec PROGRAM with the mapped environment.

It also rewrites the internal names in the app's log lines and console output
(docker logs, the Diagnostics page), so what an administrator reads names only
OPENWINGS_* settings.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from typing import MutableMapping

NEW = "OPENWINGS_"
# The upstream programs' own prefix (internal; never configured directly).
OLD = "ODY" + "SSEUS_"

# OPENWINGS_* names that belong to the installer on the host, never to the app.
# They are not passed into the container; skipped here all the same so a
# stray one can never switch an upstream setting of the same suffix.
HOST_ONLY = frozenset({
    "OPENWINGS_REF",
    "OPENWINGS_DIR",
    "OPENWINGS_VERBOSE",
    "OPENWINGS_NO_OPEN",
    "OPENWINGS_DOCKER_WAIT",
    "OPENWINGS_COLIMA_WAIT",
    "OPENWINGS_COLIMA_STOP_LIMIT",
})


def internal_name(name: str) -> str:
    """OPENWINGS_X -> the internal name the app reads."""
    return OLD + name[len(NEW):] if name.startswith(NEW) else name


def public_name(name: str) -> str:
    """The internal name -> OPENWINGS_X (what .env and the docs call it)."""
    return NEW + name[len(OLD):] if name.startswith(OLD) else name


def apply(environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Move every OPENWINGS_X in ``environ`` (default os.environ) to its
    internal name. An OPENWINGS_X value replaces an internal value already
    there. Returns the internal names that were set."""
    env = os.environ if environ is None else environ
    moved = []
    for name in sorted(k for k in list(env) if k.startswith(NEW) and k not in HOST_ONLY):
        value = env.pop(name)
        target = internal_name(name)
        env[target] = value
        moved.append(target)
    return moved


def public_environ(environ: MutableMapping[str, str] | None = None) -> dict[str, str]:
    """A copy of ``environ`` (default os.environ) with the settings under their
    OPENWINGS_* names, for shells and other processes people look into. A
    Python child maps them back by itself (wings/site_names.py)."""
    env = os.environ if environ is None else environ
    return {public_name(k): v for k, v in env.items()}


# --- Output -----------------------------------------------------------------

_LOWER = OLD.lower().rstrip("_")
_WORD = _LOWER.capitalize()
_REWRITES = (
    (re.compile(r"\b" + OLD + r"(?=[A-Z0-9])"), NEW),
    (re.compile(r"\b" + _LOWER + r"_rag\b"), "documents index"),
    (re.compile(r"\b" + _LOWER + r"_memories\b"), "memories index"),
    (re.compile(r"\b" + _LOWER + r"_tool_index\b"), "tool index"),
    (re.compile(r"\b" + _LOWER + r"-ui\b"), "openwings"),
    (re.compile(r"\b" + _WORD + r"\b"), "OpenWings"),
)


def public_text(text: str) -> str:
    """Rewrite internal names in a line meant for people to read."""
    if not text or (_LOWER not in text.lower()):
        return text
    for pattern, repl in _REWRITES:
        text = pattern.sub(repl, text)
    return text


class _Record(logging.LogRecord):
    def getMessage(self) -> str:  # noqa: N802 (logging API)
        return public_text(super().getMessage())


class _Stream:
    """Text stream wrapper that rewrites internal names on write."""

    def __init__(self, stream):
        self._stream = stream

    def write(self, s):
        return self._stream.write(public_text(s) if isinstance(s, str) else s)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def __getattr__(self, name):
        return getattr(self._stream, name)


def install_output_filter() -> None:
    """Rewrite internal names in every log record and in stdout/stderr."""
    if logging.getLogRecordFactory() is logging.LogRecord:
        logging.setLogRecordFactory(_Record)
    if not isinstance(sys.stdout, _Stream):
        sys.stdout = _Stream(sys.stdout)
    if not isinstance(sys.stderr, _Stream):
        sys.stderr = _Stream(sys.stderr)


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: names.py PROGRAM [ARGS...]", file=sys.stderr)
        return 2
    apply()
    program = argv[0]
    if program.endswith(".sh"):
        os.execv("/bin/sh", ["/bin/sh", *argv])
    os.execvp(program, argv)
    return 127  # not reached


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

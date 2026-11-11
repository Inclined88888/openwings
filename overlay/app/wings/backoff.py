"""Wings model retry policy: rate limits and overloads (patch 0016).

Upstream's streaming model path (every chat and agent reply) retried a
502/503/504/529 once after half a second and never retried a 429 rate
limit, so a busy hosted provider ended the reply with an error. OpenManus
retries with exponential backoff. Wings retries 429 rate limits and 503/529
overloads before any output has been shown, waiting what the provider asks
for when it says ("try again in 20s") and otherwise 2, 4, 8 seconds (1, 2, 4
for overloads), each wait at most 20 seconds, with a little jitter. When the
provider asks for longer than that (an hourly or daily quota), Wings does not
retry at all. The failed stream is closed (releasing the local model slot)
before each wait. Network errors (including upstream's own "Cannot reach"
503s), 502 and 504 keep upstream's handling, and with several models
configured upstream's switch to the next model is unchanged.

  WINGS_MODEL_RETRIES   attempts after the first (default 3, 0 turns it off)
"""
from __future__ import annotations

import os
import random
import re

_MAX_WAIT_S = 20.0
_HINT = re.compile(r"(?:try again in|retry after|retry in|retry-after:?|please wait)\s*", re.IGNORECASE)
_PART = re.compile(
    r"\s*(?:and\s+)?([0-9]+(?:\.[0-9]+)?)\s*"
    r"(milliseconds?|ms|seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d)?(?![a-z])",
    re.IGNORECASE,
)
_UNIT_S = {"ms": 0.001, "mi": 0.001, "s": 1.0, "se": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
# Upstream's own connection failures are reported as 503 too; they are not a
# provider asking us to slow down, so they keep upstream's handling.
_CONNECTION = re.compile(r"^(?:Cannot reach |Upstream \S+ unreachable \(cooldown active\))")


def max_attempts() -> int:
    raw = os.environ.get("WINGS_MODEL_RETRIES", "").strip()
    try:
        value = int(raw) if raw else 3
    except ValueError:
        value = 3
    return max(0, min(value, 6))


def _unit_seconds(unit: str) -> float:
    unit = (unit or "s").lower()
    if unit.startswith("mil") or unit == "ms":
        return 0.001
    if unit.startswith("min") or unit == "m":
        return 60.0
    return _UNIT_S.get(unit[0], 1.0)


def hinted_wait(text: str) -> float | None:
    """Seconds the provider asked us to wait, if its message says so:
    "try again in 20s", "7m12.48s", "1h2m3s", "2 minutes", "retry after 30"."""
    text = str(text or "")
    start = _HINT.search(text)
    if not start:
        return None
    pos, total, found = start.end(), 0.0, False
    while True:
        part = _PART.match(text, pos)
        if not part or part.end() == pos:
            break
        total += float(part.group(1)) * _unit_seconds(part.group(2))
        found, pos = True, part.end()
        if not part.group(2):
            break  # a bare number ("retry after 30") is seconds and ends the hint
    return round(total, 6) if found else None


def is_connection_error(text: str) -> bool:
    return bool(_CONNECTION.search(str(text or "").strip()))


def wait_for(attempt: int, status: int | None, text: str = "", *, rand=random.uniform) -> float | None:
    """Seconds to wait before retry number `attempt` (1, 2, ...), or None
    when retrying cannot help: the provider asked for longer than we wait
    (an hourly or daily quota), or the error was our own connection failure."""
    if is_connection_error(text):
        return None
    hinted = hinted_wait(text)
    if hinted is not None:
        if hinted > _MAX_WAIT_S:
            return None
        wait = hinted
    else:
        base = 2.0 if status == 429 else 1.0
        wait = base * (2 ** max(attempt - 1, 0))
    wait = min(max(wait, 0.2), _MAX_WAIT_S)
    return round(wait * rand(0.9, 1.15), 2)

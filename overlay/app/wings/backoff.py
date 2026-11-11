"""Wings model retry policy: rate limits and overloads (patch 0016).

Upstream's streaming model path (every chat and agent reply) retried a
502/503/504/529 once after half a second and never retried a 429 rate
limit, so a busy hosted provider ended the reply with an error. OpenManus
retries with exponential backoff. Wings retries 429 rate limits and 503/529
overloads before any output has been shown, waiting what the provider asks
for when it says ("try again in 20s") and otherwise 2, 4, 8 seconds (1, 2, 4
for overloads), each wait at most 20 seconds, with a little jitter. Network
errors, 502 and 504 keep upstream's handling, and with several models
configured upstream's switch to the next model is unchanged.

  WINGS_MODEL_RETRIES   attempts after the first (default 3, 0 turns it off)
"""
from __future__ import annotations

import os
import random
import re

_MAX_WAIT_S = 20.0
_HINT = re.compile(
    r"(?:try again in|retry after|retry in|retry-after:?|please wait)\s*([0-9]+(?:\.[0-9]+)?)\s*(ms|milliseconds?|s|secs?|seconds?)?",
    re.IGNORECASE,
)


def max_attempts() -> int:
    raw = os.environ.get("WINGS_MODEL_RETRIES", "").strip()
    try:
        value = int(raw) if raw else 3
    except ValueError:
        value = 3
    return max(0, min(value, 6))


def hinted_wait(text: str) -> float | None:
    """Seconds the provider asked us to wait, if its message says so."""
    match = _HINT.search(str(text or ""))
    if not match:
        return None
    value = float(match.group(1))
    unit = (match.group(2) or "s").lower()
    if unit.startswith("m"):
        value /= 1000.0
    return value if value >= 0 else None


def wait_for(attempt: int, status: int | None, text: str = "", *, rand=random.uniform) -> float:
    """Seconds to wait before retry number `attempt` (1, 2, ...)."""
    hinted = hinted_wait(text)
    if hinted is not None:
        wait = hinted
    else:
        base = 2.0 if status == 429 else 1.0
        wait = base * (2 ** max(attempt - 1, 0))
    wait = min(max(wait, 0.2), _MAX_WAIT_S)
    return round(wait * rand(0.9, 1.15), 2)

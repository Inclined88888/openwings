"""Wings run budget: hard token and spend caps for agent work.

Upstream Odysseus caps an agent run by rounds and tool calls only; nothing
stops it on tokens or money, and scheduled tasks and automatic plan
continuation run with nobody watching. These caps, all off unless set in
.env, end the run cleanly with a visible message:

  WINGS_RUN_MAX_TOKENS   input plus output tokens of one agent run
  WINGS_RUN_MAX_USD      estimated cost of one agent run, in US dollars
  WINGS_DAY_MAX_TOKENS   tokens per person per day (all chats and tasks)
  WINGS_DAY_MAX_USD      estimated cost per person per day

Counting (patch 0013): every model round of an agent run, as upstream
records it in usage_buckets (real usage from the provider when it reports
it, otherwise upstream's estimate), and every plain chat turn. Cost uses
upstream's price table (src/model_pricing.py); endpoints upstream marks as
not cost tracked (local models) count as zero dollars but still count
tokens. Small helper calls (titles, memory extraction) are not counted.
The day follows the container's time zone (TZ).
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import math
import os
import time
from pathlib import Path

logger = logging.getLogger("wings.budget")

_KEEP_DAYS = 35


def _limit(name: str) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw) if raw else 0.0
    except ValueError:
        logger.warning("%s=%r is not a number; ignoring it", name, raw)
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def limits() -> dict:
    return {
        "run_tokens": int(_limit("WINGS_RUN_MAX_TOKENS")),
        "run_usd": _limit("WINGS_RUN_MAX_USD"),
        "day_tokens": int(_limit("WINGS_DAY_MAX_TOKENS")),
        "day_usd": _limit("WINGS_DAY_MAX_USD"),
    }


def any_limit() -> bool:
    return any(limits().values())


def bucket_tokens(bucket: dict) -> int:
    total = 0
    for key in ("input_tokens", "output_tokens"):
        try:
            total += max(int(bucket.get(key) or 0), 0)
        except (TypeError, ValueError):
            pass
    return total


def bucket_cost(bucket: dict) -> float:
    """Estimated USD for one round. Unknown models cost 0 (upstream never
    guesses prices); rounds on endpoints upstream marks as not cost tracked
    cost 0."""
    if bucket.get("endpoint_cost_tracked") is False:
        return 0.0
    try:
        from src.model_pricing import MODEL_PRICING, match_model_key
        key = match_model_key(str(bucket.get("model") or ""), MODEL_PRICING.keys())
        if not key:
            return 0.0
        price_in, price_out = MODEL_PRICING[key]
        cost = (max(int(bucket.get("input_tokens") or 0), 0) * price_in
                + max(int(bucket.get("output_tokens") or 0), 0) * price_out) / 1_000_000
        return cost if math.isfinite(cost) and cost > 0 else 0.0
    except Exception:
        return 0.0


# ------------------------------------------------------------- day ledger
def _ledger_path() -> Path:
    base = Path(os.environ.get("ODYSSEUS_DATA_DIR") or "/app/data") / "wings"
    return base / "usage_ledger.json"


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


@contextlib.contextmanager
def _locked():
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield path
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record(owner: str | None, tokens: int, usd: float) -> None:
    """Add usage to today's total for this person."""
    if tokens <= 0 and usd <= 0:
        return
    who = str(owner or "default")
    day = _today()
    with _locked() as path:
        data = _read(path)
        for old in sorted(data)[:-_KEEP_DAYS]:
            data.pop(old, None)
        entry = data.setdefault(day, {}).setdefault(who, {"tokens": 0, "usd": 0.0})
        entry["tokens"] = int(entry.get("tokens") or 0) + int(tokens)
        entry["usd"] = round(float(entry.get("usd") or 0.0) + float(usd), 6)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)


def today(owner: str | None) -> dict:
    who = str(owner or "default")
    entry = (_read(_ledger_path()).get(_today()) or {}).get(who) or {}
    return {"tokens": int(entry.get("tokens") or 0), "usd": float(entry.get("usd") or 0.0)}


def day_exhausted(owner: str | None) -> str | None:
    """Message when this person's daily cap is used up, else None."""
    lim = limits()
    if not (lim["day_tokens"] or lim["day_usd"]):
        return None
    used = today(owner)
    if lim["day_tokens"] and used["tokens"] >= lim["day_tokens"]:
        return (f"Daily token limit reached ({used['tokens']:,} of {lim['day_tokens']:,} tokens today). "
                "It resets at midnight; an admin can change WINGS_DAY_MAX_TOKENS in .env.")
    if lim["day_usd"] and used["usd"] >= lim["day_usd"]:
        return (f"Daily spend limit reached (about ${used['usd']:.2f} of ${lim['day_usd']:.2f} today). "
                "It resets at midnight; an admin can change WINGS_DAY_MAX_USD in .env.")
    return None


def record_chat_turn(session_id: str | None, metrics: dict) -> None:
    """Plain chat turns (agent runs are recorded round by round instead)."""
    if not isinstance(metrics, dict) or "usage_buckets" in metrics or metrics.get("agent_rounds"):
        return
    tokens = 0
    for key in ("input_tokens", "output_tokens"):
        try:
            tokens += max(int(metrics.get(key) or 0), 0)
        except (TypeError, ValueError):
            pass
    try:
        usd = float(metrics.get("cost_usd") or 0.0)
        usd = usd if math.isfinite(usd) and usd > 0 else 0.0
    except (TypeError, ValueError):
        usd = 0.0
    owner = None
    try:
        from core.database import SessionLocal, Session as DBSession
        db = SessionLocal()
        try:
            row = db.query(DBSession.owner).filter(DBSession.id == session_id).first()
            owner = row[0] if row else None
        finally:
            db.close()
    except Exception:
        pass
    try:
        record(owner, tokens, usd)
    except Exception:
        logger.exception("could not record chat usage")


# ------------------------------------------------------------- run usage
class RunUsage(list):
    """upstream's usage_buckets list for one agent run, counting as it goes.

    Upstream appends one bucket per model round at several places in
    stream_agent_loop; counting in append() covers all of them."""

    def __init__(self, owner: str | None = None):
        super().__init__()
        self.owner = owner
        self.tokens = 0
        self.usd = 0.0

    def append(self, bucket):  # noqa: D401 - list API
        super().append(bucket)
        if not isinstance(bucket, dict):
            return
        tokens, usd = bucket_tokens(bucket), bucket_cost(bucket)
        self.tokens += tokens
        self.usd += usd
        try:
            record(self.owner, tokens, usd)
        except Exception:
            logger.exception("could not record agent usage")

    def exceeded(self) -> str | None:
        """Message when this run, or the person's day, is over a cap."""
        lim = limits()
        if lim["run_tokens"] and self.tokens >= lim["run_tokens"]:
            return (f"Run token limit reached ({self.tokens:,} of {lim['run_tokens']:,} tokens). "
                    "The agent stopped; press Continue to allow another run, or raise WINGS_RUN_MAX_TOKENS.")
        if lim["run_usd"] and self.usd >= lim["run_usd"]:
            return (f"Run spend limit reached (about ${self.usd:.2f} of ${lim['run_usd']:.2f}). "
                    "The agent stopped; press Continue to allow another run, or raise WINGS_RUN_MAX_USD.")
        return day_exhausted(self.owner)

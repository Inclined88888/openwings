"""Wings run budget: hard token and spend caps (patch 0013).

Upstream Odysseus caps an agent run by rounds and tool calls only; nothing
stops it on tokens or money, and scheduled tasks and automatic plan
continuation run with nobody watching. These caps, all off unless set in
.env, end the work cleanly with a visible message:

  WINGS_RUN_MAX_TOKENS   input plus output tokens of one agent run
  WINGS_RUN_MAX_USD      estimated cost of one agent run, in US dollars
  WINGS_DAY_MAX_TOKENS   tokens per person per day (everything they run)
  WINGS_DAY_MAX_USD      estimated cost per person per day

What is counted: every model call, at the point where it reaches the
provider, so no feature can go around it.
  * src/llm_core.py: every streaming call when its stream ends (stream_llm)
    and every non-streaming call when the provider answers (llm_call,
    llm_call_async). That covers chat, agent rounds, the summaries an agent
    writes after its loop, the model tools it calls (ask another model, a
    teacher, another chat), the non-streaming /api/chat route, deep research
    and its scheduled runs, and helper calls such as titles, memory and
    skill drafting.
  * src/clean_agent_preview.py: the compact agent runtime, which calls
    providers itself.
Token counts are the provider's when it sends them, otherwise upstream's
estimate. Cost is the provider's reported cost when it sends one
(OpenRouter does), otherwise upstream's price table (src/model_pricing.py);
endpoints upstream classes as not cost tracked (local, private network,
subscription) cost nothing but still count tokens. Image generation and
speech are not token billed and are not counted.

Whose: the person behind the running request (its server side authority),
else the person a research job was started for, else the owner of the chat
session the call names, else "default".

Run: every call made under one request (an agent run, the child agents and
tools it starts, a teacher run that takes over) counts toward that run. A
deep research job the agent starts runs as its own request: it counts
toward the person's day, not the run. The run caps are checked before
each round's tools run and before each compact round, so a run can go past
a cap by the round in flight (and its closing summary). The day caps are
also checked before every chat message, agent run and research start.
The day follows the container's time zone (TZ).
"""
from __future__ import annotations

import contextlib
import contextvars
import fcntl
import json
import logging
import math
import os
import threading
import time
import weakref
from collections import OrderedDict
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


def _count(value) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(number, 0)


def bucket_tokens(bucket: dict) -> int:
    return _count(bucket.get("input_tokens")) + _count(bucket.get("output_tokens"))


def price(model: str, input_tokens: int, output_tokens: int) -> float:
    """Upstream's price table estimate in USD; 0 for unknown models
    (upstream never guesses prices)."""
    try:
        from src.model_pricing import MODEL_PRICING, match_model_key
        key = match_model_key(str(model or ""), MODEL_PRICING.keys())
        if not key:
            return 0.0
        price_in, price_out = MODEL_PRICING[key]
        cost = (_count(input_tokens) * price_in + _count(output_tokens) * price_out) / 1_000_000
        return cost if math.isfinite(cost) and cost > 0 else 0.0
    except Exception:
        return 0.0


def bucket_cost(bucket: dict) -> float:
    """Estimated USD for one usage record: the provider's reported cost when
    present, else the price table; 0 on endpoints not cost tracked."""
    if bucket.get("endpoint_cost_tracked") is False:
        return 0.0
    reported = _usd(bucket.get("cost_usd"))
    if reported > 0:
        return reported
    return price(bucket.get("model"), bucket.get("input_tokens"), bucket.get("output_tokens"))


def _usd(value) -> float:
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) and number > 0 else 0.0


def call_cost(model: str, url: str, input_tokens: int, output_tokens: int, reported=None) -> float:
    reported_usd = _usd(reported)
    if reported_usd > 0:
        return reported_usd
    try:
        from src.endpoint_resolver import endpoint_cost_tracked
        if not endpoint_cost_tracked(str(url or "")):
            return 0.0
    except Exception:
        pass
    return price(model, input_tokens, output_tokens)


# ------------------------------------------------------------- day ledger
def _who(owner) -> str:
    # Same normalisation as upstream's request authority (casefolded).
    return str(owner or "").strip().casefold() or "default"


def _ledger_path() -> Path:
    base = Path(os.environ.get("ODYSSEUS_DATA_DIR") or "/app/data") / "wings"
    return base / "usage_ledger.json"


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


_THREAD_LOCK = threading.Lock()


@contextlib.contextmanager
def _locked():
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _THREAD_LOCK, open(path.with_suffix(".lock"), "a+") as lock:
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
    who = _who(owner)
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
    entry = (_read(_ledger_path()).get(_today()) or {}).get(_who(owner)) or {}
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


# ------------------------------------------------------------- runs
class _Totals:
    """Shared totals of one request's run (and the child runs it starts)."""

    __slots__ = ("tokens", "usd", "__weakref__")

    def __init__(self):
        self.tokens = 0
        self.usd = 0.0


_RUNS: "weakref.WeakValueDictionary[str, _Totals]" = weakref.WeakValueDictionary()
_RUNS_LOCK = threading.Lock()
# Recent requests' totals stay alive after their run object is gone, so a
# second run under the same request (upstream's teacher takeover runs after
# the student run has ended) continues the same totals instead of starting
# a fresh budget.
_RECENT: "OrderedDict[str, _Totals]" = OrderedDict()
_RECENT_MAX = 256


def _keep(request_id: str, totals: "_Totals") -> None:
    _RECENT[request_id] = totals
    _RECENT.move_to_end(request_id)
    while len(_RECENT) > _RECENT_MAX:
        _RECENT.popitem(last=False)


def _authority():
    try:
        from src.agent_runtime.authority import active_request_authority
        return active_request_authority()
    except Exception:
        return None


class RunUsage(list):
    """upstream's usage_buckets list for one agent run, plus the run's token
    and cost totals. The totals are fed by every model call made under the
    run's request (count() below), not by the buckets, so calls the agent
    loop never turns into a bucket (summaries, model tools) count too; a
    child run under the same request shares its parent's totals."""

    def __init__(self, owner: str | None = None, request_id: str | None = None):
        super().__init__()
        self.owner = owner
        if request_id is None:
            authority = _authority()
            request_id = getattr(authority, "request_id", None)
        totals = None
        if request_id:
            with _RUNS_LOCK:
                totals = _RUNS.get(request_id) or _RECENT.get(request_id)
                if totals is None:
                    totals = _Totals()
                _RUNS[request_id] = totals
                _keep(request_id, totals)
        self._totals = totals or _Totals()

    @property
    def tokens(self) -> int:
        return self._totals.tokens

    @property
    def usd(self) -> float:
        return self._totals.usd

    def add(self, tokens: int, usd: float) -> None:
        self._totals.tokens += max(int(tokens), 0)
        self._totals.usd += max(float(usd), 0.0)

    def exceeded(self) -> str | None:
        """Message when this run, or the person's day, is over a cap."""
        return _run_message(self._totals) or day_exhausted(self.owner)


def _run_message(totals) -> str | None:
    lim = limits()
    if totals is None:
        return None
    if lim["run_tokens"] and totals.tokens >= lim["run_tokens"]:
        return (f"Run token limit reached ({totals.tokens:,} of {lim['run_tokens']:,} tokens). "
                "The agent stopped; press Continue to allow another run, or raise WINGS_RUN_MAX_TOKENS.")
    if lim["run_usd"] and totals.usd >= lim["run_usd"]:
        return (f"Run spend limit reached (about ${totals.usd:.2f} of ${lim['run_usd']:.2f}). "
                "The agent stopped; press Continue to allow another run, or raise WINGS_RUN_MAX_USD.")
    return None


def current_stop() -> str | None:
    """Message when the running request's run, or its person's day, is
    over a cap (for runtimes that hold no RunUsage of their own)."""
    authority = _authority()
    if authority is None:
        return None
    with _RUNS_LOCK:
        rid = getattr(authority, "request_id", "") or ""
        totals = _RUNS.get(rid) or _RECENT.get(rid)
    return _run_message(totals) or day_exhausted(getattr(authority, "owner", None))


# ------------------------------------------------------------- counting
# Whose work this is, for code that runs model calls outside a request's
# authority (a deep research job started from the panel). Set in the route
# before the job's task is created, so the task inherits it.
_OWNER: contextvars.ContextVar = contextvars.ContextVar("wings_budget_owner", default=None)


def set_owner(owner) -> None:
    _OWNER.set(str(owner or "") or None)


_SESSION_OWNERS: "OrderedDict[str, tuple[float, str | None]]" = OrderedDict()
_SESSION_OWNER_TTL_S = 60.0


def _session_owner(session_id) -> str | None:
    sid = str(session_id or "")
    if not sid:
        return None
    cached = _SESSION_OWNERS.get(sid)
    if cached is not None and time.monotonic() - cached[0] < _SESSION_OWNER_TTL_S:
        _SESSION_OWNERS.move_to_end(sid)
        return cached[1]
    owner = None
    try:
        from core.database import SessionLocal, Session as DBSession
        db = SessionLocal()
        try:
            row = db.query(DBSession.owner).filter(DBSession.id == sid).first()
            owner = row[0] if row else None
        finally:
            db.close()
    except Exception:
        return None
    _SESSION_OWNERS[sid] = (time.monotonic(), owner)
    _SESSION_OWNERS.move_to_end(sid)
    while len(_SESSION_OWNERS) > 512:
        _SESSION_OWNERS.popitem(last=False)
    return owner


def count(*, model: str, url: str, input_tokens: int, output_tokens: int, reported_usd=None,
          session_id=None, authority=None) -> None:
    """Count one model call: toward the running request's run and toward
    its person's day. Never raises."""
    try:
        tokens = _count(input_tokens) + _count(output_tokens)
        usd = call_cost(model, url, input_tokens, output_tokens, reported_usd)
        if tokens <= 0 and usd <= 0:
            return
        authority = authority if authority is not None else _authority()
        owner = getattr(authority, "owner", None) or _OWNER.get() or _session_owner(session_id)
        request_id = getattr(authority, "request_id", None)
        if request_id:
            with _RUNS_LOCK:
                totals = _RUNS.get(request_id) or _RECENT.get(request_id)
            if totals is not None:
                totals.tokens += tokens
                totals.usd += usd
        record(owner, tokens, usd)
    except Exception:
        logger.exception("could not count model usage")


def _estimate_messages(messages) -> int:
    try:
        from src.model_context import estimate_tokens
        return int(estimate_tokens(list(messages or [])))
    except Exception:
        try:
            return int(len(json.dumps(messages, default=str)) * 0.3)
        except Exception:
            return 0


def _estimate_text(chars: int) -> int:
    return int(math.ceil(max(chars, 0) * 0.3))


def _usage_counts(usage) -> tuple[int, int, float] | None:
    """(input, output, reported USD) from a provider usage block in any of
    the shapes upstream handles (OpenAI, Anthropic, Responses API)."""
    if not isinstance(usage, dict):
        return None
    keys_in = ("prompt_tokens", "input_tokens")
    keys_out = ("completion_tokens", "output_tokens")
    if not any(k in usage for k in keys_in + keys_out):
        return None
    tokens_in = next((_count(usage[k]) for k in keys_in if k in usage), 0)
    tokens_out = next((_count(usage[k]) for k in keys_out if k in usage), 0)
    return tokens_in, tokens_out, _usd(usage.get("cost_usd") or usage.get("cost"))


def count_response(data, *, url: str, model: str, messages, session_id=None) -> None:
    """Count one non-streaming provider answer (its JSON body)."""
    try:
        if not isinstance(data, dict):
            return
        counts = _usage_counts(data.get("usage"))
        if counts is None and ("prompt_eval_count" in data or "eval_count" in data):
            counts = (_count(data.get("prompt_eval_count")), _count(data.get("eval_count")), 0.0)
        if counts is None:
            body = data.get("choices") or data.get("content") or data.get("message") or ""
            counts = (_estimate_messages(messages), _estimate_text(len(json.dumps(body, default=str))), 0.0)
        count(model=model, url=url, input_tokens=counts[0], output_tokens=counts[1],
              reported_usd=counts[2], session_id=session_id)
    except Exception:
        logger.exception("could not count model usage")


class StreamMeter:
    """Watches one streaming call's chunks (upstream's SSE events) and counts
    the call once when the stream ends, however it ends."""

    def __init__(self, *, url: str, model: str, messages, session_id=None, raw: bool = False):
        self.url, self.model, self.messages, self.session_id = url, model, messages, session_id
        self.raw = raw  # provider SSE lines (compact runtime) instead of upstream's events
        self.authority = _authority()  # the caller's request, captured now
        self.usage = None
        self.chars = 0
        self.done = False

    def observe(self, chunk) -> None:
        try:
            text = chunk if isinstance(chunk, str) else str(chunk or "")
            for line in text.splitlines():
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if not body or body == "[DONE]" or body[0] != "{":
                    continue
                try:
                    event = json.loads(body)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if self.raw:
                    self._observe_provider(event)
                else:
                    self._observe_event(event)
        except Exception:
            pass

    def _observe_event(self, event: dict) -> None:
        if event.get("type") == "usage" and isinstance(event.get("data"), dict):
            counts = _usage_counts(event["data"])
            if counts is not None:
                self.usage = counts
            return
        delta = event.get("delta")
        if isinstance(delta, str):
            self.chars += len(delta)
        elif event.get("type") == "tool_call_delta":
            self.chars += len(str(event.get("arg_delta") or ""))
        elif event.get("type") == "tool_calls" and not self.chars:
            self.chars += len(json.dumps(event.get("calls"), default=str))

    def _observe_provider(self, event: dict) -> None:
        counts = _usage_counts(event.get("usage"))
        if counts is not None and (counts[0] or counts[1]):
            self.usage = counts
        for choice in event.get("choices") or ():
            delta = (choice or {}).get("delta") or {}
            for key in ("content", "reasoning_content", "reasoning"):
                if isinstance(delta.get(key), str):
                    self.chars += len(delta[key])
            for call in delta.get("tool_calls") or ():
                function = (call or {}).get("function") or {}
                self.chars += len(str(function.get("arguments") or "")) + len(str(function.get("name") or ""))

    def finish(self) -> None:
        if self.done:
            return
        self.done = True
        if self.usage is not None:
            tokens_in, tokens_out, reported = self.usage
        elif self.chars:
            tokens_in, tokens_out, reported = _estimate_messages(self.messages), _estimate_text(self.chars), 0.0
        else:
            return  # nothing came back (a refused or failed call)
        count(model=self.model, url=self.url, input_tokens=tokens_in, output_tokens=tokens_out,
              reported_usd=reported, session_id=self.session_id, authority=self.authority)

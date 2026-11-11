"""Unit tests for the Wings token and spend caps (overlay/app/wings/budget.py).
Run with ./tests/unit/run.sh (inside the built image, network off)."""
from __future__ import annotations

import importlib
import json
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, "/app")

API = "https://api.example.com/v1"   # cost tracked
LOCAL = "http://localhost:11434/v1"  # not cost tracked


@pytest.fixture
def budget(monkeypatch, tmp_path):
    for name in ("WINGS_RUN_MAX_TOKENS", "WINGS_RUN_MAX_USD", "WINGS_DAY_MAX_TOKENS", "WINGS_DAY_MAX_USD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path))
    import wings.budget as b
    b = importlib.reload(b)
    monkeypatch.setattr(b, "_authority", lambda: None)
    monkeypatch.setattr(b, "_session_owner", lambda sid: None)
    return b


def _priced_model():
    from src.model_pricing import MODEL_PRICING
    for key, (price_in, price_out) in MODEL_PRICING.items():
        if price_in > 0 and price_out > 0:
            return key, price_in, price_out
    pytest.skip("no priced model in the table")


def _as(budget, monkeypatch, owner, request_id):
    monkeypatch.setattr(budget, "_authority", lambda: SimpleNamespace(owner=owner, request_id=request_id))


def test_off_by_default(budget):
    run = budget.RunUsage("alice")
    budget.count(model="x", url=LOCAL, input_tokens=10_000_000, output_tokens=0)
    assert not budget.any_limit()
    assert run.exceeded() is None
    assert budget.day_exhausted("alice") is None


def test_bad_values_are_ignored(budget, monkeypatch):
    monkeypatch.setenv("WINGS_RUN_MAX_TOKENS", "lots")
    monkeypatch.setenv("WINGS_RUN_MAX_USD", "-3")
    monkeypatch.setenv("WINGS_DAY_MAX_USD", "nan")
    assert budget.limits() == {"run_tokens": 0, "run_usd": 0.0, "day_tokens": 0, "day_usd": 0.0}


def test_every_call_of_a_request_counts_toward_its_run(budget, monkeypatch):
    monkeypatch.setenv("WINGS_RUN_MAX_TOKENS", "1000")
    _as(budget, monkeypatch, "alice", "req-1")
    run = budget.RunUsage("alice")
    run.append({"model": "local", "input_tokens": 999_999})  # buckets are upstream's; not counted twice
    budget.count(model="local", url=LOCAL, input_tokens=400, output_tokens=100)
    assert run.exceeded() is None
    child = budget.RunUsage("alice")        # a child run under the same request shares the totals
    budget.count(model="local", url=LOCAL, input_tokens=450, output_tokens=60)
    message = run.exceeded()
    assert message and "Run token limit" in message and "1,010" in message
    assert child.exceeded() == message and budget.current_stop() == message
    assert list(run) == [{"model": "local", "input_tokens": 999_999}]
    _as(budget, monkeypatch, "alice", "req-2")
    assert budget.RunUsage("alice").exceeded() is None   # a new request starts from zero


def test_run_spend_cap_uses_reported_cost_then_the_price_table(budget, monkeypatch):
    key, price_in, _ = _priced_model()
    monkeypatch.setenv("WINGS_RUN_MAX_USD", "0.5")
    _as(budget, monkeypatch, "alice", "req-usd")
    run = budget.RunUsage("alice")
    budget.count(model="vendor/unpriced-model", url=API, input_tokens=10, output_tokens=10, reported_usd=0.3)
    assert run.exceeded() is None and run.usd == pytest.approx(0.3)
    tokens = int(0.3 * 1_000_000 / price_in) + 1
    budget.count(model=key, url=API, input_tokens=tokens, output_tokens=0)
    assert "Run spend limit" in (run.exceeded() or "")


def test_local_endpoints_and_unknown_models_cost_nothing(budget):
    key, _, _ = _priced_model()
    assert budget.call_cost(key, LOCAL, 10**6, 10**6) == 0.0
    assert budget.call_cost("my-own-finetune-zz9", API, 10**6, 0) == 0.0
    assert budget.call_cost(key, API, 10**6, 0) > 0
    assert budget.bucket_cost({"model": "x", "cost_usd": 0.25}) == 0.25
    assert budget.bucket_tokens({"input_tokens": "12", "output_tokens": None}) == 12


def test_daily_cap_spans_runs_and_people_are_separate(budget, monkeypatch):
    monkeypatch.setenv("WINGS_DAY_MAX_TOKENS", "1000")
    _as(budget, monkeypatch, "Alice", "r1")
    first = budget.RunUsage("alice")
    budget.count(model="m", url=LOCAL, input_tokens=700, output_tokens=0)
    assert first.exceeded() is None
    _as(budget, monkeypatch, "alice", "r2")
    second = budget.RunUsage("alice")
    budget.count(model="m", url=LOCAL, input_tokens=300, output_tokens=0)
    assert "Daily token limit" in (second.exceeded() or "")
    assert "Daily token limit" in (budget.day_exhausted("ALICE") or "")
    assert budget.day_exhausted("bob") is None
    assert budget.today("alice")["tokens"] == 1000


def test_calls_without_a_request_use_the_job_owner_or_the_chat_owner(budget, monkeypatch):
    budget.set_owner("carol")
    budget.count(model="m", url=LOCAL, input_tokens=5, output_tokens=5)
    assert budget.today("carol")["tokens"] == 10
    budget.set_owner(None)
    monkeypatch.setattr(budget, "_session_owner", lambda sid: "dave" if sid == "s1" else None)
    budget.count(model="m", url=LOCAL, input_tokens=7, output_tokens=0, session_id="s1")
    budget.count(model="m", url=LOCAL, input_tokens=3, output_tokens=0)
    assert budget.today("dave")["tokens"] == 7 and budget.today(None)["tokens"] == 3


def test_old_days_are_dropped(budget, monkeypatch):
    days = iter([f"2026-01-{d:02d}" for d in range(1, 32)] + [f"2026-02-{d:02d}" for d in range(1, 20)])
    monkeypatch.setattr(budget, "_today", lambda: next(days))
    for _ in range(45):
        budget.record("alice", 1, 0.0)
    data = json.loads(budget._ledger_path().read_text())
    assert len(data) <= budget._KEEP_DAYS + 1
    assert "2026-01-01" not in data


def test_non_streaming_answers_are_counted(budget):
    budget.set_owner("erin")
    budget.count_response({"usage": {"prompt_tokens": 120, "completion_tokens": 30, "cost": 0.01}},
                          url=API, model="vendor/x", messages=[])
    budget.count_response({"usage": {"input_tokens": 50, "output_tokens": 5}}, url=LOCAL, model="m", messages=[])
    budget.count_response({"prompt_eval_count": 8, "eval_count": 2}, url=LOCAL, model="m", messages=[])
    budget.count_response({"choices": [{"message": {"content": "x" * 100}}]}, url=LOCAL, model="m",
                          messages=[{"role": "user", "content": "hello there"}])
    used = budget.today("erin")
    assert used["usd"] == pytest.approx(0.01)
    assert used["tokens"] > 120 + 30 + 55 + 10


def test_stream_meter_counts_once_with_provider_usage_or_an_estimate(budget):
    budget.set_owner("fay")
    meter = budget.StreamMeter(url=API, model="vendor/x", messages=[{"role": "user", "content": "hi"}])
    meter.observe('data: {"delta": "Hello"}\n\n')
    meter.observe('data: {"type": "usage", "data": {"input_tokens": 40, "output_tokens": 2, "cost_usd": 0.002}}\n\n')
    meter.finish()
    meter.finish()
    assert budget.today("fay") == {"tokens": 42, "usd": pytest.approx(0.002)}
    est = budget.StreamMeter(url=LOCAL, model="m", messages=[{"role": "user", "content": "x" * 400}])
    est.observe('data: {"delta": "' + "y" * 100 + '"}\n\n')
    est.observe('data: {"type": "tool_call_delta", "index": 0, "name": "f", "arg_delta": "{}"}\n\n')
    est.finish()
    assert budget.today("fay")["tokens"] > 42 + 100
    failed = budget.StreamMeter(url=API, model="m", messages=[{"role": "user", "content": "x" * 4000}])
    failed.observe('event: error\ndata: {"error": "rate limited", "status": 429}\n\n')
    failed.finish()   # nothing came back: nothing billed
    assert budget.today("fay")["tokens"] < 42 + 100 + 1200


def test_raw_provider_stream_lines_are_metered(budget):
    budget.set_owner("gus")
    meter = budget.StreamMeter(url=LOCAL, model="m", messages=[], raw=True)
    meter.observe('data: {"choices": [{"delta": {"content": "abc"}}]}')
    meter.observe('data: {"choices": [], "usage": {"prompt_tokens": 90, "completion_tokens": 9}}')
    meter.observe("data: [DONE]")
    meter.finish()
    assert budget.today("gus")["tokens"] == 99


def test_the_meter_counts_toward_the_run_it_was_started_in(budget, monkeypatch):
    monkeypatch.setenv("WINGS_RUN_MAX_TOKENS", "50")
    _as(budget, monkeypatch, "hal", "req-meter")
    run = budget.RunUsage("hal")
    meter = budget.StreamMeter(url=LOCAL, model="m", messages=[])
    monkeypatch.setattr(budget, "_authority", lambda: None)   # finished elsewhere
    meter.observe('data: {"type": "usage", "data": {"input_tokens": 60, "output_tokens": 0}}')
    meter.finish()
    assert "Run token limit" in (run.exceeded() or "")
    assert budget.today("hal")["tokens"] == 60


def test_runs_are_forgotten_when_they_end(budget, monkeypatch):
    import gc
    _as(budget, monkeypatch, "ivy", "req-gone")
    run = budget.RunUsage("ivy")
    assert "req-gone" in budget._RUNS
    del run
    gc.collect()
    assert "req-gone" not in budget._RUNS

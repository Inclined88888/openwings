"""Unit tests for the Wings token and spend caps (overlay/app/wings/budget.py).
Run with ./tests/unit/run.sh (inside the built image, network off)."""
from __future__ import annotations

import importlib
import sys

import pytest

sys.path.insert(0, "/app")


@pytest.fixture
def budget(monkeypatch, tmp_path):
    for name in ("WINGS_RUN_MAX_TOKENS", "WINGS_RUN_MAX_USD", "WINGS_DAY_MAX_TOKENS", "WINGS_DAY_MAX_USD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path))
    import wings.budget as b
    return importlib.reload(b)


def _priced_model():
    from src.model_pricing import MODEL_PRICING
    for key, (price_in, price_out) in MODEL_PRICING.items():
        if price_in > 0 and price_out > 0:
            return key, price_in, price_out
    pytest.skip("no priced model in the table")


def test_off_by_default(budget):
    run = budget.RunUsage("alice")
    run.append({"model": "x", "input_tokens": 10_000_000, "output_tokens": 0})
    assert not budget.any_limit()
    assert run.exceeded() is None
    assert budget.day_exhausted("alice") is None


def test_bad_values_are_ignored(budget, monkeypatch):
    monkeypatch.setenv("WINGS_RUN_MAX_TOKENS", "lots")
    monkeypatch.setenv("WINGS_RUN_MAX_USD", "-3")
    monkeypatch.setenv("WINGS_DAY_MAX_USD", "nan")
    assert budget.limits() == {"run_tokens": 0, "run_usd": 0.0, "day_tokens": 0, "day_usd": 0.0}


def test_run_token_cap_counts_every_round(budget, monkeypatch):
    monkeypatch.setenv("WINGS_RUN_MAX_TOKENS", "1000")
    run = budget.RunUsage("alice")
    run.append({"model": "local", "input_tokens": 400, "output_tokens": 100, "endpoint_cost_tracked": False})
    assert run.exceeded() is None
    run.append({"model": "local", "input_tokens": 450, "output_tokens": 60, "endpoint_cost_tracked": False})
    message = run.exceeded()
    assert message and "Run token limit" in message and "1,010" in message
    assert sum(b["input_tokens"] for b in run) == 850  # still a normal list for upstream


def test_run_spend_cap_uses_the_price_table(budget, monkeypatch):
    key, price_in, price_out = _priced_model()
    monkeypatch.setenv("WINGS_RUN_MAX_USD", "0.5")
    run = budget.RunUsage("alice")
    tokens = int(0.6 * 1_000_000 / price_in) + 1
    run.append({"model": key, "input_tokens": tokens, "output_tokens": 0})
    assert run.usd >= 0.6 * 0.99
    assert "Run spend limit" in (run.exceeded() or "")


def test_untracked_endpoints_and_unknown_models_cost_nothing(budget):
    key, _, _ = _priced_model()
    assert budget.bucket_cost({"model": key, "input_tokens": 10**6, "output_tokens": 10**6,
                               "endpoint_cost_tracked": False}) == 0.0
    assert budget.bucket_cost({"model": "my-own-finetune-zz9", "input_tokens": 10**6, "output_tokens": 0}) == 0.0
    assert budget.bucket_tokens({"input_tokens": "12", "output_tokens": None}) == 12


def test_daily_cap_spans_runs_and_people_are_separate(budget, monkeypatch):
    monkeypatch.setenv("WINGS_DAY_MAX_TOKENS", "1000")
    first = budget.RunUsage("alice")
    first.append({"model": "m", "input_tokens": 700, "output_tokens": 0, "endpoint_cost_tracked": False})
    assert first.exceeded() is None
    second = budget.RunUsage("alice")
    second.append({"model": "m", "input_tokens": 300, "output_tokens": 0, "endpoint_cost_tracked": False})
    assert "Daily token limit" in (second.exceeded() or "")
    assert "Daily token limit" in (budget.day_exhausted("alice") or "")
    assert budget.day_exhausted("bob") is None
    assert budget.today("alice")["tokens"] == 1000


def test_old_days_are_dropped(budget, monkeypatch):
    days = iter([f"2026-01-{d:02d}" for d in range(1, 32)] + [f"2026-02-{d:02d}" for d in range(1, 20)])
    monkeypatch.setattr(budget, "_today", lambda: next(days))
    for _ in range(45):
        budget.record("alice", 1, 0.0)
    import json
    data = json.loads(budget._ledger_path().read_text())
    assert len(data) <= budget._KEEP_DAYS + 1
    assert "2026-01-01" not in data


def test_agent_turns_are_not_counted_twice(budget):
    budget.record_chat_turn("s1", {"input_tokens": 50, "output_tokens": 5, "agent_rounds": 3})
    budget.record_chat_turn("s1", {"input_tokens": 50, "output_tokens": 5, "usage_buckets": []})
    assert budget.today(None)["tokens"] == 0
    budget.record_chat_turn("s1", {"input_tokens": 50, "output_tokens": 5, "cost_usd": "bad"})
    assert budget.today(None) == {"tokens": 55, "usd": 0.0}

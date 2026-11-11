"""Unit tests for the OpenWings model retry policy (overlay/app/wings/backoff.py).
Run with ./tests/unit/run.sh (inside the built image, network off)."""
from __future__ import annotations

import importlib
import sys

import pytest

sys.path.insert(0, "/app")


@pytest.fixture
def backoff(monkeypatch):
    monkeypatch.delenv("WINGS_MODEL_RETRIES", raising=False)
    import wings.backoff as b
    return importlib.reload(b)


def test_attempts_default_and_bounds(backoff, monkeypatch):
    assert backoff.max_attempts() == 3
    monkeypatch.setenv("WINGS_MODEL_RETRIES", "0")
    assert backoff.max_attempts() == 0
    monkeypatch.setenv("WINGS_MODEL_RETRIES", "99")
    assert backoff.max_attempts() == 6
    monkeypatch.setenv("WINGS_MODEL_RETRIES", "lots")
    assert backoff.max_attempts() == 3


def test_reads_the_providers_wait_hint(backoff):
    assert backoff.hinted_wait("Rate limit reached. Please try again in 20s.") == 20
    assert backoff.hinted_wait("Please try again in 350ms") == 0.35
    assert backoff.hinted_wait("retry after 7 seconds") == 7
    assert backoff.hinted_wait("Retry-After: 3") == 3
    assert backoff.hinted_wait("overloaded_error") is None


def test_waits_grow_and_stay_bounded(backoff):
    fixed = lambda lo, hi: 1.0  # noqa: E731 - no jitter
    assert [backoff.wait_for(n, 429, "", rand=fixed) for n in (1, 2, 3)] == [2.0, 4.0, 8.0]
    assert [backoff.wait_for(n, 529, "", rand=fixed) for n in (1, 2, 3)] == [1.0, 2.0, 4.0]
    assert backoff.wait_for(1, 429, "try again in 20s", rand=fixed) == 20.0
    assert backoff.wait_for(1, 429, "try again in 1ms", rand=fixed) == 0.2
    assert backoff.wait_for(6, 429, "", rand=fixed) == 20.0
    for _ in range(50):
        assert 0.18 <= backoff.wait_for(1, 429, "try again in 0.2s") <= 23.0


def test_compound_and_long_hints(backoff):
    fixed = lambda lo, hi: 1.0  # noqa: E731 - no jitter
    assert backoff.hinted_wait("Please try again in 7m12.48s.") == 432.48
    assert backoff.hinted_wait("Try again in 1h2m3s") == 3723
    assert backoff.hinted_wait("try again in 2 minutes") == 120
    assert backoff.hinted_wait("Try again in 3 days.") == 3 * 86400
    # Asked to wait longer than OpenWings ever waits: retrying cannot succeed.
    for text in ("Please try again in 7m12.48s.", "try again in 120s", "Try again in 3 days."):
        assert backoff.wait_for(1, 429, text, rand=fixed) is None


def test_own_connection_failures_are_not_backed_off(backoff):
    assert backoff.wait_for(1, 503, "Cannot reach api.example.com") is None
    assert backoff.wait_for(1, 503, "Upstream api.example.com unreachable (cooldown active)") is None
    assert backoff.wait_for(1, 503, "Service temporarily overloaded", rand=lambda lo, hi: 1.0) == 1.0

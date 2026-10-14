"""Deterministic retry policy and single-flight admission."""

from __future__ import annotations

import random

import pytest

from uxon.domain.host_breaker import BreakerSpec, HostBreaker


def test_transient_threshold_reset_deadline_reservation_and_capped_failure():
    now = [1000.0]
    breaker = HostBreaker(BreakerSpec(trip_after=3, half_open_jitter_pct=0), clock=lambda: now[0])
    assert breaker.should_attempt()
    breaker.mark_inflight()
    assert not breaker.should_attempt()
    breaker.clear_inflight()
    assert breaker.should_attempt()
    for _ in range(2):
        breaker.on_failure()
        assert breaker.state == "closed" and breaker.should_attempt()
    breaker.on_success()
    for _ in range(2):
        breaker.on_failure()
        assert breaker.state == "closed"
    breaker.on_failure()
    assert (breaker.state, breaker.backoff_seconds, breaker.next_attempt_at) == ("open", 30, 1030)
    now[0] = 1029.999
    assert not breaker.should_attempt()
    now[0] = 1030
    assert breaker.should_attempt() and breaker.state == "half_open"
    breaker.mark_inflight()
    assert not breaker.should_attempt()
    breaker.on_failure()
    assert (breaker.state, breaker.backoff_seconds, breaker.next_attempt_at, breaker.in_flight) == (
        "open",
        60,
        1090,
        False,
    )
    now[0] = 1090
    assert breaker.should_attempt()
    breaker.mark_inflight()
    breaker.on_failure()
    assert breaker.backoff_seconds == 60 and breaker.next_attempt_at == 1150
    now[0] = 1150
    assert breaker.should_attempt()
    breaker.mark_inflight()
    breaker.on_success()
    assert (breaker.state, breaker.backoff_seconds, breaker.next_attempt_at, breaker.in_flight) == (
        "closed",
        30,
        0,
        False,
    )
    assert breaker.should_attempt()
    for _ in range(2):
        breaker.on_failure()
        assert breaker.state == "closed"
    breaker.on_failure()
    assert breaker.state == "open" and breaker.next_attempt_at == 1180


@pytest.mark.parametrize("position", [0, 0.5, 1], ids=["lower", "center", "upper"])
def test_jitter_boundaries_apply_to_transient_and_fixed_compatibility(position):
    class BoundaryRandom(random.Random):
        def uniform(self, a, b):
            calls.append((a, b))
            return a + (b - a) * position

    now, calls = [1000.0], []
    breaker = HostBreaker(
        BreakerSpec(trip_after=1, half_open_jitter_pct=25),
        clock=lambda: now[0],
        rng=BoundaryRandom(),
    )
    breaker.on_failure()
    assert calls == [(-0.25, 0.25)]
    assert breaker.next_attempt_at == 1000 + 30 * (0.75 + 0.5 * position)
    for _ in range(3):
        breaker.on_failure(incompatible=True)
        assert calls[-1] == (0.9, 1.1)
        assert breaker.backoff_seconds == 300
        assert breaker.next_attempt_at == pytest.approx(now[0] + 270 + 60 * position)
        assert not breaker.should_attempt()
        now[0] = breaker.next_attempt_at
        assert breaker.should_attempt()
        breaker.mark_inflight()
        assert not breaker.should_attempt()
    breaker.on_success()
    assert breaker.should_attempt() and breaker.state == "closed" and breaker.backoff_seconds == 30

"""Unit tests for :class:`uxon.host_breaker.HostBreaker`.

The breaker is pure stdlib state plus an injectable clock and RNG; we
drive both via test seams so the tests are deterministic and don't
sleep.
"""

from __future__ import annotations

import random
import unittest

from uxon.host_breaker import BreakerSpec, HostBreaker


class FakeClock:
    """Manually-advanced monotonic clock for tests."""

    def __init__(self, t0: float = 1000.0) -> None:
        self.t = t0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _make(
    *,
    spec: BreakerSpec | None = None,
    clock: FakeClock | None = None,
    seed: int = 0,
) -> tuple[HostBreaker, FakeClock]:
    """Build a breaker wired to a deterministic clock and RNG."""
    spec = spec or BreakerSpec()
    clock = clock or FakeClock()
    rng = random.Random(seed)
    return HostBreaker(spec, clock=clock, rng=rng), clock


class ClosedToOpenTransitionTests(unittest.TestCase):
    def test_remains_closed_below_trip_after(self) -> None:
        spec = BreakerSpec(trip_after=3)
        br, _ = _make(spec=spec)
        br.on_failure()
        br.on_failure()
        # Two failures, threshold is 3 — still closed.
        self.assertEqual(br.state, "closed")
        self.assertTrue(br.should_attempt(now=1000.0))

    def test_trips_to_open_on_threshold_failure(self) -> None:
        spec = BreakerSpec(trip_after=3, cap_seconds=60.0, factor=2.0)
        br, clock = _make(spec=spec, seed=1)
        for _ in range(3):
            br.on_failure()
        self.assertEqual(br.state, "open")
        # next_attempt_at sits in the future relative to ``clock``.
        self.assertGreater(br.next_attempt_at, clock())

    def test_open_blocks_should_attempt_until_next_attempt_at(self) -> None:
        spec = BreakerSpec(trip_after=1, cap_seconds=60.0, half_open_jitter_pct=0.0)
        br, clock = _make(spec=spec)
        br.on_failure()
        self.assertEqual(br.state, "open")
        # Halfway through the wait — still blocked.
        clock.advance(br.backoff_seconds / 2)
        self.assertFalse(br.should_attempt())
        # Past the wait — admitted (and promoted to half_open).
        clock.t = br.next_attempt_at + 0.001
        self.assertTrue(br.should_attempt())
        self.assertEqual(br.state, "half_open")


class HalfOpenAdmitsExactlyOneProbeTests(unittest.TestCase):
    def test_second_tick_during_inflight_is_dropped(self) -> None:
        spec = BreakerSpec(trip_after=1, half_open_jitter_pct=0.0)
        br, clock = _make(spec=spec)
        br.on_failure()  # → open
        clock.t = br.next_attempt_at + 0.001
        # First tick: promoted to half_open and admitted.
        self.assertTrue(br.should_attempt())
        self.assertEqual(br.state, "half_open")
        br.mark_inflight()
        # Second tick during the probe: dropped.
        self.assertFalse(br.should_attempt())


class HalfOpenSuccessClosesAndResetsBackoffTests(unittest.TestCase):
    def test_success_in_half_open_resets_to_closed(self) -> None:
        spec = BreakerSpec(trip_after=1, factor=2.0, cap_seconds=60.0)
        br, clock = _make(spec=spec)
        br.on_failure()  # → open
        clock.t = br.next_attempt_at + 0.001
        br.should_attempt()  # → half_open
        br.mark_inflight()
        initial_backoff = br.backoff_seconds
        br.on_success()
        self.assertEqual(br.state, "closed")
        self.assertFalse(br.in_flight)
        self.assertEqual(br.next_attempt_at, 0.0)
        # Backoff reset to the initial seed value (which is
        # cap_seconds/factor for the default spec — 30s here).
        self.assertEqual(br.backoff_seconds, initial_backoff)
        self.assertEqual(br.backoff_seconds, 30.0)


class HalfOpenFailureReturnsToOpenWithDoubledCappedBackoffTests(unittest.TestCase):
    def test_failure_in_half_open_doubles_backoff_capped(self) -> None:
        spec = BreakerSpec(
            trip_after=1,
            factor=2.0,
            cap_seconds=60.0,
            half_open_jitter_pct=0.0,
        )
        br, clock = _make(spec=spec)
        br.on_failure()  # → open with backoff = 30
        first_backoff = br.backoff_seconds
        self.assertEqual(first_backoff, 30.0)

        clock.t = br.next_attempt_at + 0.001
        br.should_attempt()  # → half_open
        br.mark_inflight()
        br.on_failure()  # half_open → open, backoff doubled and capped
        self.assertEqual(br.state, "open")
        # 30 * 2 = 60, capped at 60.
        self.assertEqual(br.backoff_seconds, 60.0)
        self.assertGreaterEqual(br.next_attempt_at, clock() + 60.0 - 1e-9)

        # Another failure on the next half_open round still caps at 60.
        clock.t = br.next_attempt_at + 0.001
        br.should_attempt()
        br.mark_inflight()
        br.on_failure()
        self.assertEqual(br.backoff_seconds, 60.0)


class JitterIsBoundedByConfiguredPercentageTests(unittest.TestCase):
    def test_jitter_stays_within_pct_window(self) -> None:
        # Run many trials with different seeds; verify the deviation
        # of next_attempt_at from the unjittered ideal is always
        # within the configured percentage of the backoff window.
        spec = BreakerSpec(
            trip_after=1,
            factor=2.0,
            cap_seconds=60.0,
            half_open_jitter_pct=25.0,
        )
        for seed in range(50):
            clock = FakeClock(t0=1000.0)
            br = HostBreaker(spec, clock=clock, rng=random.Random(seed))
            br.on_failure()
            # backoff_seconds is 30 for this spec.
            unjittered = clock() + br.backoff_seconds
            deviation = abs(br.next_attempt_at - unjittered)
            max_jitter = br.backoff_seconds * 0.25
            self.assertLessEqual(
                deviation,
                max_jitter + 1e-9,
                f"seed={seed}: deviation {deviation} > max {max_jitter}",
            )

    def test_jitter_is_actually_applied(self) -> None:
        # Sanity: with non-zero jitter and varying seeds we observe at
        # least two distinct next_attempt_at values. Otherwise the
        # jitter math is silently no-op.
        spec = BreakerSpec(trip_after=1, half_open_jitter_pct=25.0)
        seen: set[float] = set()
        for seed in range(20):
            clock = FakeClock(t0=1000.0)
            br = HostBreaker(spec, clock=clock, rng=random.Random(seed))
            br.on_failure()
            seen.add(br.next_attempt_at)
        self.assertGreater(len(seen), 1)


class ShouldAttemptIsFalseBetweenFailureAndNextAttemptAtTests(unittest.TestCase):
    def test_blocked_for_full_backoff_window(self) -> None:
        spec = BreakerSpec(trip_after=1, half_open_jitter_pct=0.0)
        br, clock = _make(spec=spec)
        br.on_failure()
        target = br.next_attempt_at
        # Sample several times across the wait window; all blocked.
        for offset in (0.0, 1.0, 5.0, 10.0, 29.0):
            clock.t = 1000.0 + offset
            if clock() < target:
                self.assertFalse(
                    br.should_attempt(),
                    f"should be blocked at t={clock()} (target={target})",
                )

    def test_unblocks_at_or_after_next_attempt_at(self) -> None:
        spec = BreakerSpec(trip_after=1, half_open_jitter_pct=0.0)
        br, clock = _make(spec=spec)
        br.on_failure()
        clock.t = br.next_attempt_at
        # ``>=`` boundary admits the probe.
        self.assertTrue(br.should_attempt())


class InflightGateTests(unittest.TestCase):
    def test_closed_breaker_blocks_concurrent_probes(self) -> None:
        # Even in ``closed`` state we must not double-fire while the
        # previous probe is still running.
        br, _ = _make()
        self.assertTrue(br.should_attempt(now=1000.0))
        br.mark_inflight()
        self.assertFalse(br.should_attempt(now=1000.0))
        br.clear_inflight()
        self.assertTrue(br.should_attempt(now=1000.0))


if __name__ == "__main__":
    unittest.main()

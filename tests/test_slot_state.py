"""Pure-data tests for :mod:`uxon.tui.slot_state`.

Stage 8 of the multi-host design spec ships the slot-store *types* and
the single pure transition :func:`apply`. The tests here pin down the
contract the later scheduler / selector layers rely on:

* Success advances ``value``, ``last_success_at``, clears
  ``last_error``, resets the failure counter, and appends to the ring.
* Failure preserves ``value`` and ``last_success_at``, populates
  ``last_error``, increments the failure counter, and still appends to
  the ring (so a flaky source's elapsed-time histogram remains useful).
* The ring is bounded — pushing N+1 entries with ring_size=N keeps the
  N most recent.
* ``from_cache`` round-trips on success; on failure the previous
  provenance is preserved.
* :func:`apply` is pure: same inputs → equal output.

These tests deliberately avoid Textual / event-loop wiring; that is
the slot-store stage's whole point — pure data with a pure reducer.
"""

from __future__ import annotations

import unittest

from uxon.tui.slot_state import (
    CadencePolicy,
    SlotResult,
    SlotState,
    Source,
    apply,
)


class ApplySuccessTests(unittest.TestCase):
    def test_first_success_populates_value_and_timestamps(self) -> None:
        prev: SlotState[str] = SlotState()
        r = SlotResult[str](
            value="hello",
            error=None,
            elapsed_ms=42,
            attempted_at=1000.0,
        )
        nxt = apply(prev, r)
        self.assertEqual(nxt.value, "hello")
        self.assertEqual(nxt.last_success_at, 1000.0)
        self.assertEqual(nxt.last_attempt_at, 1000.0)
        self.assertIsNone(nxt.last_error)
        self.assertEqual(nxt.consecutive_failures, 0)
        self.assertEqual(nxt.elapsed_ms_recent, (42,))

    def test_success_clears_prior_error_and_resets_failure_counter(self) -> None:
        prev = SlotState[str](
            value="old",
            last_error="boom",
            consecutive_failures=3,
            elapsed_ms_recent=(10, 20),
        )
        r = SlotResult[str](
            value="fresh",
            error=None,
            elapsed_ms=30,
            attempted_at=500.0,
        )
        nxt = apply(prev, r)
        self.assertEqual(nxt.value, "fresh")
        self.assertIsNone(nxt.last_error)
        self.assertEqual(nxt.consecutive_failures, 0)
        self.assertEqual(nxt.elapsed_ms_recent, (10, 20, 30))


class ApplyFailureTests(unittest.TestCase):
    def test_failure_preserves_value_and_increments_failures(self) -> None:
        prev = SlotState[str](
            value="kept",
            last_success_at=100.0,
            last_attempt_at=100.0,
            consecutive_failures=1,
        )
        r = SlotResult[str](
            value=None,
            error="ssh: connect timeout",
            elapsed_ms=5000,
            attempted_at=200.0,
        )
        nxt = apply(prev, r)
        # Value must NOT be wiped on transient failure.
        self.assertEqual(nxt.value, "kept")
        # Last successful fetch timestamp is preserved …
        self.assertEqual(nxt.last_success_at, 100.0)
        # … but the latest attempt timestamp advances.
        self.assertEqual(nxt.last_attempt_at, 200.0)
        self.assertEqual(nxt.last_error, "ssh: connect timeout")
        self.assertEqual(nxt.consecutive_failures, 2)
        self.assertEqual(nxt.elapsed_ms_recent, (5000,))

    def test_failure_appends_to_ring(self) -> None:
        prev = SlotState[str](elapsed_ms_recent=(1, 2, 3))
        r = SlotResult[str](
            value=None,
            error="x",
            elapsed_ms=4,
            attempted_at=0.0,
        )
        nxt = apply(prev, r)
        self.assertEqual(nxt.elapsed_ms_recent, (1, 2, 3, 4))


class RingBufferTests(unittest.TestCase):
    def test_ring_truncates_at_default_size_of_16(self) -> None:
        state: SlotState[int] = SlotState()
        for i in range(17):
            state = apply(
                state,
                SlotResult[int](
                    value=i,
                    error=None,
                    elapsed_ms=i,
                    attempted_at=float(i),
                ),
            )
        # Default ring_size is 16; the first elapsed_ms (0) must have
        # been evicted, leaving the 16 most-recent entries 1..16.
        self.assertEqual(len(state.elapsed_ms_recent), 16)
        self.assertEqual(state.elapsed_ms_recent[0], 1)
        self.assertEqual(state.elapsed_ms_recent[-1], 16)

    def test_explicit_ring_size_truncates(self) -> None:
        state: SlotState[int] = SlotState()
        for i in range(5):
            state = apply(
                state,
                SlotResult[int](value=i, error=None, elapsed_ms=i, attempted_at=0.0),
                ring_size=3,
            )
        self.assertEqual(state.elapsed_ms_recent, (2, 3, 4))


class FromCacheTests(unittest.TestCase):
    def test_from_cache_round_trips_on_success(self) -> None:
        r = SlotResult[str](
            value="cached",
            error=None,
            elapsed_ms=1,
            attempted_at=0.0,
            from_cache=True,
        )
        nxt = apply(SlotState[str](), r)
        self.assertTrue(nxt.from_cache)

    def test_from_cache_preserved_across_failure(self) -> None:
        prev = SlotState[str](value="old", from_cache=True)
        r = SlotResult[str](
            value=None,
            error="boom",
            elapsed_ms=1,
            attempted_at=0.0,
            from_cache=False,
        )
        nxt = apply(prev, r)
        # The failure didn't produce a fresh value, so the previous
        # cache provenance is still the accurate description of
        # ``value``.
        self.assertTrue(nxt.from_cache)


class PurityTests(unittest.TestCase):
    def test_apply_is_pure_same_inputs_equal_outputs(self) -> None:
        prev = SlotState[str](value="x", consecutive_failures=2)
        r = SlotResult[str](value="y", error=None, elapsed_ms=7, attempted_at=99.0)
        a = apply(prev, r)
        b = apply(prev, r)
        # Frozen dataclasses use value-equality by default.
        self.assertEqual(a, b)
        # And the original ``prev`` was not mutated.
        self.assertEqual(prev.value, "x")
        self.assertEqual(prev.consecutive_failures, 2)


class TypeFixtureSmokeTests(unittest.TestCase):
    """The Source/CadencePolicy types are not yet wired; smoke-test their
    construction so a future scheduler change can rely on the dataclass
    field set staying stable.
    """

    def test_cadence_policy_defaults(self) -> None:
        cp = CadencePolicy(interval=30.0)
        self.assertEqual(cp.interval, 30.0)
        self.assertEqual(cp.jitter_pct, 0.0)
        self.assertEqual(cp.initial_offset, 0.0)
        self.assertIsNone(cp.breaker)
        self.assertTrue(cp.coalesce_missed)

    def test_source_dataclass_fields(self) -> None:
        def fetch(_cfg: object) -> SlotResult[int]:
            return SlotResult[int](value=1, error=None, elapsed_ms=0, attempted_at=0.0)

        s = Source[int](
            id="local",
            fetch=fetch,
            apply=apply,
            cadence=CadencePolicy(interval=30.0),
        )
        self.assertEqual(s.id, "local")
        self.assertTrue(s.kick_on_mount)
        # Round-trip the reducer pointer.
        self.assertIs(s.apply, apply)


if __name__ == "__main__":
    unittest.main()

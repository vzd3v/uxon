"""Slot landings retain known data on failure and bound long-lived history."""

from uxon.tui.slot_state import SlotResult, SlotState, apply


def _projection(state):
    return (
        state.value,
        state.last_success_at,
        state.last_attempt_at,
        state.last_error,
        state.consecutive_failures,
        state.elapsed_ms_recent,
    )


def test_success_failure_recovery_and_successful_empty() -> None:
    state = SlotState[str]()
    snapshots = [state]
    expected_history = [(None, None, None, None, 0, ())]
    for value, error, elapsed, at, expected in (
        ("hello", None, 42, 100.0, ("hello", 100.0, 100.0, None, 0, (42,))),
        (None, "timeout", 50, 200.0, ("hello", 100.0, 200.0, "timeout", 1, (42, 50))),
        (None, "offline", 60, 300.0, ("hello", 100.0, 300.0, "offline", 2, (42, 50, 60))),
        ("fresh", None, 30, 400.0, ("fresh", 400.0, 400.0, None, 0, (42, 50, 60, 30))),
        (None, None, 10, 500.0, (None, 500.0, 500.0, None, 0, (42, 50, 60, 30, 10))),
    ):
        state = apply(
            state, SlotResult(value=value, error=error, elapsed_ms=elapsed, attempted_at=at)
        )
        assert _projection(state) == expected, at
        assert [_projection(s) for s in snapshots] == expected_history
        snapshots.append(state)
        expected_history.append(expected)


def test_history_keeps_newest_samples_for_success_and_failure() -> None:
    state = SlotState[int](elapsed_ms_recent=(1, 2, 3))
    failed = apply(
        state, SlotResult(value=None, error="offline", elapsed_ms=4, attempted_at=1.0), ring_size=3
    )
    assert failed.elapsed_ms_recent == (2, 3, 4)
    recovered = apply(
        failed, SlotResult(value=1, error=None, elapsed_ms=5, attempted_at=2.0), ring_size=3
    )
    assert recovered.elapsed_ms_recent == (3, 4, 5)
    assert state.elapsed_ms_recent == (1, 2, 3)
    oversized = SlotState[int](elapsed_ms_recent=tuple(range(24)))
    bounded = apply(oversized, SlotResult(value=1, error=None, elapsed_ms=24, attempted_at=3.0))
    assert bounded.elapsed_ms_recent == tuple(range(9, 25))
    assert oversized.elapsed_ms_recent == tuple(range(24))

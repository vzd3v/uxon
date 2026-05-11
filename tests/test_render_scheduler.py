"""Unit tests for :class:`uxon.tui.render_scheduler.RenderScheduler`.

These tests exercise the scheduler in isolation with a fake ``App``
that exposes only :meth:`set_timer`. They do not require Textual's
event loop — the timer fires when the test calls ``advance``.
"""

from __future__ import annotations

import unittest
from collections.abc import Callable
from dataclasses import dataclass, field

from uxon.tui.render_scheduler import RenderScheduler


@dataclass
class _FakeTimer:
    delay: float
    callback: Callable[[], None]
    stopped: bool = False

    def stop(self) -> None:
        self.stopped = True


@dataclass
class _FakeApp:
    timers: list[_FakeTimer] = field(default_factory=list)

    def set_timer(self, delay: float, callback: Callable[[], None]) -> _FakeTimer:
        t = _FakeTimer(delay=delay, callback=callback)
        self.timers.append(t)
        return t

    def fire_pending(self) -> int:
        """Fire all not-yet-stopped timers in order. Returns count fired."""
        fired = 0
        for t in list(self.timers):
            if not t.stopped:
                t.stopped = True
                t.callback()
                fired += 1
        return fired


class RenderSchedulerTests(unittest.TestCase):
    def _make(self, debounce_ms: int = 100, max_latency_ms: int = 1000, render_returns: bool = True):
        app = _FakeApp()
        calls: list[frozenset[str]] = []

        def render(kinds: frozenset[str]) -> bool:
            calls.append(kinds)
            return render_returns

        sched = RenderScheduler(
            app,  # type: ignore[arg-type]
            debounce_ms=debounce_ms,
            max_latency_ms=max_latency_ms,
            render=render,
        )
        return sched, app, calls

    def test_first_request_fires_immediately(self) -> None:
        sched, app, calls = self._make()
        sched.request("main_ctx")
        self.assertEqual(calls, [frozenset({"main_ctx"})])
        self.assertEqual(app.timers, [])

    def test_burst_within_window_yields_one_immediate_plus_one_trailing(self) -> None:
        sched, app, calls = self._make()
        sched.request("main_ctx")
        sched.request("remote")
        sched.request("remote")
        # Only the leading edge has fired so far; trailing is pending.
        self.assertEqual(calls, [frozenset({"main_ctx"})])
        # A timer was set for the trailing fire.
        active = [t for t in app.timers if not t.stopped]
        self.assertEqual(len(active), 1)
        # Fire the trailing timer manually.
        active[0].callback()
        self.assertEqual(len(calls), 2)
        # Trailing fire carries both kinds queued during cooldown.
        self.assertEqual(calls[1], frozenset({"remote"}))

    def test_render_false_preserves_dirty(self) -> None:
        sched, app, calls = self._make(render_returns=False)
        sched.request("main_ctx")
        # Render was called but returned False.
        self.assertEqual(calls, [frozenset({"main_ctx"})])
        # Next request should fire immediately again (idle, _last_fire
        # never updated because render returned False).
        sched.request("remote")
        self.assertEqual(len(calls), 2)
        # The second fire's payload includes the still-dirty kind.
        self.assertEqual(calls[1], frozenset({"main_ctx", "remote"}))

    def test_shutdown_cancels_pending_timer(self) -> None:
        sched, app, _ = self._make()
        sched.request("main_ctx")  # leading edge
        sched.request("remote")  # debounced
        active = [t for t in app.timers if not t.stopped]
        self.assertEqual(len(active), 1)
        sched.shutdown()
        self.assertTrue(all(t.stopped for t in app.timers))


if __name__ == "__main__":
    unittest.main()

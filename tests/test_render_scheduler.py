"""Render cadence against monotonic time and deadline-respecting timers."""

from collections.abc import Callable
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from uxon.tui.render_scheduler import RenderScheduler


@dataclass
class _Timer:
    deadline: float
    callback: Callable[[], None]
    stopped: bool = False

    def stop(self) -> None:
        self.stopped = True


@dataclass
class _ClockApp:
    now: float = 10.0
    timers: list[_Timer] = field(default_factory=list)

    def set_timer(self, delay: float, callback: Callable[[], None]) -> _Timer:
        timer = _Timer(self.now + delay, callback)
        self.timers.append(timer)
        return timer

    def advance(self, target: float) -> None:
        fired = 0
        while due := sorted(
            (t for t in self.timers if not t.stopped and t.deadline <= target),
            key=lambda t: t.deadline,
        ):
            fired += 1
            assert fired <= 20, (
                f"timer failed to settle: now={self.now}, target={target}, due={due}"
            )
            timer = due[0]
            self.now = timer.deadline
            timer.stopped = True
            timer.callback()
        self.now = target


def test_leading_trailing_union_and_maximum_latency() -> None:
    app = _ClockApp()
    calls = []

    def render(kinds):
        calls.append((app.now, kinds))
        return True

    scheduler = RenderScheduler(app, debounce_ms=100, max_latency_ms=250, render=render)
    with patch("uxon.tui.render_scheduler.time.monotonic", side_effect=lambda: app.now):
        scheduler.request("main_ctx")
        app.advance(10.02)
        scheduler.request("remote")
        scheduler.request("remote")
        app.advance(10.05)
        scheduler.request("availability")
        app.advance(10.149)
        assert calls == [(10.0, frozenset({"main_ctx"}))]
        app.advance(10.151)
        assert calls[1][0] == pytest.approx(10.15)
        assert calls[1][1] == frozenset({"remote", "availability"})
        app.advance(10.16)
        scheduler.request("main_ctx")
        for at, kind in (
            (10.22, "remote"),
            (10.28, "remote"),
            (10.34, "availability"),
            (10.40, "remote"),
        ):
            app.advance(at)
            scheduler.request(kind)
        assert len(calls) == 2
        app.advance(10.411)
        assert len(calls) == 3
        assert calls[2][0] == pytest.approx(10.41)
        assert calls[2][1] == frozenset({"main_ctx", "remote", "availability"})


def test_rejected_render_keeps_dirty_and_shutdown_cancels_trailing() -> None:
    app = _ClockApp()
    calls = []
    outcomes = iter((False, True))

    def render(kinds):
        calls.append((app.now, kinds))
        return next(outcomes)

    scheduler = RenderScheduler(app, debounce_ms=100, max_latency_ms=250, render=render)
    with patch("uxon.tui.render_scheduler.time.monotonic", side_effect=lambda: app.now):
        scheduler.request("main_ctx")
        app.advance(10.5)
        assert calls == [(10.0, frozenset({"main_ctx"}))]
        scheduler.request("remote")
        assert calls == [
            (10.0, frozenset({"main_ctx"})),
            (10.5, frozenset({"main_ctx", "remote"})),
        ]
        app.advance(10.51)
        scheduler.request("availability")
        scheduler.shutdown()
        app.advance(11.0)
        assert len(calls) == 2

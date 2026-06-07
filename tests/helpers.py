# SPDX-License-Identifier: MIT
"""Shared test helpers.

Test fixtures used by more than one ``test_*.py`` module live here so
sibling test files don't import from each other. ``tests/`` is on
``sys.path`` via pytest rootdir collection (no ``__init__.py``),
matching the existing ``harness`` package convention.
"""

from __future__ import annotations

from uxon.domain.config import Config
from uxon.domain.session import SessionInfo


def make_config(**overrides: object) -> Config:
    base: dict[str, object] = {
        "runtime_user": "",
        "default_launch_mode": "caller",
        "enable_all_users_list": False,
        "launch_user_by_caller": {},
        "session_users": [],
        "allowed_roots": ["/srv/repos"],
        "session_prefix": "uxon-",
        "legacy_session_prefixes": (),
        "enabled_agents": ("claude",),
        "default_agent": "claude",
        "agent_default_args": {"claude": (), "codex": (), "cursor": ()},
        "new_project_root": "/srv/repos",
        "repeat_noninteractive_mode": "fail",
        "tmux_socket_template": "/tmp/uxon-{user}.sock",
        "tui_refresh_interval_seconds": 2.0,
        "git_create_enabled": False,
        "default_git_remote_profile": "",
        "git_remote_profiles": [],
    }
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


def run_off_loop_sync(fn, *, on_success=None, on_error=None, label="action"):
    """Synchronous stand-in for ``UxonApp.run_off_loop`` in non-async tests.

    Production runs ``fn`` (a blocking tmux/ssh/sudo callback) on a worker
    thread and invokes the continuation back on the loop. Tests that drive
    a controller through a fake App have no event loop, so this runs ``fn``
    inline and calls the matching continuation — same end-to-end behaviour,
    same error routing (any exception → ``on_error``).
    """
    try:
        value = fn()
    except Exception as exc:  # noqa: BLE001 — mirror run_off_loop's capture
        if on_error is not None:
            on_error(exc)
        return
    if on_success is not None:
        on_success(value)


class LoopBlockMonitor:
    """Empirical event-loop responsiveness probe (the watchdog layer).

    Schedules a self-rescheduling timer every ``interval`` seconds and
    records the gap between consecutive ticks. A gap much larger than
    ``interval`` means the loop could not service the timer — i.e. some
    callback blocked it. This measures the loop directly, independent of
    *how* it was blocked (subprocess, sleep, CPU loop), complementing the
    subprocess boundary guard.

    Usage::

        mon = LoopBlockMonitor()
        mon.start(app)
        ...drive a blocking action...
        mon.stop()
        assert mon.max_block < 0.25  # loop stayed responsive
    """

    def __init__(self, interval: float = 0.01) -> None:
        self.interval = interval
        self.gaps: list[float] = []
        self._last: float | None = None
        self._timer = None

    def start(self, app) -> None:
        import time

        def beat() -> None:
            now = time.monotonic()
            if self._last is not None:
                self.gaps.append(now - self._last)
            self._last = now
            self._timer = app.set_timer(self.interval, beat)

        self._last = time.monotonic()
        self._timer = app.set_timer(self.interval, beat)

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    @property
    def max_block(self) -> float:
        return max(self.gaps, default=0.0)


def make_session(name: str = "uxon-demo@claude", *, user: str = "u-vz") -> SessionInfo:
    return SessionInfo(
        user=user,
        name=name,
        attached="0",
        windows="1",
        created="2026-05-03T12:00:00+00:00",
        last_attached="2026-05-03T12:30:00+00:00",
        pane_pids=(111,),
        active_pid=111,
        active_cmd="claude",
        active_path="/srv/repos/demo",
    )

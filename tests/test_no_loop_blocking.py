"""The keystroke-swallow bug class, pinned by machine.

Root cause of dropped arrows / multi-press Esc / frozen "new project":
a blocking call (tmux/git/ssh/sudo via ``subprocess``) executed on the
asyncio thread Textual uses to read the keyboard. While it runs the loop
cannot service input and the escape-sequence parser starves.

This module makes the class non-regressable:

* :class:`GuardUnitTests` pins the invariant primitive — ``assert_off_event_loop``
  raises on a thread with a running loop, passes on a worker thread.
* :class:`InteractiveActionsRunOffLoopTests` drives every blocking
  interactive action and asserts its callback ran on a worker thread,
  never on the loop. Each recorder reports ``asyncio.get_running_loop()``
  success; a recorded ``True`` means the action would have frozen the UI.

Run it yourself: ``pytest tests/test_no_loop_blocking.py``. Red means a
blocking call sits on the loop again.
"""

from __future__ import annotations

import asyncio
import threading
import unittest


def _textual_available() -> bool:
    try:
        import textual  # noqa: F401
    except ImportError:
        return False
    return True


def _on_loop() -> bool:
    """True iff called on a thread that is running an asyncio event loop."""
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


class GuardUnitTests(unittest.IsolatedAsyncioTestCase):
    """The guard primitive: the discriminator the whole fix rests on."""

    async def test_raises_on_event_loop_thread(self) -> None:
        from uxon.infra.loop_guard import EventLoopBlockedError, assert_off_event_loop

        with self.assertRaises(EventLoopBlockedError):
            assert_off_event_loop("test blocking call")

    async def test_passes_on_worker_thread(self) -> None:
        from uxon.infra.loop_guard import assert_off_event_loop

        outcome: list[str] = []

        def worker() -> None:
            try:
                assert_off_event_loop("worker call")
                outcome.append("ok")
            except Exception as exc:  # noqa: BLE001 — record any failure
                outcome.append(f"raised:{exc!r}")

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        self.assertEqual(outcome, ["ok"])

    async def test_subprocess_guard_trips_on_loop(self) -> None:
        """With the guard installed, spawning a subprocess on the loop raises."""
        import subprocess

        from uxon.infra.loop_guard import (
            EventLoopBlockedError,
            install_subprocess_guard,
        )

        install_subprocess_guard()  # idempotent — conftest already installed it
        with self.assertRaises(EventLoopBlockedError):
            subprocess.run(["true"], capture_output=True)


def _mk_ctx(**overrides):
    from uxon.tui.context import LaunchRequest, TuiContext
    from uxon.tui.refresh import SourceSpec

    base = dict(
        sessions=[],
        total_cpu="0",
        total_ram="0",
        version="0.12.0",
        cwd="/srv/work",
        cwd_short="work",
        new_project_root="/srv/work",
        existing_projects=[],
        cwd_writable=True,
        current_user="me",
        on_launch_cwd=lambda agent_id, mode_id, target_dir=None: LaunchRequest(
            cmd=("/bin/true",), label="cwd"
        ),
        on_launch_new=lambda n, agent_id, mode_id, g: LaunchRequest(
            cmd=("/bin/true",), label="new"
        ),
        on_launch_existing=lambda n, agent_id, mode_id: LaunchRequest(
            cmd=("/bin/true",), label="existing"
        ),
    )
    base.update(overrides)
    ctx = TuiContext(**base)
    ctx.refresh_sources = [
        SourceSpec(
            name="main_ctx_rebuild",
            fetch=lambda ctx=ctx: ctx,
            cadence_seconds_attr="tui_refresh_interval_seconds",
            kick_on_mount=True,
        )
    ]
    return ctx


def _session(name: str, user: str = "me"):
    from uxon.tui.context import TuiSession

    return TuiSession(
        name=name,
        short=name,
        attached=False,
        pid="1",
        cpu="0",
        ram="-",
        created="",
        last_activity="",
        cmd="",
        path="",
        user=user,
    )


@unittest.skipUnless(_textual_available(), "textual not installed")
class InteractiveActionsRunOffLoopTests(unittest.IsolatedAsyncioTestCase):
    """Every blocking interactive callback must run on a worker, not the loop.

    A recorder appends ``_on_loop()`` when the callback fires. The
    assertion is ``[False]`` — the callback ran off the event-loop
    thread. On the pre-fix code each of these recorded ``[True]``.
    """

    async def _settle(self, pilot, n: int = 4) -> None:
        for _ in range(n):
            await pilot.pause()

    async def test_local_kill_runs_off_loop(self) -> None:
        from uxon.tui.app import UxonApp
        from uxon.tui.widgets.session_list_view import SessionListView

        rec: list[bool] = []
        ctx = _mk_ctx(
            sessions=[_session("s1")],
            on_kill=lambda user, name: rec.append(_on_loop()),
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.kick_refresh()
            await self._settle(pilot)
            table = app.screen.query_one("#sessions-dashboard", SessionListView)
            table.focus()
            table.move_cursor(row=0)
            await pilot.pause()
            await pilot.press("d")  # kill → ConfirmYesNo
            await pilot.pause()
            await pilot.press("y")  # confirm
            await self._settle(pilot)
            self.assertEqual(rec, [False], msg="on_kill ran on the event loop — UI would freeze")

    async def test_kill_all_own_runs_off_loop(self) -> None:
        from uxon.tui.app import UxonApp

        rec: list[bool] = []
        ctx = _mk_ctx(
            sessions=[_session("s1"), _session("s2")],
            on_kill_all=lambda: rec.append(_on_loop()),
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.kick_refresh()
            await self._settle(pilot)
            await pilot.press("D")  # kill-all-own → ConfirmPhrase
            await pilot.pause()
            # ConfirmPhrase requires typing the phrase verbatim, then Enter.
            for ch in "kill-all":
                await pilot.press(ch if ch != "-" else "minus")
            await pilot.press("enter")
            await self._settle(pilot)
            self.assertEqual(rec, [False], msg="on_kill_all ran on the event loop")

    async def test_attach_runs_off_loop(self) -> None:
        from uxon.tui.app import UxonApp
        from uxon.tui.context import LaunchRequest

        rec: list[bool] = []

        def on_attach(user, name):
            rec.append(_on_loop())
            return LaunchRequest(cmd=("/bin/true",), label="attach")

        ctx = _mk_ctx(sessions=[_session("s1")], on_attach=on_attach)
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.kick_refresh()
            await self._settle(pilot)
            # Neutralise the TTY handoff so request_launch doesn't exit the app.
            app.request_launch = lambda req: None  # type: ignore[assignment, method-assign]
            app.screen._launch_flow.attach_session("me", "s1")
            await self._settle(pilot)
            self.assertEqual(rec, [False], msg="on_attach ran on the event loop")

    async def test_remote_attach_runs_off_loop(self) -> None:
        from types import SimpleNamespace

        from uxon.tui.app import UxonApp
        from uxon.tui.context import LaunchRequest

        rec: list[bool] = []

        def on_remote_attach(host_name, user, name):
            rec.append(_on_loop())
            return LaunchRequest(cmd=("/bin/true",), label="rattach")

        ctx = _mk_ctx(on_remote_attach=on_remote_attach)
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.request_launch = lambda req: None  # type: ignore[assignment, method-assign]
            row = SimpleNamespace(host="kris", user="me", name="s1")
            app.screen._launch_flow.attach_row(row)
            await self._settle(pilot)
            self.assertEqual(rec, [False], msg="on_remote_attach (ssh) ran on the event loop")

    async def test_launch_session_probe_runs_off_loop(self) -> None:
        """The 'new project'/launch freeze: the existing-session probe."""
        from uxon.tui.app import UxonApp

        rec: list[bool] = []

        def probe():
            rec.append(_on_loop())
            return ()  # no existing sessions → on_new() path

        new_called: list[bool] = []
        ctx = _mk_ctx(sessions=[_session("s1")])
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.kick_refresh()
            await self._settle(pilot)
            app.screen._launch_flow.maybe_show_session_choice(
                target_dir="/srv/work/proj",
                target_label="proj",
                agent_id="claude",
                mode_id="normal",
                on_new=lambda: new_called.append(True),
                probe=probe,
            )
            await self._settle(pilot)
            self.assertEqual(
                rec, [False], msg="session probe ran on the event loop — launch freeze"
            )
            self.assertEqual(
                new_called, [True], msg="on_new continuation must still run on the loop"
            )

    async def test_launch_cwd_commit_runs_off_loop(self) -> None:
        """The launch *commit* — not only the probe — must run off the loop.

        ``on_launch_cwd`` shells out to tmux (`collect_sessions`) before it
        builds the request; on the loop it froze the UI and, with the guard
        armed, failed the launch with a misleading toast. Drives ``launch_cwd``
        to the LaunchOptionsScreen, then dismisses it with an (agent, mode)
        pick to reach ``commit_primary``.
        """
        from uxon.infra.agents import AgentAvailability
        from uxon.tui.app import UxonApp
        from uxon.tui.context import LaunchRequest
        from uxon.tui.screens.launch_options import LaunchOptionsScreen

        rec: list[bool] = []

        def on_launch_cwd(agent_id, mode_id, target_dir=None):
            rec.append(_on_loop())
            return LaunchRequest(cmd=("/bin/true",), label="cwd")

        # cwd is non-git → workspace probe returns [] → degraded options
        # screen; cwd_writable=True → no sudo probe; empty existing-sessions
        # probe → on_new (commit_primary) fires directly. A seeded agent makes
        # the options screen mount (an empty agent list won't render).
        ctx = _mk_ctx(
            on_launch_cwd=on_launch_cwd,
            on_probe_existing_sessions=lambda *a: (),
            enabled_agents=["claude"],
            agent_availability={"claude": AgentAvailability(status="ok", path="/usr/bin/claude")},
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.kick_refresh()
            await self._settle(pilot)
            app.request_launch = lambda req: None  # type: ignore[assignment, method-assign]
            app.screen._launch_flow.launch_cwd()
            # Worktree-probe worker → _WorktreesProbed → LaunchOptionsScreen.
            opts = None
            for _ in range(10):
                await pilot.pause()
                opts = next(
                    (s for s in app.screen_stack if isinstance(s, LaunchOptionsScreen)), None
                )
                if opts is not None:
                    break
            self.assertIsNotNone(opts, msg="LaunchOptionsScreen was not reached")
            opts.dismiss(("claude", "normal"))
            await self._settle(pilot)
            self.assertEqual(rec, [False], msg="on_launch_cwd commit ran on the event loop")

    async def test_container_probe_runs_off_loop(self) -> None:
        """The container probe/start shell-outs must run off the loop too.

        ``[container].enabled`` gates the launch on ``on_container_gate``,
        which shells out via the bounded-timeout probe. Like the launch
        commit, it runs on the off-loop worker — a hung ``docker inspect``
        must never freeze the UI. Here the launch callback stands in for that
        work and records the thread the container probe would run on (the real
        ``subprocess.run`` boundary is the guard).
        """
        import getpass

        from uxon.infra import container as container_infra
        from uxon.infra.agents import AgentAvailability
        from uxon.tui.app import UxonApp
        from uxon.tui.context import LaunchRequest
        from uxon.tui.screens.launch_options import LaunchOptionsScreen

        rec: list[bool] = []

        def on_launch_cwd(agent_id, mode_id, target_dir=None):
            # Drive the REAL container probe shell-out (the new off-loop work)
            # and record the thread it runs on. ``subprocess.run`` is the
            # guarded boundary; ``["true"]`` exits 0 fast. Probe as the current
            # user so the same-user fast path (no sudo) keeps the test hermetic.
            rec.append(_on_loop())
            container_infra._probe_exit_ok(["true"], getpass.getuser())
            return LaunchRequest(cmd=("/bin/true",), label="cwd")

        ctx = _mk_ctx(
            on_launch_cwd=on_launch_cwd,
            on_probe_existing_sessions=lambda *a: (),
            enabled_agents=["claude"],
            agent_availability={"claude": AgentAvailability(status="ok", path="/usr/bin/claude")},
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.kick_refresh()
            await self._settle(pilot)
            app.request_launch = lambda req: None  # type: ignore[assignment, method-assign]
            app.screen._launch_flow.launch_cwd()
            opts = None
            for _ in range(10):
                await pilot.pause()
                opts = next(
                    (s for s in app.screen_stack if isinstance(s, LaunchOptionsScreen)), None
                )
                if opts is not None:
                    break
            self.assertIsNotNone(opts, msg="LaunchOptionsScreen was not reached")
            opts.dismiss(("claude", "normal"))
            await self._settle(pilot)
            self.assertEqual(rec, [False], msg="container probe ran on the event loop")


@unittest.skipUnless(_textual_available(), "textual not installed")
class LoopStaysResponsiveTests(unittest.IsolatedAsyncioTestCase):
    """Empirical watchdog: a slow action must not stall the keystroke loop.

    Independent of the thread-identity assertions above: a heartbeat timer
    records the gap between ticks while a deliberately slow callback runs.
    The slow callback simulates real tmux/ssh latency with ``time.sleep``.
    Off the loop the heartbeat keeps ticking (gap ≈ interval); on the loop
    (the regression) the sleep would freeze every tick for its full
    duration. This catches blocking by *any* means, not only subprocess.
    """

    async def test_slow_action_does_not_freeze_loop(self) -> None:
        import asyncio as _asyncio
        import time

        from helpers import LoopBlockMonitor

        from uxon.tui.app import UxonApp
        from uxon.tui.widgets.session_list_view import SessionListView

        # Loose backstop, not a tight bound: the thread-identity tests above
        # are the real contract. A generous block/threshold ratio keeps this
        # robust against scheduler/GC hiccups under parallel test load — only
        # a multi-hundred-ms loop stall (the regression) trips it.
        block = 0.8

        ctx = _mk_ctx(
            sessions=[_session("s1")],
            # Simulate the latency of a real tmux/ssh kill.
            on_kill=lambda user, name: time.sleep(block),
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.kick_refresh()
            for _ in range(4):
                await pilot.pause()
            table = app.screen.query_one("#sessions-dashboard", SessionListView)
            table.focus()
            table.move_cursor(row=0)
            await pilot.pause()

            await pilot.press("d")  # kill → confirm
            for _ in range(3):
                await pilot.pause()
            mon = LoopBlockMonitor(interval=0.01)
            mon.start(app)
            await pilot.press("y")  # confirm → off-loop worker sleeps `block`
            # Keep the loop running across the whole blocking window so the
            # heartbeat can tick — if the sleep were on the loop, it could not.
            await _asyncio.sleep(block + 0.2)
            for _ in range(5):
                await pilot.pause()
            mon.stop()

            self.assertGreater(len(mon.gaps), 0, msg="heartbeat never ticked — test is invalid")
            # Off the loop, gaps stay near the 10 ms interval; the regression
            # (sleep on the loop) would freeze one heartbeat for the full
            # `block`. Half of `block` separates the two with wide margin.
            self.assertLess(
                mon.max_block,
                block / 2,
                msg=(
                    f"event loop stalled {mon.max_block:.3f}s during a {block}s action — "
                    "blocking work is back on the loop"
                ),
            )


if __name__ == "__main__":
    unittest.main()

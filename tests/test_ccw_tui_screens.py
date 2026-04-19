"""Pilot tests for textual screens (T8+).

Uses ``App.run_test()`` + ``Pilot`` to drive the TUI in-process. Covers
MainScreen routing, digit-jump guard, kill flow, CallbackError → toast,
refresh re-calls ``on_refresh``.

See ``tests/harness/pty_tui.py`` for end-to-end pty tests.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB = os.path.abspath(os.path.join(_HERE, "..", "lib"))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)


def _textual_available() -> bool:
    try:
        import textual  # noqa: F401
    except ImportError:
        return False
    return True


def _mk_ctx(**overrides):
    from ccw_tui.context import LaunchRequest, TuiContext

    base = dict(
        sessions=[],
        total_cpu="0",
        total_ram="0",
        version="0.12.0",
        cwd="/srv/work",
        cwd_short="work",
        new_project_root="/srv/work",
        existing_projects=[],
        cwd_allowed=True,
        current_user="devagent",
        on_launch_cwd=lambda dsp: LaunchRequest(cmd=("/bin/true",), label="cwd"),
        on_launch_new=lambda n, d, g: LaunchRequest(cmd=("/bin/true",), label="new"),
        on_launch_existing=lambda n, d: LaunchRequest(cmd=("/bin/true",), label="existing"),
    )
    base.update(overrides)
    return TuiContext(**base)


@unittest.skipUnless(_textual_available(), "textual not installed")
class MainScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_q_quits(self) -> None:
        from ccw_tui.app import CcwApp

        app = CcwApp(_mk_ctx())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
        self.assertEqual(app.quit_rc, 0)

    async def test_digit_1_activates_action_cwd(self) -> None:
        from ccw_tui.app import CcwApp

        app = CcwApp(_mk_ctx())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("1")
            await pilot.pause()
        self.assertIsNotNone(app.pending_launch)
        self.assertEqual(app.pending_launch.label, "cwd")

    async def test_digit_jump_guard_fresh_superuser_empty(self) -> None:
        """Empty-superuser state: digit ACTION_COUNT+1 (which would land
        on Settings) must NOT auto-open Settings. Equivalent to the
        legacy ``DigitJumpGuardTests::fresh_superuser_digit_4`` case.
        """
        from ccw_tui.app import CcwApp

        ctx = _mk_ctx(has_sudo=True)
        app = CcwApp(ctx)
        captured: list[str] = []
        async with app.run_test(size=(100, 30)) as pilot:
            orig = app.notify
            def cap(msg, **kw):
                captured.append(str(msg))
                return orig(msg, **kw)
            app.notify = cap
            await pilot.pause()
            await pilot.press("4")
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
        self.assertIsNone(app.pending_launch)
        self.assertTrue(
            any("moves cursor only" in m for m in captured),
            f"expected digit-4 guard toast in {captured!r}",
        )

    async def test_kill_calls_on_kill_callback(self) -> None:
        from ccw_tui.app import CcwApp
        from ccw_tui.context import TuiSession

        kill_calls: list[tuple[str, str]] = []
        refresh_calls = []

        def fake_kill(user: str, name: str) -> None:
            kill_calls.append((user, name))

        def fake_refresh():
            refresh_calls.append(1)
            return _mk_ctx(
                sessions=[],
                on_kill=fake_kill,
                on_refresh=fake_refresh,
            )

        session = TuiSession(
            name="devagent.foo",
            short="foo",
            attached=False,
            pid="1",
            cpu="1.0",
            ram="1M",
            created="1s",
            last_activity="1s",
            cmd="claude",
            path="/srv/work",
            user="devagent",
        )
        ctx = _mk_ctx(
            sessions=[session],
            on_kill=fake_kill,
            on_refresh=fake_refresh,
        )
        app = CcwApp(ctx)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # Focus the session table and press 'd'.
            from ccw_tui.widgets import SessionTable
            t = app.screen.query_one("#sessions-own", SessionTable)
            t.focus()
            t.move_cursor(row=0)
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
        self.assertEqual(kill_calls, [("devagent", "devagent.foo")])

    async def test_refresh_re_calls_on_refresh(self) -> None:
        from ccw_tui.app import CcwApp

        refresh_calls: list[int] = []

        def fake_refresh():
            refresh_calls.append(1)
            return _mk_ctx(on_refresh=fake_refresh)

        ctx = _mk_ctx(on_refresh=fake_refresh)
        app = CcwApp(ctx)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("r")
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
        self.assertEqual(len(refresh_calls), 1)

    async def test_callback_error_renders_toast(self) -> None:
        from ccw_tui.app import CcwApp
        from ccw_tui.context import CallbackError

        def boom_launch(dsp):
            raise CallbackError("nope")

        ctx = _mk_ctx(on_launch_cwd=boom_launch)
        app = CcwApp(ctx)
        captured: list[tuple[str, str]] = []
        async with app.run_test(size=(100, 30)) as pilot:
            orig = app.notify

            def cap(msg, **kw):
                captured.append((str(msg), kw.get("severity", "")))
                return orig(msg, **kw)

            app.notify = cap
            await pilot.pause()
            await pilot.press("1")
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
        errored = [m for m, sev in captured if sev == "error" and "nope" in m]
        self.assertTrue(errored, f"expected error toast in {captured!r}")
        self.assertIsNone(app.pending_launch)


if __name__ == "__main__":
    unittest.main()

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
        on_launch_cwd=lambda agent_id, mode_id: LaunchRequest(cmd=("/bin/true",), label="cwd"),
        on_launch_new=lambda n, agent_id, mode_id, g: LaunchRequest(cmd=("/bin/true",), label="new"),
        on_launch_existing=lambda n, agent_id, mode_id: LaunchRequest(cmd=("/bin/true",), label="existing"),
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
            await pilot.press("1")   # digit-1 → PermissionsScreen
            await pilot.pause()
            await pilot.press("1")   # pick regular → on_launch_cwd(False)
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
            # ConfirmYesNo modal is active — answer y.
            await pilot.press("y")
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

        def boom_launch(agent_id, mode_id):
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
            await pilot.press("1")        # digit-1 → PermissionsScreen
            await pilot.pause()
            await pilot.press("1")        # pick regular → on_launch_cwd(False)
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
        errored = [m for m, sev in captured if sev == "error" and "nope" in m]
        self.assertTrue(errored, f"expected error toast in {captured!r}")
        self.assertIsNone(app.pending_launch)


@unittest.skipUnless(_textual_available(), "textual not installed")
class ConfirmModalTests(unittest.IsolatedAsyncioTestCase):
    async def test_yesno_y_confirms(self) -> None:
        from textual.app import App
        from ccw_tui.screens.confirm import ConfirmYesNo

        class Host(App):
            result = None

            def on_mount(self):
                def done(r):
                    self.result = r
                    self.exit()
                self.push_screen(ConfirmYesNo("Kill foo?"), done)

        app = Host()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
        self.assertTrue(app.result)

    async def test_yesno_n_cancels(self) -> None:
        from textual.app import App
        from ccw_tui.screens.confirm import ConfirmYesNo

        class Host(App):
            result = None

            def on_mount(self):
                def done(r):
                    self.result = r
                    self.exit()
                self.push_screen(ConfirmYesNo("Kill foo?"), done)

        app = Host()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
        self.assertFalse(app.result)

    async def test_phrase_matching_string_confirms(self) -> None:
        from textual.app import App
        from ccw_tui.screens.confirm import ConfirmPhrase

        class Host(App):
            result = None

            def on_mount(self):
                def done(r):
                    self.result = r
                    self.exit()
                self.push_screen(ConfirmPhrase("Danger!", "kill-all"), done)

        app = Host()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.screen.query_one("#confirm-input").focus()
            await pilot.press(*"kill-all")
            await pilot.press("enter")
            await pilot.pause()
        self.assertTrue(app.result)

    async def test_phrase_mismatch_rejects(self) -> None:
        from textual.app import App
        from ccw_tui.screens.confirm import ConfirmPhrase

        class Host(App):
            result = None

            def on_mount(self):
                def done(r):
                    self.result = r
                    self.exit()
                self.push_screen(ConfirmPhrase("Danger!", "kill-all"), done)

        app = Host()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.screen.query_one("#confirm-input").focus()
            await pilot.press(*"nope")
            await pilot.press("enter")
            await pilot.pause()
        self.assertFalse(app.result)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(_textual_available(), "textual not installed")
class PermissionsScreenTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, keys):
        from textual.app import App
        from ccw_tui.screens.permissions import PermissionsScreen

        class Host(App):
            result = "unset"

            def on_mount(self):
                def done(r):
                    self.result = r
                    self.exit()
                self.push_screen(PermissionsScreen(), done)

        app = Host()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            for k in keys:
                await pilot.press(k)
            await pilot.pause()
        return app.result

    async def test_digit_1_returns_regular(self):
        self.assertEqual(await self._run(["1"]), False)

    async def test_digit_2_returns_dsp(self):
        self.assertEqual(await self._run(["2"]), True)

    async def test_escape_returns_none(self):
        self.assertIsNone(await self._run(["escape"]))


@unittest.skipUnless(_textual_available(), "textual not installed")
class NewProjectScreenTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, text, submit_key="enter"):
        from textual.app import App
        from textual.widgets import Input
        from ccw_tui.screens.new_project import NewProjectScreen

        class Host(App):
            result = "unset"

            def on_mount(self):
                def done(r):
                    self.result = r
                    self.exit()
                self.push_screen(NewProjectScreen("/srv/work"), done)

        app = Host()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            inp = app.screen.query_one("#name-input", Input)
            inp.focus()
            if text:
                await pilot.press(*list(text))
            await pilot.press(submit_key)
            await pilot.pause()
        return app.result

    async def test_valid_name_returned(self):
        self.assertEqual(await self._run("foo"), "foo")

    async def test_empty_name_rejected(self):
        result = await self._run("", submit_key="enter")
        self.assertEqual(result, "unset")

    async def test_escape_cancels(self):
        result = await self._run("bar", submit_key="escape")
        self.assertIsNone(result)


@unittest.skipUnless(_textual_available(), "textual not installed")
class GitProfileScreenTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, keys, options, default=""):
        from textual.app import App
        from ccw_tui.screens.git_profile import GitProfileScreen

        class Host(App):
            result = "unset"

            def on_mount(self):
                def done(r):
                    self.result = r
                    self.exit()
                self.push_screen(GitProfileScreen(options, default_profile=default), done)

        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            for k in keys:
                await pilot.press(k)
            await pilot.pause()
        return app.result

    async def test_digit_0_picks_skip(self):
        opts = [("profA", "A"), ("profB", "B")]
        self.assertEqual(await self._run(["0"], opts), "")

    async def test_digit_1_picks_first(self):
        opts = [("profA", "A"), ("profB", "B")]
        self.assertEqual(await self._run(["1"], opts), "profA")

    async def test_escape_cancels(self):
        opts = [("profA", "A")]
        self.assertIsNone(await self._run(["escape"], opts))

    async def test_default_profile_preselected(self):
        opts = [("profA", "A"), ("profB", "B")]
        # Default = profB → cursor starts at index 2 → Enter picks profB.
        self.assertEqual(await self._run(["enter"], opts, default="profB"), "profB")


@unittest.skipUnless(_textual_available(), "textual not installed")
class ExistingProjectScreenTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, keys, projects):
        from textual.app import App
        from ccw_tui.screens.existing import ExistingProjectScreen

        # Test helper: accept bare names; ExistingProjectScreen now
        # expects ``(name, compact_mtime)`` tuples.
        prepared = [(p, "") if isinstance(p, str) else p for p in projects]

        class Host(App):
            result = "unset"

            def on_mount(self):
                def done(r):
                    self.result = r
                    self.exit()
                self.push_screen(ExistingProjectScreen(prepared, "/srv/work"), done)

        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            for k in keys:
                await pilot.press(k)
            await pilot.pause()
        return app.result

    async def test_digit_1_picks_first(self):
        projs = ["alpha", "beta", "gamma"]
        self.assertEqual(await self._run(["1"], projs), "alpha")

    async def test_enter_picks_cursor(self):
        self.assertEqual(await self._run(["enter"], ["alpha"]), "alpha")

    async def test_escape_cancels(self):
        self.assertIsNone(await self._run(["escape"], ["alpha"]))


@unittest.skipUnless(_textual_available(), "textual not installed")
class SettingsScreenTests(unittest.IsolatedAsyncioTestCase):
    async def _mk_cbs(self, entries_factory):
        from ccw_tui.screens.settings import SettingsCallbacks

        saved: list = []
        removed: list = []

        def save(k, v):
            saved.append((k, v))

        def remove(k):
            removed.append(k)

        def save_mapping(k, v):
            saved.append((k, v))

        return saved, removed, SettingsCallbacks(
            get_entries=entries_factory,
            save_setting=save,
            remove_setting=remove,
            save_mapping=save_mapping,
        )

    async def test_bool_toggle_saves_value(self):
        from textual.app import App
        from ccw_settings import SettingEntry, SettingSpec
        from ccw_tui.screens.settings import SettingsScreen, BoolToggleModal

        spec = SettingSpec("git_create_enabled", "bool", "desc")
        entries = [SettingEntry(spec=spec, value=False, source="default", editable=True)]

        saved, removed, cbs = await self._mk_cbs(lambda: entries)

        class Host(App):
            def on_mount(self):
                self.push_screen(SettingsScreen(cbs))

        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            # SettingsScreen is active; its DataTable cursor is on row 0
            # (the bool entry). Press Enter → BoolToggleModal pushed.
            await pilot.press("enter")
            await pilot.pause()
            # Click True button.
            from ccw_tui.screens.settings import BoolToggleModal
            modal = app.screen_stack[-1]
            self.assertIsInstance(modal, BoolToggleModal)
            btn = modal.query_one("#true")
            btn.press()
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
        self.assertEqual(saved, [("git_create_enabled", True)])

    async def test_reset_calls_remove(self):
        from textual.app import App
        from ccw_settings import SettingEntry, SettingSpec
        from ccw_tui.screens.settings import SettingsScreen

        spec = SettingSpec("session_prefix", "string", "desc")
        entries = [SettingEntry(spec=spec, value="ccw", source="repo", editable=True)]
        saved, removed, cbs = await self._mk_cbs(lambda: entries)

        class Host(App):
            def on_mount(self):
                self.push_screen(SettingsScreen(cbs))

        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
        self.assertEqual(removed, ["session_prefix"])


@unittest.skipUnless(_textual_available(), "textual not installed")
class GitRemotesScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_populates_and_esc_dismisses(self):
        from textual.app import App
        from ccw_tui.screens.git_remotes import GitRemotesScreen

        rows = [
            ("foo", "github.com", "alice", "gh", "alice", "private", ""),
            ("bar", "gitlab.com", "bob", "token", "bob", "public", "~/.tok"),
        ]

        class Host(App):
            dismissed = False
            def on_mount(self):
                def done(_r):
                    self.dismissed = True
                    self.exit()
                self.push_screen(GitRemotesScreen(rows), done)

        app = Host()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
        self.assertTrue(app.dismissed)

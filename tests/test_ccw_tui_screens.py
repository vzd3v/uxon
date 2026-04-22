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

from harness.textual_scenarios import ScreenScenario, press_keys, run_screen_scenarios  # noqa: E402


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

        app = CcwApp(_mk_ctx(), probe_agents=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
        self.assertEqual(app.quit_rc, 0)

    async def test_digit_1_activates_action_cwd(self) -> None:
        from ccw_tui.app import CcwApp

        app = CcwApp(_mk_ctx(), probe_agents=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("1")      # digit-1 → LaunchOptionsScreen
            await pilot.pause()
            await pilot.press("enter")  # confirm mode (normal, index 0)
            await pilot.pause()
        self.assertIsNotNone(app.pending_launch)
        self.assertEqual(app.pending_launch.label, "cwd")

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
        app = CcwApp(ctx, probe_agents=False)
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
        app = CcwApp(ctx, probe_agents=False)
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
        app = CcwApp(ctx, probe_agents=False)
        captured: list[tuple[str, str]] = []
        async with app.run_test(size=(100, 30)) as pilot:
            orig = app.notify

            def cap(msg, **kw):
                captured.append((str(msg), kw.get("severity", "")))
                return orig(msg, **kw)

            app.notify = cap
            await pilot.pause()
            await pilot.press("1")        # digit-1 → LaunchOptionsScreen
            await pilot.pause()
            await pilot.press("enter")    # confirm mode → on_launch_cwd(...)
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
        errored = [m for m, sev in captured if sev == "error" and "nope" in m]
        self.assertTrue(errored, f"expected error toast in {captured!r}")
        self.assertIsNone(app.pending_launch)


@unittest.skipUnless(_textual_available(), "textual not installed")
class ConfirmModalTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirm_modal_smoke_batch(self) -> None:
        from textual.widgets import Input
        from ccw_tui.screens.confirm import ConfirmPhrase, ConfirmYesNo

        async def phrase(app, pilot):
            app.screen.query_one("#confirm-input", Input).focus()
            await pilot.press(*"kill-all")
            await pilot.press("enter")

        scenarios = [
            ScreenScenario("yesno-y", lambda: ConfirmYesNo("Kill foo?"), press_keys("y"), True),
            ScreenScenario("yesno-n", lambda: ConfirmYesNo("Kill foo?"), press_keys("n"), False),
            ScreenScenario("phrase-match", lambda: ConfirmPhrase("Danger!", "kill-all"), phrase, True),
        ]
        results = await run_screen_scenarios(scenarios, size=(80, 24))
        self.assertEqual(results, [s.expected for s in scenarios])

if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(_textual_available(), "textual not installed")
class LaunchOptionsScreenTests(unittest.IsolatedAsyncioTestCase):
    """Pilot tests for the two-panel agent × permission-mode modal."""

    def _make_avail(self, status: str):
        from ccw_agents import AgentAvailability
        return AgentAvailability(status=status)

    async def _run_screen(self, ctx, keys):
        from textual.app import App
        from ccw_tui.screens.launch_options import LaunchOptionsScreen

        class Host(App):
            result = "unset"

            def on_mount(self):
                def done(r):
                    self.result = r
                    self.exit()
                self.push_screen(LaunchOptionsScreen(ctx), done)

        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            for k in keys:
                await pilot.press(k)
            await pilot.pause()
        return app.result

    async def test_launch_options_layout_smoke_batch(self) -> None:
        from textual.app import App
        from ccw_tui.screens.launch_options import LaunchOptionsScreen

        async def assert_single(app, pilot):
            screen = app.screen
            panel = screen.query_one("#agent-panel")
            self.assertFalse(panel.display)

        async def assert_two(app, pilot):
            screen = app.screen
            panel = screen.query_one("#agent-panel")
            self.assertTrue(panel.display)

        async def assert_pending(app, pilot):
            screen = app.screen
            self.assertIn("claude", screen._visible_agents)
            agent_list = screen.query_one("#agent-list")
            labels = [str(item.query_one("Static").content) for item in agent_list.children]
            self.assertTrue(any("checking" in label for label in labels), f"no checking in {labels}")
            await pilot.press("enter")

        scenarios = [
            ScreenScenario(
                "single-agent-hidden",
                lambda: LaunchOptionsScreen(_mk_ctx(
                    enabled_agents=("claude",),
                    default_agent="claude",
                    agent_availability={"claude": self._make_avail("ok")},
                )),
                assert_single,
                "unset",
            ),
            ScreenScenario(
                "two-agents-visible",
                lambda: LaunchOptionsScreen(_mk_ctx(
                    enabled_agents=("claude", "cursor"),
                    default_agent="claude",
                    agent_availability={
                        "claude": self._make_avail("ok"),
                        "cursor": self._make_avail("ok"),
                    },
                )),
                assert_two,
                "unset",
            ),
            ScreenScenario(
                "pending-label",
                lambda: LaunchOptionsScreen(_mk_ctx(
                    enabled_agents=("claude",),
                    default_agent="claude",
                    agent_availability={"claude": self._make_avail("pending")},
                )),
                assert_pending,
                ("claude", "normal"),
            ),
        ]

        results = await run_screen_scenarios(scenarios)
        self.assertEqual(results, [s.expected for s in scenarios])

    async def test_dismiss_returns_agent_and_mode(self) -> None:
        ctx = _mk_ctx(
            enabled_agents=("claude",),
            default_agent="claude",
            agent_availability={"claude": self._make_avail("ok")},
        )
        # Single agent → mode panel active; enter at index 0 = "normal"
        result = await self._run_screen(ctx, ["enter"])
        self.assertEqual(result, ("claude", "normal"))

    async def test_escape_returns_none(self) -> None:
        ctx = _mk_ctx(
            enabled_agents=("claude",),
            default_agent="claude",
            agent_availability={"claude": self._make_avail("ok")},
        )
        result = await self._run_screen(ctx, ["escape"])
        self.assertIsNone(result)

    async def test_arrow_to_cursor_rebuilds_modes_and_commits_cursor(self) -> None:
        """Regression: arrow-down on the agent list must update _current_agent
        and rebuild the mode list for that agent.

        Previous bug: the screen-level up/down bindings were shadowed by
        ListView's built-in cursor_up/cursor_down, so _maybe_rebuild_mode
        never ran. Arrowing down to cursor kept _current_agent=claude and
        kept claude's three modes (normal/auto/yolo) in the right panel,
        so pressing enter twice committed ("claude", "yolo (--dsp)")
        instead of a cursor launch.
        """
        from textual.app import App
        from textual.widgets import ListView
        from ccw_tui.screens.launch_options import LaunchOptionsScreen

        ctx = _mk_ctx(
            enabled_agents=("claude", "cursor"),
            default_agent="claude",
            agent_availability={
                "claude": self._make_avail("ok"),
                "cursor": self._make_avail("ok"),
            },
        )

        class Host(App):
            result = "unset"

            def on_mount(self):
                def done(r):
                    self.result = r
                    self.exit()
                self.push_screen(LaunchOptionsScreen(ctx), done)

        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            screen = app.screen
            # Move highlight to cursor (2nd entry).
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(screen._current_agent, "cursor")
            mode_list = screen.query_one("#mode-list", ListView)
            # cursor has exactly two modes in the catalog: normal, yolo.
            self.assertEqual(len(mode_list.children), 2)
            mode_ids = [item.id for item in mode_list.children]
            self.assertEqual(mode_ids, ["mode-normal", "mode-yolo"])
            # Right → switch to mode panel; down → highlight yolo; enter → commit.
            await pilot.press("right")
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
        self.assertEqual(app.result, ("cursor", "yolo"))

    async def test_all_missing_dismisses_with_none(self) -> None:
        from textual.app import App
        from ccw_tui.screens.launch_options import LaunchOptionsScreen

        ctx = _mk_ctx(
            enabled_agents=("claude",),
            default_agent="claude",
            agent_availability={"claude": self._make_avail("missing")},
        )

        class Host(App):
            result = "unset"

            def on_mount(self):
                def done(r):
                    self.result = r
                    self.exit()
                self.push_screen(LaunchOptionsScreen(ctx), done)

        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
        self.assertIsNone(app.result)

    async def test_live_update_to_all_missing_dismisses_without_stale_commit(self) -> None:
        from textual.app import App
        from ccw_tui.screens.launch_options import LaunchOptionsScreen

        ctx = _mk_ctx(
            enabled_agents=("claude",),
            default_agent="claude",
            agent_availability={"claude": self._make_avail("pending")},
        )

        class Host(App):
            result = "unset"

            def on_mount(self):
                def done(r):
                    self.result = r
                    self.exit()
                self.push_screen(LaunchOptionsScreen(ctx), done)

        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            screen = app.screen
            ctx.agent_availability["claude"] = self._make_avail("missing")
            await screen._rebuild_agent_list()
            await pilot.pause()
        self.assertIsNone(app.result)

    async def test_live_update_from_two_agents_to_one_switches_to_mode_panel(self) -> None:
        from textual.app import App
        from ccw_tui.screens.launch_options import LaunchOptionsScreen

        ctx = _mk_ctx(
            enabled_agents=("claude", "cursor"),
            default_agent="claude",
            agent_availability={
                "claude": self._make_avail("pending"),
                "cursor": self._make_avail("ok"),
            },
        )

        class Host(App):
            result = "unset"

            def on_mount(self):
                def done(r):
                    self.result = r
                    self.exit()
                self.push_screen(LaunchOptionsScreen(ctx), done)

        app = Host()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            screen = app.screen
            ctx.agent_availability["claude"] = self._make_avail("missing")
            await screen._rebuild_agent_list()
            await pilot.pause()
            self.assertEqual(screen._visible_agents, ["cursor"])
            self.assertEqual(screen._current_agent, "cursor")
            self.assertEqual(screen._active_panel, "mode")
            await pilot.press("enter")
            await pilot.pause()
        self.assertEqual(app.result, ("cursor", "normal"))


@unittest.skipUnless(_textual_available(), "textual not installed")
class NewProjectScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_project_smoke_batch(self) -> None:
        from textual.widgets import Input
        from ccw_tui.screens.new_project import NewProjectScreen

        async def submit_foo(app, pilot):
            app.screen.query_one("#name-input", Input).focus()
            await pilot.press(*"foo")
            await pilot.press("enter")

        async def cancel_bar(app, pilot):
            app.screen.query_one("#name-input", Input).focus()
            await pilot.press(*"bar")
            await pilot.press("escape")

        scenarios = [
            ScreenScenario("valid-name", lambda: NewProjectScreen("/srv/work"), submit_foo, "foo"),
            ScreenScenario("escape-cancel", lambda: NewProjectScreen("/srv/work"), cancel_bar, None),
        ]
        results = await run_screen_scenarios(scenarios, size=(80, 24))
        self.assertEqual(results, [s.expected for s in scenarios])


@unittest.skipUnless(_textual_available(), "textual not installed")
class GitProfileScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_git_profile_smoke_batch(self) -> None:
        from ccw_tui.screens.git_profile import GitProfileScreen

        scenarios = [
            ScreenScenario(
                "escape-cancel",
                lambda: GitProfileScreen([("profA", "A")]),
                press_keys("escape"),
                None,
            ),
            ScreenScenario(
                "default-profile-enter",
                lambda: GitProfileScreen([("profA", "A"), ("profB", "B")], default_profile="profB"),
                press_keys("enter"),
                "profB",
            ),
        ]
        results = await run_screen_scenarios(scenarios)
        self.assertEqual(results, [s.expected for s in scenarios])


@unittest.skipUnless(_textual_available(), "textual not installed")
class ExistingProjectScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_project_smoke_batch(self) -> None:
        from ccw_tui.screens.existing import ExistingProjectScreen

        scenarios = [
            ScreenScenario(
                "enter-picks-cursor",
                lambda: ExistingProjectScreen([("alpha", "")], "/srv/work"),
                press_keys("enter"),
                "alpha",
            ),
            ScreenScenario(
                "escape-cancel",
                lambda: ExistingProjectScreen([("alpha", "")], "/srv/work"),
                press_keys("escape"),
                None,
            ),
        ]
        results = await run_screen_scenarios(scenarios)
        self.assertEqual(results, [s.expected for s in scenarios])


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

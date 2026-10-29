"""Tests for the attach-vs-new choice modal.

Two scopes:

1. CLI probe helper (``probe_tui_compatible_sessions``) — pure-Python,
   no textual dependency. Filters a fake session list down to entries
   compatible with (target_dir, profile_id).

2. ``SessionChoiceScreen`` modal behaviour — ↑/↓ + Enter row selection,
   the ``n`` new-alongside shortcut, Esc cancel, and the shape of the
   dismiss values. Requires textual + pilot.

3. End-to-end MainScreen wiring: ``_launch_cwd`` must consult
   ``on_probe_existing_sessions`` before committing, and route the
   "attach" branch through ``on_attach`` (not ``on_launch_cwd``).
"""

from __future__ import annotations

import unittest

from helpers import run_off_loop_sync


def _textual_available() -> bool:
    try:
        import textual  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(_textual_available(), "textual not installed")
class SessionChoiceScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_keyboard_mouse_new_and_cancel_choices(self) -> None:
        from harness.textual_scenarios import ScreenScenario, press_keys, run_screen_scenarios
        from textual.widgets import ListView

        from uxon.tui.screens.session_choice import SessionChoiceScreen

        existing = (("alice", "uxon-myproj@claude", False), ("bob", "uxon-myproj@claude-2", True))

        async def click_second(app, pilot):
            self.assertEqual(app.screen.query_one("#session-list", ListView).index, 0)
            await pilot.click("#sess-1")

        scenarios = [
            ScreenScenario(
                "first-enter",
                lambda: SessionChoiceScreen("myproj", existing),
                press_keys("up", "down", "enter"),
                ("attach", "uxon-myproj@claude", "alice"),
            ),
            ScreenScenario(
                "second-enter",
                lambda: SessionChoiceScreen("myproj", existing),
                press_keys("down", "enter"),
                ("attach", "uxon-myproj@claude-2", "bob"),
            ),
            ScreenScenario(
                "second-click",
                lambda: SessionChoiceScreen("myproj", existing),
                click_second,
                ("attach", "uxon-myproj@claude-2", "bob"),
            ),
            ScreenScenario(
                "new-shortcut",
                lambda: SessionChoiceScreen("myproj", existing),
                press_keys("n"),
                ("new", None, None),
            ),
            ScreenScenario(
                "trailing-new",
                lambda: SessionChoiceScreen("myproj", existing),
                press_keys("up", "enter"),
                ("new", None, None),
            ),
            ScreenScenario(
                "cancel",
                lambda: SessionChoiceScreen("myproj", existing),
                press_keys("escape"),
                None,
            ),
        ]
        self.assertEqual(await run_screen_scenarios(scenarios), [s.expected for s in scenarios])

    async def test_keyboard_works_when_pushed_from_dismiss_callback(self) -> None:
        """Regression: keyboard-dead modal when opened from another modal.

        The real launch flow pushes SessionChoiceScreen from inside
        LaunchOptionsScreen's dismiss callback. A synchronous ``focus()``
        in ``on_mount`` raced the popped screen's deferred focus-restore,
        which stole focus to a background widget and left the modal
        keyboard-dead. Declarative ``AUTO_FOCUS`` is applied at the
        framework's compose/resume moment instead, so focus lands on the
        list and the ``n`` binding fires. Mirrors the production
        stacking (base screen -> first modal -> dismiss -> this modal).
        """
        from textual.app import App
        from textual.screen import ModalScreen, Screen
        from textual.widgets import Button, ListView

        from uxon.tui.screens.session_choice import SessionChoiceScreen

        results: list[object] = []

        class _FirstModal(ModalScreen):
            def compose(self):
                yield Button("opt", id="opt")

            def on_mount(self) -> None:
                self.query_one("#opt", Button).focus()

            def on_key(self, event) -> None:
                if event.key == "enter":
                    self.dismiss("picked")

        class _Base(Screen):
            def compose(self):
                yield Button("base", id="base")

            def on_mount(self) -> None:
                self.query_one("#base", Button).focus()

        class _Host(App):
            def on_mount(self) -> None:
                self.push_screen(_Base())

                def after_first(_):
                    self.push_screen(
                        SessionChoiceScreen(
                            target_label="myproj",
                            existing=(("uxon-myproj@claude", False),),
                        ),
                        lambda r: results.append(r),
                    )

                self.push_screen(_FirstModal(), after_first)

        app = _Host()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("enter")  # dismiss first modal -> pushes ours
            await pilot.pause()
            self.assertIsInstance(app.screen, SessionChoiceScreen)
            self.assertIsInstance(app.focused, ListView)
            await pilot.press("n")  # keyboard must drive the modal
            await pilot.pause()

        self.assertEqual(results, [("new", None, None)])


def _mk_ctx(**overrides):
    from uxon.tui.context import LaunchRequest, TuiContext

    base = dict(
        sessions=[],
        total_cpu="0",
        total_ram="0",
        version="0",
        cwd="/srv/work",
        cwd_short="work",
        new_project_root="/srv/work",
        existing_projects=[],
        cwd_writable=True,
        current_user="dev",
        launch_user="dev",
        on_launch_cwd=lambda a, m, target_dir=None: LaunchRequest(cmd=("/bin/true",), label="cwd"),
        on_launch_new=lambda n, a, m, g: LaunchRequest(cmd=("/bin/true",), label="new"),
        on_launch_existing=lambda n, a, m: LaunchRequest(cmd=("/bin/true",), label="existing"),
    )
    base.update(overrides)
    ctx = TuiContext(**base)
    ctx.refresh_sources = []
    return ctx


class MainScreenHelperTests(unittest.TestCase):
    def test_probe_choice_routes_new_attach_cancel_and_probe_user(self) -> None:
        from types import SimpleNamespace

        from uxon.tui.screens.launch_flow import LaunchFlow
        from uxon.tui.screens.session_choice import SessionChoiceScreen

        for existing, choice, expected in (
            ((), None, [("new",)]),
            ((("uxon-work@claude", False),), None, []),
            ((("uxon-work@claude", False),), ("new", None, None), [("new",)]),
            (
                (("uxon-work@claude", False),),
                ("attach", "uxon-work@claude", None),
                [("attach", "bob", "uxon-work@claude")],
            ),
            (
                (("alice", "uxon-work@claude", False),),
                ("attach", "uxon-work@claude", "alice"),
                [("attach", "alice", "uxon-work@claude")],
            ),
        ):
            with self.subTest(existing=existing, choice=choice):
                probes, effects, screens = [], [], []

                def probe(*args, probes=probes, existing=existing):
                    probes.append(args)
                    return existing

                def push(screen, done, screens=screens, choice=choice):
                    self.assertIsInstance(screen, SessionChoiceScreen)
                    screens.append(screen)
                    done(choice)

                host = SimpleNamespace(
                    cfg=_mk_ctx(on_probe_existing_sessions=probe, launch_user="bob"),
                    app=SimpleNamespace(push_screen=push, run_off_loop=run_off_loop_sync),
                    _attach_session=lambda user, name, effects=effects: effects.append(
                        ("attach", user, name)
                    ),
                )
                LaunchFlow(host).maybe_show_session_choice(
                    target_dir="/srv/work",
                    target_label="work",
                    profile_id="claude",
                    mode_id="normal",
                    on_new=lambda effects=effects: effects.append(("new",)),
                )
                self.assertEqual(probes, [("/srv/work", "claude", "normal")])
                self.assertEqual(len(screens), 1 if existing else 0)
                self.assertEqual(effects, expected)


if __name__ == "__main__":
    unittest.main()

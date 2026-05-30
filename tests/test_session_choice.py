"""Tests for the attach-vs-new choice modal.

Two scopes:

1. CLI probe helper (``probe_tui_compatible_sessions``) — pure-Python,
   no textual dependency. Filters a fake session list down to entries
   compatible with (target_dir, agent_id).

2. ``SessionChoiceScreen`` modal behaviour — keyboard a/n/Esc and the
   shape of the dismiss values. Requires textual + pilot.

3. End-to-end MainScreen wiring: ``_launch_cwd`` must consult
   ``on_probe_existing_sessions`` before committing, and route the
   "attach" branch through ``on_attach`` (not ``on_launch_cwd``).
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from helpers import make_config

from uxon.cli import SessionInfo


def _textual_available() -> bool:
    try:
        import textual  # noqa: F401
    except ImportError:
        return False
    return True


class ProbeHelperTests(unittest.TestCase):
    def test_compatible_sessions_filtered_by_stem_and_agent(self) -> None:
        from uxon.cli import probe_tui_compatible_sessions

        target_dir = "/srv/repos/myproj"
        sessions = [
            SessionInfo(
                user="dev",
                name="uxon-myproj@claude",
                attached="0",
                windows="1",
                created="",
                last_attached="",
                pane_pids=(),
                active_pid=None,
                active_cmd="",
                active_path=target_dir,
                agent="claude",
            ),
            SessionInfo(
                user="dev",
                name="uxon-myproj@codex",
                attached="0",
                windows="1",
                created="",
                last_attached="",
                pane_pids=(),
                active_pid=None,
                active_cmd="",
                active_path=target_dir,
                agent="codex",
            ),
            SessionInfo(
                user="dev",
                name="uxon-other@claude",
                attached="0",
                windows="1",
                created="",
                last_attached="",
                pane_pids=(),
                active_pid=None,
                active_cmd="",
                active_path="/srv/repos/other",
                agent="claude",
            ),
        ]
        cfg = make_config()

        def fake_collect(users, c):
            return sessions

        import uxon.cli as cli_mod

        original = cli_mod.collect_sessions
        cli_mod.collect_sessions = fake_collect
        try:
            matches = probe_tui_compatible_sessions(cfg, "dev", target_dir, "claude")
        finally:
            cli_mod.collect_sessions = original

        names = [s.name for s in matches]
        self.assertEqual(names, ["uxon-myproj@claude"])


@unittest.skipUnless(_textual_available(), "textual not installed")
class SessionChoiceScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_attach_returns_highlighted_name(self) -> None:
        from textual.app import App

        from uxon.tui.screens.session_choice import SessionChoiceScreen

        results: list[object] = []

        class _Host(App):
            async def on_mount(self) -> None:
                screen = SessionChoiceScreen(
                    target_label="myproj",
                    existing=(("uxon-myproj@claude", False), ("uxon-myproj@claude-2", True)),
                )
                await self.push_screen(screen, lambda r: results.append(r))

        async with _Host().run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()

        self.assertEqual(results, [("attach", "uxon-myproj@claude")])

    async def test_new_alongside_returns_new_sentinel(self) -> None:
        from textual.app import App

        from uxon.tui.screens.session_choice import SessionChoiceScreen

        results: list[object] = []

        class _Host(App):
            async def on_mount(self) -> None:
                screen = SessionChoiceScreen(
                    target_label="myproj",
                    existing=(("uxon-myproj@claude", False),),
                )
                await self.push_screen(screen, lambda r: results.append(r))

        async with _Host().run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()

        self.assertEqual(results, [("new", None)])

    async def test_keyboard_works_when_pushed_from_dismiss_callback(self) -> None:
        """Regression: keyboard-dead modal when opened from another modal.

        The real launch flow pushes SessionChoiceScreen from inside
        LaunchOptionsScreen's dismiss callback. A synchronous ``focus()``
        in ``on_mount`` raced the popped screen's deferred focus-restore,
        which stole focus to a background widget and left the modal
        keyboard-dead. Declarative ``AUTO_FOCUS`` is applied at the
        framework's compose/resume moment instead, so focus lands on the
        list and the ``n``/``a`` bindings fire. Mirrors the production
        stacking (base screen -> first modal -> dismiss -> this modal).
        """
        from textual.app import App
        from textual.screen import ModalScreen, Screen
        from textual.widgets import Button, ListView

        from uxon.tui.screens.session_choice import SessionChoiceScreen

        # AUTO_FOCUS is the contract that prevents the race.
        self.assertEqual(SessionChoiceScreen.AUTO_FOCUS, "#session-list")

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

        self.assertEqual(results, [("new", None)])

    async def test_escape_cancels(self) -> None:
        from textual.app import App

        from uxon.tui.screens.session_choice import SessionChoiceScreen

        results: list[object] = []

        class _Host(App):
            async def on_mount(self) -> None:
                screen = SessionChoiceScreen(
                    target_label="myproj",
                    existing=(("uxon-myproj@claude", False),),
                )
                await self.push_screen(screen, lambda r: results.append(r))

        async with _Host().run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

        self.assertEqual(results, [None])


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
        on_launch_cwd=lambda a, m: LaunchRequest(cmd=("/bin/true",), label="cwd"),
        on_launch_new=lambda n, a, m, g: LaunchRequest(cmd=("/bin/true",), label="new"),
        on_launch_existing=lambda n, a, m: LaunchRequest(cmd=("/bin/true",), label="existing"),
    )
    base.update(overrides)
    ctx = TuiContext(**base)
    ctx.refresh_sources = []
    return ctx


class _StubScreen:
    """Minimal stand-in that hosts ``MainScreen._maybe_show_session_choice``.

    ``MainScreen.app`` is a read-only property on the Textual base class,
    so we can't monkeypatch it onto a real MainScreen instance. Instead
    we re-bind the unbound function onto this lightweight stub — the
    helper only touches ``self.ctx``, ``self.app.push_screen``,
    ``self.app.notify``, ``self.app.request_launch``, and the
    ``_attach_session`` method, all of which we mock here.
    """

    def __init__(self, ctx, attach_log):
        self.ctx = ctx
        self.app = MagicMock()
        self.app.push_screen = MagicMock()
        self.app.notify = MagicMock()
        self.app.request_launch = MagicMock()
        self._attach_log = attach_log

    def _attach_session(self, user, session_name):
        self._attach_log.append((user, session_name))


def _bind_helper(stub):
    from uxon.tui.screens.main import MainScreen

    return MainScreen._maybe_show_session_choice.__get__(stub, _StubScreen)


class MainScreenHelperTests(unittest.TestCase):
    """Direct-call tests for ``_maybe_show_session_choice``."""

    def test_no_existing_calls_on_new_directly(self) -> None:
        ctx = _mk_ctx(on_probe_existing_sessions=lambda d, a: ())
        stub = _StubScreen(ctx, attach_log=[])
        helper = _bind_helper(stub)
        called: list[str] = []
        helper(
            target_dir="/srv/work",
            target_label="work",
            agent_id="claude",
            on_new=lambda: called.append("new"),
        )
        self.assertEqual(called, ["new"])
        stub.app.push_screen.assert_not_called()

    def test_existing_pushes_modal(self) -> None:
        ctx = _mk_ctx(
            on_probe_existing_sessions=lambda d, a: (("uxon-work@claude", False),),
        )
        stub = _StubScreen(ctx, attach_log=[])
        helper = _bind_helper(stub)
        helper(
            target_dir="/srv/work",
            target_label="work",
            agent_id="claude",
            on_new=lambda: None,
        )
        stub.app.push_screen.assert_called_once()
        screen_arg = stub.app.push_screen.call_args[0][0]
        self.assertEqual(type(screen_arg).__name__, "SessionChoiceScreen")

    def test_attach_branch_invokes_on_attach(self) -> None:
        attach_log: list[tuple[str, str]] = []
        ctx = _mk_ctx(
            on_probe_existing_sessions=lambda d, a: (("uxon-work@claude", False),),
        )
        stub = _StubScreen(ctx, attach_log=attach_log)
        helper = _bind_helper(stub)
        new_called: list[str] = []
        helper(
            target_dir="/srv/work",
            target_label="work",
            agent_id="claude",
            on_new=lambda: new_called.append("new"),
        )
        callback = stub.app.push_screen.call_args[0][1]
        callback(("attach", "uxon-work@claude"))
        self.assertEqual(attach_log, [("dev", "uxon-work@claude")])
        self.assertEqual(new_called, [])

    def test_new_branch_calls_on_new(self) -> None:
        ctx = _mk_ctx(
            on_probe_existing_sessions=lambda d, a: (("uxon-work@claude", False),),
        )
        stub = _StubScreen(ctx, attach_log=[])
        helper = _bind_helper(stub)
        new_called: list[str] = []
        helper(
            target_dir="/srv/work",
            target_label="work",
            agent_id="claude",
            on_new=lambda: new_called.append("new"),
        )
        callback = stub.app.push_screen.call_args[0][1]
        callback(("new", None))
        self.assertEqual(new_called, ["new"])

    def test_cancel_is_a_noop(self) -> None:
        ctx = _mk_ctx(
            on_probe_existing_sessions=lambda d, a: (("uxon-work@claude", False),),
        )
        stub = _StubScreen(ctx, attach_log=[])
        helper = _bind_helper(stub)
        new_called: list[str] = []
        helper(
            target_dir="/srv/work",
            target_label="work",
            agent_id="claude",
            on_new=lambda: new_called.append("new"),
        )
        callback = stub.app.push_screen.call_args[0][1]
        callback(None)
        self.assertEqual(new_called, [])
        self.assertEqual(stub._attach_log, [])


if __name__ == "__main__":
    unittest.main()

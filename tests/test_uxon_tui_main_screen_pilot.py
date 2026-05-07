"""Pilot smoke for :class:`MainScreen` keybinding scope.

Specifically: ``Esc`` no longer terminates the app from the
top-level screen — the binding was removed in 3.4 to make Esc
scope-local on modals/sub-screens.
"""

from __future__ import annotations

import unittest


def _textual_available() -> bool:
    try:
        import textual  # noqa: F401
    except ImportError:
        return False
    return True


def _mk_ctx(**overrides):
    from uxon.tui.context import LaunchRequest, TuiContext

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
        current_user="devagent",
        on_launch_cwd=lambda agent_id, mode_id: LaunchRequest(cmd=("/bin/true",), label="cwd"),
        on_launch_new=lambda n, agent_id, mode_id, g: LaunchRequest(
            cmd=("/bin/true",), label="new"
        ),
        on_launch_existing=lambda n, agent_id, mode_id: LaunchRequest(
            cmd=("/bin/true",), label="existing"
        ),
    )
    base.update(overrides)
    ctx = TuiContext(**base)
    ctx.refresh_sources = []
    return ctx


@unittest.skipUnless(_textual_available(), "textual not installed")
class EscNotQuitTests(unittest.IsolatedAsyncioTestCase):
    async def test_escape_does_not_quit_main_screen(self) -> None:
        from uxon.tui.app import UxonApp
        from uxon.tui.screens.main import MainScreen

        ctx = _mk_ctx()
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            # Esc no longer triggers ``action_quit`` — the screen must
            # still be MainScreen after the keypress.
            self.assertIsInstance(app.screen, MainScreen)


if __name__ == "__main__":
    unittest.main()

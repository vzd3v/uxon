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


@unittest.skipUnless(_textual_available(), "textual not installed")
class SearchBarSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_smoke_search_filter_forces_flat_then_clear(self) -> None:
        from uxon.remote_hosts import RemoteHost
        from uxon.tui.app import UxonApp

        ctx = _mk_ctx(
            remote_hosts=[
                RemoteHost(name="kris", ssh_alias="kris", description="", remote_uxon="uxon"),
                RemoteHost(name="ada", ssh_alias="ada", description="", remote_uxon="uxon"),
            ]
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # SearchBar Input has focus by default after on_mount.
            focused = pilot.app.focused
            self.assertIsNotNone(focused)
            self.assertIn("Input", type(focused).__name__)
            # Type "kris" — non-empty filter forces flat render.
            await pilot.press("k", "r", "i", "s")
            await pilot.pause()
            strip = pilot.app.screen.query_one("#host-tabs")
            self.assertFalse(strip.display)
            # Esc clears the input.
            await pilot.press("escape")
            await pilot.pause()
            # Esc again blurs back to the dashboard / sibling widget.
            await pilot.press("escape")
            await pilot.pause()
            # Tab navigation still works after the SearchBar lost focus.
            await pilot.press("]")
            await pilot.pause()
            await pilot.press("q")


if __name__ == "__main__":
    unittest.main()

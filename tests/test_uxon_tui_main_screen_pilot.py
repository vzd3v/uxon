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
            # Default view is now ``flat`` — flip to ``by_host`` first
            # so the strip is actually visible before the search test.
            # Without this, the strip is hidden regardless of search
            # state and the assertion below would pass vacuously.
            await pilot.press("v")
            await pilot.pause()
            strip = pilot.app.screen.query_one("#host-tabs")
            self.assertTrue(strip.display, "by_host should expose the strip")
            # Default focus lands on action-cwd; the search bar is
            # hidden until summoned with ``s``.
            bar = pilot.app.screen.query_one("#search-bar")
            self.assertFalse(bar.has_class("-shown"))
            # Summon the search bar.
            await pilot.press("s")
            await pilot.pause()
            self.assertTrue(bar.has_class("-shown"))
            focused = pilot.app.focused
            self.assertIsNotNone(focused)
            self.assertIn("Input", type(focused).__name__)
            # Type "kris" — non-empty filter forces flat render: the
            # strip must hide even though ``view_mode`` is still by_host.
            await pilot.press("k", "r", "i", "s")
            await pilot.pause()
            self.assertFalse(strip.display, "non-empty filter must force flat (strip hidden)")
            # First Esc clears the input but leaves the bar visible.
            await pilot.press("escape")
            await pilot.pause()
            self.assertTrue(bar.has_class("-shown"))
            # Filter cleared → forced_flat lifts → strip visible again
            # (we're still in by_host view_mode).
            self.assertTrue(strip.display, "clearing the filter must restore by_host strip")
            # Second Esc hides the bar and returns focus to the caller.
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(bar.has_class("-shown"))
            await pilot.press("q")


if __name__ == "__main__":
    unittest.main()

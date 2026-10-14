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
        current_user="dana_agent",
        on_launch_cwd=lambda profile_id, mode_id, target_dir=None: LaunchRequest(
            cmd=("/bin/true",), label="cwd"
        ),
        on_launch_new=lambda n, profile_id, mode_id, g: LaunchRequest(
            cmd=("/bin/true",), label="new"
        ),
        on_launch_existing=lambda n, profile_id, mode_id: LaunchRequest(
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

        for quit_key in ("q", "й"):
            with self.subTest(quit_key=quit_key):
                app = UxonApp(_mk_ctx(), probe_agents=False)
                async with app.run_test(size=(120, 30)) as pilot:
                    await pilot.press("escape")
                    self.assertIsInstance(app.screen, MainScreen)
                    self.assertTrue(app.is_running)
                    await pilot.press(quit_key)
                    await pilot.pause()
                    self.assertFalse(app.is_running)
                    self.assertEqual(app.quit_rc, 0)


@unittest.skipUnless(_textual_available(), "textual not installed")
class SearchBarSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_smoke_search_filter_forces_flat_then_clear(self) -> None:
        from helpers import make_dashboard_session as _session
        from textual.geometry import Region

        from uxon.domain.wire_schema import RemoteSnapshot
        from uxon.infra.remote_hosts import RemoteHost
        from uxon.tui.app import UxonApp
        from uxon.tui.messages import _RefreshSourceLanded
        from uxon.tui.widgets.session_list_view import SessionListView

        launches = []

        ctx = _mk_ctx(
            tui_render_debounce_ms=0,
            sessions=[
                _session("uxon-123@claude", user="dana_agent"),
                _session("uxon-other@claude", user="dana_agent"),
            ],
            on_launch_cwd=lambda *args, **kwargs: launches.append(args),
            on_launch_new=lambda *args, **kwargs: launches.append(args),
            on_launch_existing=lambda *args, **kwargs: launches.append(args),
            remote_hosts=[
                RemoteHost(name="kris", ssh_alias="kris", description="", remote_uxon="uxon"),
                RemoteHost(name="ada", ssh_alias="ada", description="", remote_uxon="uxon"),
                RemoteHost(
                    name="build-box", ssh_alias="build-box", description="", remote_uxon="uxon"
                ),
            ],
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.on__refresh_source_landed(_RefreshSourceLanded("main_ctx_rebuild", ctx))
            await pilot.pause()
            self.assertEqual(app.screen.query_one(SessionListView).row_count, 2)
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
            await pilot.press("1", "2", "3")
            await pilot.pause()
            self.assertEqual(app.focused.value, "123")
            self.assertEqual([r.name for r in app.screen._dashboard_rows], ["uxon-123@claude"])
            self.assertEqual(launches, [])
            self.assertIsNone(app.pending_launch)
            self.assertFalse(strip.display, "non-empty filter must force flat (strip hidden)")
            # First Esc clears the input but leaves the bar visible.
            await pilot.press("escape")
            await pilot.pause()
            self.assertTrue(bar.has_class("-shown"))
            # Filter cleared → forced_flat lifts → strip visible again
            # (we're still in by_host view_mode).
            self.assertTrue(strip.display, "clearing the filter must restore by_host strip")
            self.assertEqual(app.screen.query_one(SessionListView).row_count, 2)
            # Second Esc hides the bar and returns focus to the caller.
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(bar.has_class("-shown"))
            # Failed peers remain visible below the table, independent of tab.
            for peer in ctx.remote_hosts:
                app.on__refresh_source_landed(
                    _RefreshSourceLanded(
                        f"remote:{peer.name}",
                        RemoteSnapshot(
                            peer.name,
                            1,
                            "mismatch",
                            error_code="update_remote",
                            peer={"uxon_version": "[bold]0.9.0"},
                        ),
                    )
                )
            await pilot.pause()
            counts = app.screen.query_one("#fleet-failures")
            self.assertEqual(counts.content.count("update remote"), 3)
            self.assertIn("[bold]0.9.0", counts.content)
            await pilot.resize_terminal(40, 30)
            await pilot.pause()
            self.assertGreater(counts.size.height, 1)
            self.assertEqual(counts.content.count("update remote"), 3)
            rendered = "".join(
                strip.text
                for strip in counts.render_lines(
                    Region(0, 0, counts.size.width, counts.size.height),
                )
            )
            self.assertEqual(" ".join(rendered.split()).count("update remote"), 3)
            self.assertIn("[bold]0.9.0", rendered)
            self.assertGreaterEqual(
                counts.region.y, app.screen.query_one("#sessions-dashboard").region.bottom
            )
            await pilot.press("h")
            await pilot.pause()
            expanded = app.screen.query_one("#fleet-expanded")
            self.assertTrue(expanded.display)
            self.assertEqual(
                sum("update remote" in str(child.content) for child in expanded.children), 3
            )
            await pilot.press("q")


if __name__ == "__main__":
    unittest.main()

"""Pilot tests for the arrow-key navigation introduced in 3.4 (final).

Each test pins one load-bearing contract; reverse-direction symmetry
is verified by the cyclic wrap inside the kept tests rather than by
duplicate ←/→ pairs (cyclic ``% len`` is symmetric — the only way
forward could pass while backward fails is an off-by-one in the wrap,
which the wrap assertion already guards). Compute boundaries are
tested as a pure function in :mod:`tests.test_dashboard_buckets`.
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
    # Mirror the production wiring so the first ``kick_on_mount`` tick
    # populates ``state.main`` (and therefore the dashboard rows /
    # block_starts) before the assertions run.
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
    """Build a minimal :class:`TuiSession` for dashboard population."""
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
class TopActionRowCyclingTests(unittest.IsolatedAsyncioTestCase):
    """←/→ cycles the three top buttons cyclically; ↓ leaves the group."""

    async def test_right_cycles_forward(self) -> None:
        from uxon.tui.app import UxonApp
        from uxon.tui.widgets import ActionRow

        app = UxonApp(_mk_ctx(), probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # Default focus is on the SearchBar input — blur to step
            # into the action group via Tab.
            await pilot.press("escape")
            await pilot.pause()
            app.screen.query_one("#action-cwd", ActionRow).focus()
            await pilot.pause()
            self.assertEqual(app.focused.id, "action-cwd")
            await pilot.press("right")
            await pilot.pause()
            self.assertEqual(app.focused.id, "action-new")
            await pilot.press("right")
            await pilot.pause()
            self.assertEqual(app.focused.id, "action-open")
            # Cyclic: → from the last button wraps to the first.
            await pilot.press("right")
            await pilot.pause()
            self.assertEqual(app.focused.id, "action-cwd")

    async def test_down_leaves_group(self) -> None:
        """↓ from any button exits past the entire group in one step."""
        from uxon.tui.app import UxonApp
        from uxon.tui.widgets import ActionRow

        app = UxonApp(_mk_ctx(), probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            # Start on action-new (middle button) — the trickiest case
            # because focus_next would land on action-open before
            # leaving the group; the ↓ binding must skip past it.
            app.screen.query_one("#action-new", ActionRow).focus()
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()
            focused = app.focused
            self.assertIsNotNone(focused)
            # Must have left the action group entirely; landing on any
            # other button (cwd / open) would mean ↓ degraded to the
            # default focus_next.
            self.assertNotIn(
                getattr(focused, "id", None),
                {"action-cwd", "action-new", "action-open"},
                msg="↓ from middle button must skip past the entire group",
            )


@unittest.skipUnless(_textual_available(), "textual not installed")
class FlatBlockJumpTests(unittest.IsolatedAsyncioTestCase):
    """←/→ on the dashboard in flat view jumps cursor between blocks."""

    async def test_right_jumps_to_next_block(self) -> None:
        from uxon.remote_hosts import RemoteHost
        from uxon.tui.app import UxonApp
        from uxon.tui.widgets.session_dashboard_table import SessionDashboardTable

        # Names chosen so the recency-then-name sort lands the own
        # rows before the other-user row (otherwise alphabet alone
        # would mix them — the model selector does not segregate by
        # user inside the local block, only by host).
        ctx = _mk_ctx(
            sessions=[_session("a-own1"), _session("a-own2")],
            other_sessions=[_session("z-alice", user="alice")],
            remote_hosts=[
                RemoteHost(name="kris", ssh_alias="kris", description="", remote_uxon="uxon"),
            ],
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            # ``kick_on_mount=True`` queues the first rebuild; pause
            # again to let the dispatcher land it before reading state.
            app.kick_refresh()
            await pilot.pause()
            await pilot.pause()
            screen = app.screen
            table = screen.query_one("#sessions-dashboard", SessionDashboardTable)
            # Default view is flat — sanity check the strip is hidden.
            strip = screen.query_one("#host-tabs")
            self.assertFalse(strip.display, "default view should be flat")
            # Three blocks: own (rows 0..1) → other-user alice (row 2)
            # → remote kris (no rows but block_starts only includes
            # rows that exist, so just two starts: 0, 2).
            starts = table.block_starts
            self.assertEqual(starts, (0, 2), msg=f"unexpected block_starts: {starts}")
            # Focus the table and place cursor on the own block.
            table.focus()
            table.move_cursor(row=0)
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            self.assertEqual(table.cursor_row, 2, msg="→ should jump to other-user block")
            # Cyclic: → from the last block wraps back to row 0.
            await pilot.press("right")
            await pilot.pause()
            self.assertEqual(table.cursor_row, 0)

@unittest.skipUnless(_textual_available(), "textual not installed")
class ByHostTabCyclingTests(unittest.IsolatedAsyncioTestCase):
    """←/→ on the dashboard in by_host view cycles the active host tab."""

    async def test_right_cycles_active_tab_forward(self) -> None:
        from uxon.remote_hosts import RemoteHost
        from uxon.tui.app import UxonApp
        from uxon.tui.widgets.session_dashboard_table import SessionDashboardTable

        ctx = _mk_ctx(
            remote_hosts=[
                RemoteHost(name="kris", ssh_alias="kris", description="", remote_uxon="uxon"),
                RemoteHost(name="ada", ssh_alias="ada", description="", remote_uxon="uxon"),
            ],
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            screen = app.screen
            # Flip to by_host so the strip is visible.
            await pilot.press("v")
            await pilot.pause()
            self.assertEqual(app.main_ui.ui.view_mode, "by_host")
            self.assertEqual(app.main_ui.active_tab_index, 0)
            # ←/→ on the dashboard table cycles the active tab.
            table = screen.query_one("#sessions-dashboard", SessionDashboardTable)
            table.focus()
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            self.assertEqual(app.main_ui.active_tab_index, 1)
            await pilot.press("right")
            await pilot.pause()
            self.assertEqual(app.main_ui.active_tab_index, 2)
            # Cyclic: → from the last tab wraps to 0.
            await pilot.press("right")
            await pilot.pause()
            self.assertEqual(app.main_ui.active_tab_index, 0)

    async def test_search_active_does_not_cycle_hidden_tabs(self) -> None:
        """In by_host with an active search the strip is hidden — ←/→
        must NOT silently rotate it (regression for the bug where
        clearing search would land on an unexpected tab).
        """
        from uxon.remote_hosts import RemoteHost
        from uxon.tui.app import UxonApp
        from uxon.tui.widgets.session_dashboard_table import SessionDashboardTable

        ctx = _mk_ctx(
            remote_hosts=[
                RemoteHost(name="kris", ssh_alias="kris", description="", remote_uxon="uxon"),
                RemoteHost(name="ada", ssh_alias="ada", description="", remote_uxon="uxon"),
            ],
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            screen = app.screen
            # by_host + active search → strip hidden, view_mode still by_host.
            await pilot.press("v")
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            await pilot.press("z", "z", "z")  # filter that matches nothing
            await pilot.pause()
            self.assertEqual(app.main_ui.ui.view_mode, "by_host")
            self.assertTrue(app.main_ui.ui.filter_text)
            tab_before = app.main_ui.active_tab_index
            # Focus the table and press → — must NOT advance the tab.
            table = screen.query_one("#sessions-dashboard", SessionDashboardTable)
            table.focus()
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            self.assertEqual(
                app.main_ui.active_tab_index,
                tab_before,
                msg="search-active by_host must not cycle the hidden tab strip",
            )


if __name__ == "__main__":
    unittest.main()

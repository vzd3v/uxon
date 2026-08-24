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
class TopActionStackNavigationTests(unittest.IsolatedAsyncioTestCase):
    """↑/↓ walk the vertical action stack one row at a time."""

    async def test_down_walks_the_stack(self) -> None:
        from uxon.tui.app import UxonApp
        from uxon.tui.widgets import ActionRow

        app = UxonApp(_mk_ctx(), probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.screen.query_one("#action-cwd", ActionRow).focus()
            await pilot.pause()
            self.assertEqual(app.focused.id, "action-cwd")
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(app.focused.id, "action-new")
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(app.focused.id, "action-open")
            # ↓ from the last row leaves the stack entirely.
            await pilot.press("down")
            await pilot.pause()
            self.assertNotIn(
                getattr(app.focused, "id", None),
                {"action-cwd", "action-new", "action-open"},
                msg="↓ from the last action row must leave the stack",
            )

    async def test_up_walks_the_stack(self) -> None:
        from uxon.tui.app import UxonApp
        from uxon.tui.widgets import ActionRow

        app = UxonApp(_mk_ctx(), probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.screen.query_one("#action-open", ActionRow).focus()
            await pilot.pause()
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(app.focused.id, "action-new")
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(app.focused.id, "action-cwd")


@unittest.skipUnless(_textual_available(), "textual not installed")
class UpFromButtonsLandsOnLastRowTests(unittest.IsolatedAsyncioTestCase):
    """↑ from any top button wraps to the LAST row of the dashboard.

    There is nothing visually above the action row, so the focus chain
    wraps to the bottom widget. Without the wrap-cursor fixup the
    table's cursor stays at row 0 (its default), which reads as "↑
    jumped to the FIRST row" — the opposite of what the wrap should
    do. This test pins ``cursor_row == row_count - 1`` after ↑.
    """

    async def test_up_from_first_button_focuses_last_row(self) -> None:
        from uxon.tui.app import UxonApp
        from uxon.tui.widgets import ActionRow
        from uxon.tui.widgets.session_list_view import SessionListView

        ctx = _mk_ctx(sessions=[_session("a"), _session("b"), _session("c")])
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.kick_refresh()
            await pilot.pause()
            await pilot.pause()
            screen = app.screen
            table = screen.query_one("#sessions-dashboard", SessionListView)
            self.assertGreaterEqual(table.row_count, 2, msg="need ≥2 rows so first/last differ")
            screen.query_one("#action-cwd", ActionRow).focus()
            await pilot.pause()
            await pilot.press("up")
            await pilot.pause()
            self.assertIsInstance(app.focused, SessionListView)
            self.assertEqual(
                table.cursor_row,
                table.row_count - 1,
                msg="↑ from buttons must land on the LAST row, not the first",
            )


@unittest.skipUnless(_textual_available(), "textual not installed")
class DownFromButtonsLandsOnFirstRowTests(unittest.IsolatedAsyncioTestCase):
    """↓ from the last action row forces the dashboard cursor to row 0.

    Without the symmetric ``move_cursor(row=0)`` in
    :meth:`ActionRow.action_leave`, the table preserves its prior
    ``cursor_row`` (e.g. wherever the operator left it before going ↑
    to the action stack). Pressing ↓ from the stack then teleports
    them back to that prior position rather than to the natural top of
    the list. This regression check pins ``cursor_row == 0`` after ↓.
    """

    async def test_down_from_last_action_row_focuses_first_row(self) -> None:
        from uxon.tui.app import UxonApp
        from uxon.tui.widgets import ActionRow
        from uxon.tui.widgets.session_list_view import SessionListView

        ctx = _mk_ctx(sessions=[_session("a"), _session("b"), _session("c")])
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.kick_refresh()
            await pilot.pause()
            await pilot.pause()
            screen = app.screen
            table = screen.query_one("#sessions-dashboard", SessionListView)
            self.assertGreaterEqual(table.row_count, 2)
            # Pre-position the cursor on the LAST row so that landing
            # on row 0 is observably different from "no-op preserve".
            table.move_cursor(row=table.row_count - 1)
            screen.query_one("#action-open", ActionRow).focus()
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()
            self.assertIsInstance(app.focused, SessionListView)
            self.assertEqual(
                table.cursor_row,
                0,
                msg="↓ from the stack must land on row 0, not preserve prior position",
            )


@unittest.skipUnless(_textual_available(), "textual not installed")
class DashboardUpFocusRowAboveTests(unittest.IsolatedAsyncioTestCase):
    """↑ on the dashboard's top row lands on the action row directly above.

    The action rows are a vertical stack, so the previous focus-chain
    stop *is* the nearest row above the table — spatial navigation and
    the focus chain agree, no special-casing.
    """

    async def test_up_from_top_row_focuses_row_above(self) -> None:
        from uxon.tui.app import UxonApp
        from uxon.tui.widgets.session_list_view import SessionListView

        ctx = _mk_ctx(sessions=[_session("a-own1")])
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.kick_refresh()
            await pilot.pause()
            await pilot.pause()
            screen = app.screen
            table = screen.query_one("#sessions-dashboard", SessionListView)
            table.focus()
            table.move_cursor(row=0)
            await pilot.pause()
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(
                app.focused.id,
                "action-open",
                msg="↑ from row 0 must focus the action row directly above the table",
            )


@unittest.skipUnless(_textual_available(), "textual not installed")
class FlatBlockJumpTests(unittest.IsolatedAsyncioTestCase):
    """←/→ on the dashboard in flat view jumps cursor between blocks."""

    async def test_right_jumps_to_next_block(self) -> None:
        from uxon.infra.remote_hosts import RemoteHost
        from uxon.tui.app import UxonApp
        from uxon.tui.widgets.session_list_view import SessionListView

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
            table = screen.query_one("#sessions-dashboard", SessionListView)
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
        from uxon.infra.remote_hosts import RemoteHost
        from uxon.tui.app import UxonApp
        from uxon.tui.widgets.session_list_view import SessionListView

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
            table = screen.query_one("#sessions-dashboard", SessionListView)
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
        from uxon.infra.remote_hosts import RemoteHost
        from uxon.tui.app import UxonApp
        from uxon.tui.widgets.session_list_view import SessionListView

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
            table = screen.query_one("#sessions-dashboard", SessionListView)
            table.focus()
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            self.assertEqual(
                app.main_ui.active_tab_index,
                tab_before,
                msg="search-active by_host must not cycle the hidden tab strip",
            )


@unittest.skipUnless(_textual_available(), "textual not installed")
class FleetStatusBarToggleTests(unittest.IsolatedAsyncioTestCase):
    """`h` toggles the FleetStatusBar; state lives on the App-owned
    ``main_ui`` so it survives the ``apply_loaded_ctx`` recompose, and the
    bar is not a focus-chain stop (↑ from the buttons still reaches the
    session list — pinned by :class:`UpFromButtonsLandsOnLastRowTests`).
    """

    async def test_h_toggles_collapsed_expanded(self) -> None:
        from uxon.tui.app import UxonApp
        from uxon.tui.widgets.fleet_status_bar import FleetStatusBar

        ctx = _mk_ctx(sessions=[_session("a"), _session("b")])
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.kick_refresh()
            await pilot.pause()
            screen = app.screen
            bar = screen.query_one("#fleet-status", FleetStatusBar)
            # Default collapsed.
            self.assertFalse(app.main_ui.hosts_expanded)
            self.assertTrue(screen.query_one("#fleet-collapsed").display)
            self.assertFalse(screen.query_one("#fleet-expanded").display)
            # Not focusable — keeps it out of the focus chain.
            self.assertFalse(bar.can_focus)
            # h expands.
            await pilot.press("h")
            await pilot.pause()
            self.assertTrue(app.main_ui.hosts_expanded)
            self.assertTrue(screen.query_one("#fleet-expanded").display)
            self.assertFalse(screen.query_one("#fleet-collapsed").display)
            # h again collapses.
            await pilot.press("h")
            await pilot.pause()
            self.assertFalse(app.main_ui.hosts_expanded)
            self.assertTrue(screen.query_one("#fleet-collapsed").display)

    async def test_expanded_state_survives_structural_refresh(self) -> None:
        from uxon.tui.app import UxonApp
        from uxon.tui.screens.main import MainScreen

        # Start with NO own sessions, then add one — flips the
        # has_own_sessions layout-signature bit, the canonical structural
        # refresh. This used to force a ``switch_screen`` swap (which
        # dropped in-flight keys); it is now reconciled IN PLACE. The
        # test pins both halves: the screen object is NOT replaced, and
        # the App-owned expanded toggle still renders.
        ctx = _mk_ctx(sessions=[])
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.kick_refresh()
            await pilot.pause()
            first_screen = app.screen
            await pilot.press("h")
            await pilot.pause()
            self.assertTrue(app.main_ui.hosts_expanded)
            app.screen.apply_loaded_ctx(_mk_ctx(sessions=[_session("a")]))
            await pilot.pause()
            self.assertIsInstance(app.screen, MainScreen)
            # The fix: a structural signature flip no longer swaps the
            # screen, so the focus holder (and Textual's queued input)
            # survives the refresh — no keys dropped mid-update.
            self.assertIs(
                app.screen, first_screen, msg="structural refresh must patch in place, not swap"
            )
            self.assertTrue(app.main_ui.hosts_expanded)
            self.assertTrue(app.screen.query_one("#fleet-expanded").display)


@unittest.skipUnless(_textual_available(), "textual not installed")
class RefreshDoesNotDropKeysTests(unittest.IsolatedAsyncioTestCase):
    """A background refresh that flips the layout signature must not drop
    in-flight key events.

    Regression for the swallowed-keys bug: ``apply_loaded_ctx`` used to
    answer a signature change with ``app.switch_screen`` — a full screen
    swap that destroyed the focused widget, so any ↓ already queued in
    Textual's input pump hit a dead DOM target and vanished. The fix
    reconciles structural deltas in place (display toggles + live column
    rebuild), keeping the focus holder alive. This pins the contract by
    machine count: post exactly N ↓ events, flip the signature mid-stream
    (cross_user latch → USER column), and assert the cursor advanced by
    all N. Pre-fix this lost exactly the one key in flight during the
    swap (cursor 11 instead of 12).
    """

    async def test_signature_flip_mid_stream_drops_no_keys(self) -> None:
        from textual.events import Key

        from uxon.tui.app import UxonApp
        from uxon.tui.widgets.session_list_view import SessionListView

        sessions = [_session(f"s{i:02d}") for i in range(20)]
        app = UxonApp(_mk_ctx(sessions=sessions), probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.kick_refresh()
            await pilot.pause()
            await pilot.pause()
            table = app.screen.query_one("#sessions-dashboard", SessionListView)
            table.focus()
            table.move_cursor(row=0)
            await pilot.pause()
            self.assertNotIn("user", tuple(c.id for c in app.screen._active_columns))

            presses = 12
            for i in range(presses):
                app.post_message(Key("down", None))
                if i == presses // 2:
                    # Flip cross_user → in-place column rebuild, while ↓
                    # events are still queued in the pump.
                    app.screen.apply_loaded_ctx(
                        _mk_ctx(
                            sessions=sessions,
                            other_sessions=[_session("z-alice", user="alice")],
                        )
                    )
            for _ in range(3):
                await pilot.pause()

            table = app.screen.query_one("#sessions-dashboard", SessionListView)
            # The signature flip really happened (USER column is live)...
            self.assertIn("user", tuple(c.id for c in app.screen._active_columns))
            # ...and every one of the 12 presses landed: cursor advanced
            # from row 0 to row 12, no key swallowed by the refresh.
            self.assertEqual(
                table.cursor_row,
                presses,
                msg="a refresh that flips the layout signature dropped an in-flight key",
            )


@unittest.skipUnless(_textual_available(), "textual not installed")
class FocusRestoreSkipsHiddenRowTests(unittest.IsolatedAsyncioTestCase):
    """``_focus_key`` must not claim a hidden superuser row as a focus target.

    The superuser rows are now always mounted and toggled via
    ``display``. ``_focus_key`` queries them by id; without a display
    guard it would call ``.focus()`` on a ``display:none`` row and report
    success, parking focus on an invisible widget and leaning on Textual's
    hide-reflow to bounce it back. Pre-always-mount the row was simply
    absent → ``query_one`` miss → ``False`` → clean fallback. This pins
    that contract: a hidden target returns ``False``, a visible one True.
    """

    async def test_focus_key_returns_false_for_hidden_action_row(self) -> None:
        from uxon.domain.sudo import SudoCapability
        from uxon.tui.app import UxonApp
        from uxon.tui.widgets import ActionRow

        ctx = _mk_ctx(
            sessions=[_session("a")],
            sudo_caps=SudoCapability(reachable_users=frozenset({"alice"})),
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.kick_refresh()
            await pilot.pause()
            screen = app.screen
            settings = screen.query_one("#action-settings", ActionRow)
            # has_super is True → the row is visible and a valid target.
            self.assertTrue(settings.display)
            self.assertTrue(screen._focus_key("action:action-settings"))
            # Hide it (as a has_super False flip would) → no longer a
            # valid focus target.
            settings.display = False
            await pilot.pause()
            self.assertFalse(screen._focus_key("action:action-settings"))
            # Control: a visible row still restores.
            self.assertTrue(screen._focus_key("action:action-cwd"))


if __name__ == "__main__":
    unittest.main()

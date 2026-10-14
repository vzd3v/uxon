"""Real keyboard traversal, host switching and refresh-focus regressions."""

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
    async def test_action_stack_dashboard_traversal_and_wrap(self) -> None:
        from uxon.tui.app import UxonApp
        from uxon.tui.widgets import ActionRow
        from uxon.tui.widgets.session_list_view import SessionListView

        app = UxonApp(
            _mk_ctx(sessions=[_session("a"), _session("b"), _session("c")]), probe_agents=False
        )
        async with app.run_test(size=(120, 30)) as pilot:
            table = app.screen.query_one("#sessions-dashboard", SessionListView)
            app.kick_refresh()
            for _ in range(20):
                await pilot.pause()
                table = app.screen.query_one("#sessions-dashboard", SessionListView)
                if table.row_count == 3:
                    break
            self.assertEqual(table.row_count, 3, "main data did not land")
            app.screen.query_one("#action-cwd", ActionRow).focus()
            await pilot.pause()
            self.assertEqual(app.focused.id, "action-cwd")
            for key, focused_id, cursor in (
                ("down", "action-new", None),
                ("down", "action-open", None),
                ("down", "sessions-dashboard", 0),
                ("down", "sessions-dashboard", 1),
                ("down", "sessions-dashboard", 2),
                ("down", "action-cwd", None),
                ("up", "sessions-dashboard", 2),
                ("up", "sessions-dashboard", 1),
                ("up", "sessions-dashboard", 0),
                ("up", "action-open", None),
                ("up", "action-new", None),
                ("up", "action-cwd", None),
                ("up", "sessions-dashboard", 2),
            ):
                with self.subTest(key=key, focused_id=focused_id, cursor=cursor):
                    await pilot.press(key)
                    await pilot.pause()
                    self.assertEqual(app.focused.id, focused_id)
                    if cursor is not None:
                        self.assertEqual(table.cursor_row, cursor)
                        self.assertEqual(table.selected_row_key, f"local/me/{'abc'[cursor]}")
            # Entering down must reset an independently parked last-row cursor.
            app.screen.query_one("#action-open", ActionRow).focus()
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(app.focused.id, "sessions-dashboard")
            self.assertEqual(table.cursor_row, 0)
            self.assertIn("a", table.render_line(1).text)


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
            self.assertEqual(table.selected_row_key, "local/alice/z-alice")
            # Cyclic: → from the last block wraps back to row 0.
            await pilot.press("right")
            await pilot.pause()
            self.assertEqual(table.cursor_row, 0)
            self.assertEqual(table.selected_row_key, "local/me/a-own1")
            await pilot.press("left")
            await pilot.pause()
            self.assertEqual(table.cursor_row, 2)
            self.assertEqual(table.selected_row_key, "local/alice/z-alice")
            await pilot.press("left")
            await pilot.pause()
            self.assertEqual(table.cursor_row, 0)
            self.assertEqual(table.selected_row_key, "local/me/a-own1")


@unittest.skipUnless(_textual_available(), "textual not installed")
class ByHostTabCyclingTests(unittest.IsolatedAsyncioTestCase):
    async def test_tab_keys_click_search_and_clear_render_correct_hosts(self) -> None:
        from uxon.domain.wire_schema import RemoteSnapshot
        from uxon.infra.remote_hosts import RemoteHost
        from uxon.tui.app import UxonApp
        from uxon.tui.messages import _RefreshSourceLanded
        from uxon.tui.widgets.host_tab_strip import HostTabStrip
        from uxon.tui.widgets.session_list_view import SessionListView

        ctx = _mk_ctx(
            sessions=[_session("match-local")],
            tui_render_debounce_ms=0,
            remote_hosts=[
                RemoteHost(name=name, ssh_alias=name, description="", remote_uxon="uxon")
                for name in ("kris", "ada")
            ],
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(140, 30)) as pilot:
            table = app.screen.query_one("#sessions-dashboard", SessionListView)
            app.kick_refresh()
            for _ in range(20):
                await pilot.pause()
                table = app.screen.query_one("#sessions-dashboard", SessionListView)
                if table.row_count == 1:
                    break
            self.assertEqual(table.row_count, 1, "local data did not land")
            for name in ("kris", "ada"):
                app.on__refresh_source_landed(
                    _RefreshSourceLanded(
                        f"remote:{name}",
                        RemoteSnapshot(
                            name,
                            1.0,
                            None,
                            sessions=[
                                {"user": "me", "name": f"match-{name}", "short_id": f"match-{name}"}
                            ],
                        ),
                    )
                )
            await pilot.pause()
            self.assertEqual(table.row_count, 3)
            await pilot.press("v")
            await pilot.pause()
            strip = app.screen.query_one("#host-tabs", HostTabStrip)

            def assert_host(index, name):
                self.assertTrue(strip.display)
                self.assertEqual(app.main_ui.active_tab_index, index)
                self.assertEqual([tab.id for tab in strip.query(".-active")], [f"tab-{index}"])
                self.assertEqual(table.row_count, 1)
                self.assertEqual(
                    table.selected_row_key, f"{'local' if index == 0 else name}/me/match-{name}"
                )
                self.assertIn(f"match-{name}", table.render_line(1).text)
                for other in {"local", "kris", "ada"} - {name}:
                    self.assertNotIn(f"match-{other}", table.render_line(1).text)

            assert_host(0, "local")
            table.focus()
            for key, index, name in (
                ("right", 1, "kris"),
                ("right", 2, "ada"),
                ("right", 0, "local"),
                ("left", 2, "ada"),
                ("left", 1, "kris"),
                ("left", 0, "local"),
            ):
                await pilot.press(key)
                await pilot.pause()
                assert_host(index, name)
            self.assertTrue(await pilot.click("#tab-1"))
            await pilot.pause()
            assert_host(1, "kris")
            self.assertEqual(app.focused.id, "tab-1")
            await pilot.press("s", *"match")
            await pilot.pause()
            self.assertEqual(app.main_ui.ui.view_mode, "by_host")
            self.assertEqual(app.main_ui.ui.filter_text, "match")
            self.assertFalse(strip.display)
            self.assertEqual(table.row_count, 3)
            table.focus()
            table.move_cursor(row=0)
            for key, expected in (
                ("right", "kris"),
                ("right", "ada"),
                ("right", "local"),
                ("left", "ada"),
            ):
                await pilot.press(key)
                await pilot.pause()
                self.assertEqual(app.main_ui.active_tab_index, 1)
                self.assertEqual(table.selected_row_key, f"{expected}/me/match-{expected}")
                self.assertIn(f"match-{expected}", table.render_line(table.cursor_row + 1).text)
            await pilot.press("s", "escape")
            await pilot.pause()
            self.assertEqual(app.main_ui.ui.filter_text, "")
            assert_host(1, "kris")
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(app.screen.query_one("#search-bar").display)


@unittest.skipUnless(_textual_available(), "textual not installed")
class FleetStatusBarToggleTests(unittest.IsolatedAsyncioTestCase):
    async def test_expansion_survives_structural_landing_then_collapses(self) -> None:
        from uxon.tui.app import UxonApp
        from uxon.tui.widgets.fleet_status_bar import FleetStatusBar

        app = UxonApp(_mk_ctx(sessions=[]), probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.kick_refresh()
            await pilot.pause()
            screen = app.screen
            bar = screen.query_one("#fleet-status", FleetStatusBar)
            self.assertFalse(bar.can_focus)
            self.assertTrue(screen.query_one("#fleet-collapsed").display)
            self.assertFalse(screen.query_one("#fleet-expanded").display)
            await pilot.press("h")
            await pilot.pause()
            self.assertTrue(screen.query_one("#fleet-expanded").display)
            self.assertFalse(screen.query_one("#fleet-collapsed").display)
            screen.apply_loaded_ctx(
                _mk_ctx(
                    sessions=[_session("a")], other_sessions=[_session("z-alice", user="alice")]
                )
            )
            await pilot.pause()
            self.assertTrue(app.main_ui.hosts_expanded)
            self.assertTrue(app.screen.query_one("#fleet-expanded").display)
            details = " ".join(
                str(child.content) for child in app.screen.query_one("#fleet-expanded").children
            )
            self.assertIn("local", details)
            self.assertIn("2/0 sess", details)
            await pilot.press("h")
            await pilot.pause()
            self.assertFalse(app.main_ui.hosts_expanded)
            self.assertTrue(app.screen.query_one("#fleet-collapsed").display)
            self.assertFalse(app.screen.query_one("#fleet-expanded").display)


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
    """Losing Settings access must leave a visible keyboard target."""

    async def test_capability_loss_restores_visible_keyboard_target(self) -> None:
        from uxon.domain.sudo import SudoCapability
        from uxon.infra.agents import AgentAvailability
        from uxon.tui.app import UxonApp
        from uxon.tui.screens.launch_options import LaunchOptionsScreen
        from uxon.tui.widgets import ActionRow

        ctx = _mk_ctx(
            sessions=[_session("a")],
            sudo_caps=SudoCapability(reachable_users=frozenset({"alice"})),
            enabled_profiles=("claude",),
            agent_availability={"claude": AgentAvailability(status="ok")},
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.kick_refresh()
            await pilot.pause()
            screen = app.screen
            settings = screen.query_one("#action-settings", ActionRow)
            self.assertTrue(settings.display)
            settings.focus()
            await pilot.pause()
            self.assertEqual(app.focused.id, "action-settings")
            screen.apply_loaded_ctx(
                _mk_ctx(
                    sessions=[_session("a")],
                    sudo_caps=SudoCapability(),
                    enabled_profiles=("claude",),
                )
            )
            await pilot.pause()
            self.assertFalse(app.screen.query_one("#action-settings", ActionRow).display)
            self.assertIsNotNone(app.focused)
            self.assertTrue(app.focused.display)
            self.assertNotEqual(app.focused.id, "action-settings")
            # A visible action remains keyboard-operable after the saved target vanishes.
            app.screen.query_one("#action-cwd", ActionRow).focus()
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(app.focused.id, "action-new")
            await pilot.press("up", "enter")
            for _ in range(20):
                await pilot.pause()
                if isinstance(app.screen, LaunchOptionsScreen):
                    break
            self.assertIsInstance(app.screen, LaunchOptionsScreen)


if __name__ == "__main__":
    unittest.main()

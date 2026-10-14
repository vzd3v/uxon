"""Dashboard loading, target dispatch and structural refresh lifecycles."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from helpers import land_dashboard_context as _land
from helpers import make_dashboard_context as _mk_ctx
from helpers import make_dashboard_session as _session
from helpers import make_dashboard_snapshot as _snapshot
from helpers import visible_dashboard_text as _visible
from textual.widgets import Footer, Static

from uxon.domain.sudo import SudoCapability
from uxon.domain.wire_schema import RemoteSnapshot
from uxon.infra.remote_hosts import RemoteHost
from uxon.tui.app import UxonApp
from uxon.tui.context import LaunchRequest
from uxon.tui.messages import _RefreshSourceLanded
from uxon.tui.screens.confirm import ConfirmYesNo
from uxon.tui.widgets.session_list_view import SessionListView


class DashboardLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_loading_lands_rows_and_visible_destructive_footer(self):
        from uxon.infra import events

        logs = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"UXON_LOG_DIR": logs}))
        self.enterContext(patch.object(events, "_DEBUG_TOPICS", frozenset({"startup"})))
        app = UxonApp(_mk_ctx(loading=True), probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            screen = app.screen
            table = screen.query_one("#sessions-dashboard", SessionListView)
            note = screen.query_one("#sessions-note", Static)
            self.assertEqual(table.row_count, 0)
            from uxon.infra.agents import AgentAvailability
            from uxon.tui.messages import _HostReportUpdated

            app.post_message(_HostReportUpdated(availability={"claude": AgentAvailability("ok")}))
            await pilot.pause()
            self.assertIn("Loading sessions", str(note.content))
            self.assertFalse(note.has_class("-hidden"))

            self.assertEqual(len(app.screen_stack), 2)
            screen.loading = False
            screen.loading = True
            from textual.widgets import LoadingIndicator

            await pilot.pause()
            self.assertEqual(len(screen.query(LoadingIndicator)), 0)
            self.assertTrue(screen.query_one("#action-cwd").display)
            footer = screen.query_one(Footer)
            rendered_footer = " ".join(
                child.render_line(i).text
                for child in footer.children
                for i in range(child.size.height)
            )
            self.assertIn("Kill", rendered_footer)
            self.assertIn("d", rendered_footer)
            _land(app, _mk_ctx(sessions=[_session()]))
            await pilot.pause()
            self.assertIs(app.screen, screen)
            self.assertFalse(screen.loading)
            self.assertEqual(app.state.agent_availability.value["claude"].status, "ok")
            self.assertIs(app.ctx, screen.cfg)
            self.assertEqual(app.state.refresh_tick, 1)
            self.assertEqual([s.name for s in app.state.main.sessions], ["uxon-local@claude"])
            self.assertEqual(table.row_count, 1)
            self.assertIn("uxon-local", _visible(table))
            self.assertTrue(note.has_class("-hidden"))
            _land(app, _mk_ctx())
            await pilot.pause()
            self.assertEqual(table.row_count, 0)
            self.assertIn("No active sessions", str(note.content))
            self.assertFalse(note.has_class("-hidden"))

        records = [
            json.loads(line)
            for file in Path(logs).glob("tui-debug-*.log")
            for line in file.read_text().splitlines()
        ]
        landed = [r for r in records if r.get("at") == "first_data_landed"]
        self.assertEqual(len(landed), 1)
        self.assertEqual(landed[0]["source"], "main_ctx_rebuild")
        self.assertGreater(landed[0]["ts"], 0)

    async def test_attach_kill_target_identity_cancel_and_confirm_time_recheck(self):
        calls = []
        requests = []
        refreshes = []

        def attach(*target):
            calls.append(("attach", *target))
            return LaunchRequest(("true",), label="/".join(target))

        ctx = _mk_ctx(
            sessions=[_session()],
            other_sessions=[_session("uxon-foreign@codex", user="bob")],
            sudo_caps=SudoCapability(frozenset({"bob"})),
            remote_hosts=[RemoteHost("peer", "peer", "", "uxon")],
            on_attach=attach,
            on_remote_attach=attach,
            on_kill=lambda *target: calls.append(("kill", *target)),
            on_remote_kill=lambda *target: calls.append(("kill", *target)),
        )
        app = UxonApp(ctx, probe_agents=False)
        app.request_launch = requests.append
        async with app.run_test(size=(120, 30)) as pilot:
            _land(app, ctx)
            app.on__refresh_source_landed(_RefreshSourceLanded("remote:peer", _snapshot()))
            await pilot.pause()
            table = app.screen.query_one("#sessions-dashboard", SessionListView)
            # The external refresh boundary exposes whether kill bypasses cooldown.
            app.kick_refresh = lambda **kwargs: refreshes.append(kwargs)
            for target in (
                (None, "alice", "uxon-local@claude"),
                (None, "bob", "uxon-foreign@codex"),
                ("peer", "bob", "uxon-remote@claude"),
            ):
                with self.subTest(target=target):
                    table.focus()
                    table.move_cursor(
                        row=next(
                            i
                            for i, r in enumerate(app.screen._dashboard_rows)
                            if (r.host, r.user, r.name) == target
                        )
                    )
                    await pilot.press("enter")
                    await pilot.pause()
                    args = target[1:] if target[0] is None else target
                    self.assertEqual(calls[-1], ("attach", *args))
                    self.assertEqual(requests[-1].label, "/".join(args))
                    await pilot.press("d")
                    self.assertIsInstance(app.screen, ConfirmYesNo)
                    self.assertIn(target[1], app.screen.prompt)
                    self.assertIn(target[2], app.screen.prompt)
                    before = list(calls)
                    if target[1] == "alice" or target[0] is not None:
                        await pilot.press("escape")
                        self.assertEqual(calls, before)
                        await pilot.press("d")
                    await pilot.press("y")
                    await pilot.pause()
                    self.assertEqual(calls[-1], ("kill", *args))
                    self.assertEqual(refreshes[-1], {})
            self.assertEqual(
                calls,
                [
                    ("attach", "alice", "uxon-local@claude"),
                    ("kill", "alice", "uxon-local@claude"),
                    ("attach", "bob", "uxon-foreign@codex"),
                    ("kill", "bob", "uxon-foreign@codex"),
                    ("attach", "peer", "bob", "uxon-remote@claude"),
                    ("kill", "peer", "bob", "uxon-remote@claude"),
                ],
            )
            self.assertEqual(
                [request.label for request in requests],
                [
                    "alice/uxon-local@claude",
                    "bob/uxon-foreign@codex",
                    "peer/bob/uxon-remote@claude",
                ],
            )
            self.assertEqual(refreshes, [{}, {}, {}])
            before = list(calls)
            table.focus()
            await pilot.press("d")
            app.on__refresh_source_landed(
                _RefreshSourceLanded("remote:peer", _snapshot(error="timeout"))
            )
            await pilot.press("y")
            await pilot.pause()
            self.assertEqual(
                calls, before, "a failed poll while confirmation is open must forbid kill"
            )
            app.on__refresh_source_landed(_RefreshSourceLanded("remote:peer", _snapshot()))
            await pilot.pause()
            table.focus()
            table.move_cursor(
                row=next(i for i, r in enumerate(app.screen._dashboard_rows) if r.host)
            )
            saved_row = app.screen._dashboard_rows[table.cursor_row]
            app.on__refresh_source_landed(
                _RefreshSourceLanded("remote:peer", _snapshot(error="timeout"))
            )
            # A request captured before poll completion still rechecks live state.
            app.screen._launch_flow.attach_row(saved_row)
            await pilot.pause()
            self.assertEqual(calls, before, "stale remote rows must not attach")
            app.on__refresh_source_landed(_RefreshSourceLanded("remote:peer", _snapshot()))
            await pilot.pause()
            table.focus()
            table.move_cursor(
                row=next(i for i, r in enumerate(app.screen._dashboard_rows) if r.host)
            )
            saved_row = app.screen._dashboard_rows[table.cursor_row]
            await pilot.press("d")
            app.on__refresh_source_landed(
                _RefreshSourceLanded("remote:peer", _snapshot(name="uxon-replacement@claude"))
            )
            self.assertIsNone(app.state.remote["peer"].last_error)
            await pilot.press("y")
            app.screen._launch_flow.attach_row(saved_row)
            await pilot.pause()
            self.assertEqual(
                calls,
                before,
                "a successful poll with the target absent must also deny stale requests",
            )
            app.on__refresh_source_landed(_RefreshSourceLanded("remote:peer", _snapshot()))
            await pilot.pause()
            table.focus()
            table.move_cursor(
                row=next(i for i, r in enumerate(app.screen._dashboard_rows) if r.host)
            )
            await pilot.press("enter", "d", "y")
            await pilot.pause()
            self.assertEqual(
                calls[len(before) :],
                [
                    ("attach", "peer", "bob", "uxon-remote@claude"),
                    ("kill", "peer", "bob", "uxon-remote@claude"),
                ],
            )
            self.assertEqual(refreshes[-1], {})

            # Restore real admission and use the mounted refresh binding.
            from uxon.tui.refresh import SourceSpec

            polled = []

            def poll():
                polled.append("fresh")
                return _snapshot()

            def incompatible_poll():
                polled.append("failed")
                return replace(_snapshot(error="schema mismatch"), error_code="update_remote")

            app.ctx.refresh_sources = [SourceSpec("remote:peer", incompatible_poll)]
            del app.kick_refresh
            app.kick_refresh()
            await pilot.pause()
            self.assertEqual(polled, ["failed"])
            self.assertEqual(app.state.remote["peer"].value.error_code, "update_remote")
            app.ctx.refresh_sources = [SourceSpec("remote:peer", poll)]
            app.kick_refresh()
            await pilot.pause()
            self.assertEqual(polled, ["failed"])
            await pilot.press("r")
            await pilot.pause()
            self.assertEqual(polled, ["failed", "fresh"])
            self.assertIsNone(app.state.remote["peer"].value.error)
            self.assertIn("uxon-remote", _visible(table))

    async def test_remote_and_local_user_latch_filter_order_and_focus_survive_refresh(self):
        ctx = _mk_ctx(sessions=[_session()], remote_hosts=[RemoteHost("peer", "peer", "", "uxon")])
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            _land(app, ctx)
            await pilot.pause()
            screen = app.screen
            table = screen.query_one("#sessions-dashboard", SessionListView)
            self.assertNotIn("user", [c.id for c in screen._active_columns])
            self.assertNotIn("USER", table.render_line(0).text)
            screen.query_one("#action-open").focus()
            await pilot.pause()
            self.assertEqual(app.focused.id, "action-open")
            _land(app, replace(ctx, sessions=[_session(cpu="2.0")]))
            await pilot.pause()
            self.assertEqual(app.focused.id, "action-open")
            table.focus()
            table.move_cursor(row=0)
            selected = table.selected_row_key
            app.on__refresh_source_landed(
                _RefreshSourceLanded("remote:peer", _snapshot(user="alice"))
            )
            await pilot.pause()
            self.assertNotIn("user", [c.id for c in screen._active_columns])
            app.on__refresh_source_landed(_RefreshSourceLanded("remote:peer", _snapshot()))
            await pilot.pause()
            self.assertIn("user", [c.id for c in screen._active_columns])
            self.assertIn("USER", table.render_line(0).text)
            self.assertEqual(app.main_ui.seen_users, {"alice", "bob"})
            self.assertIs(app.screen, screen)
            self.assertIs(app.focused, table)
            self.assertEqual(table.selected_row_key, selected)
            first_order = [r.key for r in screen._dashboard_rows]
            _land(
                app,
                replace(
                    ctx,
                    sessions=[_session(cpu="9.0")],
                    other_sessions=[_session("uxon-other@codex", user="dana")],
                ),
            )
            await pilot.pause()
            self.assertIn("9.0", _visible(table))
            self.assertEqual(
                [r.key for r in screen._dashboard_rows if r.key in first_order], first_order
            )
            self.assertEqual(table.selected_row_key, selected)
            await pilot.press("v", "s")
            from textual.widgets import Input

            app.screen.query_one("#search-bar Input", Input).value = "local"
            await pilot.pause()
            self.assertEqual([r.name for r in screen._dashboard_rows], ["uxon-local@claude"])
            self.assertIn("user", [c.id for c in screen._active_columns])
            _land(app, replace(ctx, sessions=[_session(cpu="8.0")]))
            await pilot.pause()
            self.assertEqual(screen._dashboard_ui.filter_text, "local")
            self.assertEqual(screen._dashboard_ui.view_mode, "by_host")
            self.assertIn("8.0", _visible(table))
            await pilot.press("escape", "escape", "v")
            await pilot.pause()
            table.focus()
            table.move_cursor(row=next(i for i, r in enumerate(screen._dashboard_rows) if r.host))
            focus_key = screen._current_focus_key()
            selected_remote = table.selected_row_key
            screen.query_one("#action-cwd").focus()
            self.assertTrue(screen._focus_key(focus_key))
            await pilot.pause()
            self.assertIs(app.focused, table)
            self.assertEqual(table.selected_row_key, selected_remote)
            app.on__refresh_source_landed(
                _RefreshSourceLanded("remote:peer", _snapshot(name="uxon-new@claude"))
            )
            await pilot.pause()
            self.assertFalse(screen._focus_key(focus_key), "a disappeared row cannot be restored")
            self.assertNotEqual(table.selected_row_key, focus_key.removeprefix("dashboard:"))
            self.assertIn(table.selected_row_key, [r.key for r in screen._dashboard_rows])
            self.assertIs(app.focused, table)
            selected_row = screen._dashboard_rows[table.cursor_row]
            self.assertIn(selected_row.short.split("@")[0], _visible(table))
            _land(app, replace(ctx, sessions=[]))
            app.on__refresh_source_landed(
                _RefreshSourceLanded("remote:peer", RemoteSnapshot("peer", 2, None))
            )
            await pilot.pause()
            self.assertEqual(table.row_count, 0)
            self.assertIn("user", [c.id for c in screen._active_columns])
            self.assertEqual(app.main_ui.seen_users, {"alice", "bob", "dana"})

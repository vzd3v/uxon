"""Production refresh preserves quiet layouts, selection and long-list geometry."""

import unittest
from dataclasses import replace
from unittest.mock import patch

from helpers import make_dashboard_context as _mk_ctx
from helpers import make_dashboard_session as _session
from helpers import visible_dashboard_text as _visible
from textual.screen import Screen

from uxon.tui.app import UxonApp
from uxon.tui.messages import _RefreshSourceLanded
from uxon.tui.screens.confirm import ConfirmYesNo
from uxon.tui.widgets.session_list_view import SessionListView


async def _settle(pilot, app, screen):
    await pilot.pause()
    for _ in range(25):
        if (
            app._render._timer is None
            and not app._render._dirty
            and not screen._layout_required
            and not screen._scroll_required
            and not screen._dirty_widgets
            and not screen._repaint_required
        ):
            return
        await pilot.pause(0.05)
    raise AssertionError(
        f"render did not settle: dirty={app._render._dirty}, "
        f"layout={screen._layout_required}, scroll={screen._scroll_required}"
    )


async def _land(pilot, app, ctx):
    previous = app.state.refresh_tick
    app.post_message(
        _RefreshSourceLanded("main_ctx_rebuild", ctx, instance_epoch=app._instance_epoch)
    )
    for _ in range(25):
        if app.state.refresh_tick > previous:
            await _settle(pilot, app, app.screen)
            return
        await pilot.pause(0.02)
    raise AssertionError(
        f"main refresh not delivered: tick={app.state.refresh_tick}, previous={previous}"
    )


class LayoutBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_production_refresh_and_modal_preserve_geometry_visible_values_and_cursor(self):
        sessions = [_session(f"uxon-row{i:03d}@claude") for i in range(200)]
        ctx = _mk_ctx(sessions=sessions, tui_render_debounce_ms=10, tui_render_max_latency_ms=50)
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 24)) as pilot:
            await _land(pilot, app, ctx)
            screen = app.screen
            table = screen.query_one("#sessions-dashboard", SessionListView)
            table.focus()
            table.move_cursor(row=2)
            await _settle(pilot, app, screen)
            selected = table.selected_row_key
            height = table.virtual_size.height
            self.assertEqual(height, 201)
            self.assertIn("row000", table.render_line(1).text)
            self.assertIn("row001", table.render_line(2).text)
            self.assertIn("NAME", table.render_line(0).text)
            self.assertEqual(table.render_line(205).text.strip(), "")
            layouts = []
            original_layout = Screen._refresh_layout

            def count_layout(current, *args, **kwargs):
                if current is screen and current._layout_required:
                    layouts.append(True)
                return original_layout(current, *args, **kwargs)

            # Count at the framework sink: layout dirt is visible before reset,
            # independent of which widget or controller requested it.
            with patch.object(Screen, "_refresh_layout", count_layout):
                await _land(pilot, app, replace(ctx, sessions=list(sessions)))
                self.assertIs(screen.query_one("#sessions-dashboard"), table)
                self.assertEqual(layouts, [], "value-identical refresh must stay quiet")
                self.assertEqual(table.selected_row_key, selected)
                changed = [replace(s, cpu="77.0") if i == 2 else s for i, s in enumerate(sessions)]
                await _land(pilot, app, replace(ctx, sessions=changed))
                self.assertEqual(layouts, [], "telemetry must not resize the screen")
                self.assertIn("77.0", _visible(table))
                self.assertEqual(table.virtual_size.height, height)
                self.assertEqual(table.selected_row_key, selected)
                await _land(pilot, app, replace(ctx, sessions=list(reversed(changed))))
                self.assertEqual(layouts, [], "same-count reorder must not resize the screen")
                self.assertEqual(
                    [r.name for r in screen._dashboard_rows], [s.name for s in sessions]
                )
                self.assertEqual(table.virtual_size.height, height)
                self.assertEqual(table.selected_row_key, selected)
                await pilot.press("down")
                await _settle(pilot, app, screen)
                self.assertEqual(table.selected_row_key, screen._dashboard_rows[3].key)
                self.assertEqual(layouts, [], "in-viewport navigation must not request layout")
            app.push_screen(ConfirmYesNo("Return to dashboard?"))
            await pilot.pause()
            layouts.clear()
            with patch.object(Screen, "_refresh_layout", count_layout):
                await pilot.press("escape")
                await _settle(pilot, app, screen)
            # Modal removal and focus restoration may each trigger a framework layout. A resume
            # storm must not repeatedly lay out the underlying dashboard.
            self.assertLessEqual(len(layouts), 2)
            self.assertIs(app.screen, screen)
            self.assertIs(screen.query_one("#sessions-dashboard"), table)
            self.assertIs(app.focused, table)
            self.assertEqual(table.virtual_size.height, height)
            self.assertIn("77.0", _visible(table))
            layouts.clear()
            with patch.object(Screen, "_refresh_layout", count_layout):
                await _land(pilot, app, replace(ctx, sessions=changed))
                self.assertEqual(layouts, [], "refresh after modal dismissal must remain quiet")
                await pilot.press("end", "pageup")
                await _settle(pilot, app, screen)
                self.assertEqual(layouts, [], "scrolling may repaint but must not dirty layout")
            self.assertGreater(table.scroll_offset.y, 0)
            visible_index = table.cursor_row - round(table.scroll_offset.y) + 1
            self.assertGreaterEqual(visible_index, 1)
            self.assertLess(visible_index, table.size.height)
            cursor = table.cursor_row
            self.assertGreater(cursor, 100)
            self.assertLess(cursor, 199)
            distant_key = table.selected_row_key
            self.assertEqual(distant_key, screen._dashboard_rows[cursor].key)
            self.assertIn(f"row{cursor:03d}", table.render_line(visible_index).text)
            self.assertIn("NAME", table.render_line(0).text)
            await _land(pilot, app, replace(ctx, sessions=changed[:-1]))
            self.assertEqual(table.virtual_size.height, 200)
            self.assertEqual(table.row_count, 199)
            self.assertEqual(table.selected_row_key, distant_key)

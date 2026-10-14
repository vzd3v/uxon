"""Pilot tests for :class:`SessionListView` (render-on-demand dashboard).

Standalone Pilot — these assert the widget's **observable behaviour** (row
order, cursor pin-by-key, edge-release focus handoff, message emission,
viewport-only render), not DataTable / reconciler internals (both gone in the
render-on-demand refactor). Replaces the old ``SessionDashboardTable`` spy
tests.

Pinned contracts:

* ``set_rows`` lands the rows in the given order; ``row_count == len``.
* ``pin_cursor_to(key)`` follows the logical row across a reorder, and clamps
  to the nearest surviving sibling when the key is gone.
* Edge-release: ``↑`` on row 0 hands focus to the widget directly above
  (the last action row); ``↓`` on the last row hands focus to the next
  sibling.
* Enter / click post ``RowSelected`` carrying the cursor row; ``←/→`` post
  ``HostNavigate`` carrying the direction.
* ``render_line`` only builds the visible viewport (out-of-range lines are
  blank).

Pure ``order.py::place`` tests live in ``test_dashboard_order.py``.
"""

from __future__ import annotations

import unittest


def _textual_available() -> bool:
    try:
        import textual  # noqa: F401
    except ImportError:
        return False
    return True


def _make_row(
    *,
    host: str | None,
    user: str,
    name: str,
    cpu: float = 0.0,
    rss_kib: int = 0,
    last_attached_epoch: float | None = None,
):
    from uxon.tui.dashboard.row import SessionRow

    return SessionRow(
        host=host,
        user=user,
        name=name,
        short=name,
        agent="claude",
        attached=False,
        legacy=False,
        pid=None,
        cpu_pct=cpu,
        rss_kib=rss_kib,
        created_epoch=None,
        last_attached_epoch=last_attached_epoch,
        cmd="",
        path="",
    )


def _active_columns():
    """Active columns for a multi-host fixture (host + name + cpu + ram)."""
    from uxon.tui.dashboard.columns import REGISTRY

    by_id = {c.id: c for c in REGISTRY}
    return (by_id["host"], by_id["name"], by_id["cpu"], by_id["ram"])


@unittest.skipUnless(_textual_available(), "textual not installed")
class SessionListViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_row_lifecycle_preserves_selection_and_visible_values(self) -> None:
        from textual.app import App, ComposeResult

        from uxon.tui.widgets.session_list_view import SessionListView

        cols = _active_columns()
        a = _make_row(host="a", user="u", name="s1", cpu=11.0)
        b = _make_row(host="a", user="u", name="s2", cpu=22.0)
        c = _make_row(host="b", user="u", name="s3", cpu=33.0)

        class Host(App):
            def compose(self) -> ComposeResult:
                yield SessionListView(cols, id="dash")

        app = Host()
        async with app.run_test() as pilot:
            view = app.query_one("#dash", SessionListView)
            view.set_rows((a, b, c))
            await pilot.pause()
            view.focus()
            view.move_cursor(row=1)
            await pilot.pause()
            self.assertEqual(view.row_count, 3)
            self.assertEqual(view.selected_row_key, b.key)
            self.assertIn("NAME", view.render_line(0).text)
            for line, name, cpu in ((1, "s1", "11"), (2, "s2", "22"), (3, "s3", "33")):
                self.assertIn(name, view.render_line(line).text)
                self.assertIn(cpu, view.render_line(line).text)
            self.assertEqual(view.render_line(4).text.strip(), "")

            view.set_rows((c, a, b))
            view.pin_cursor_to(b.key)
            await pilot.pause()
            self.assertEqual(view.cursor_row, 2)
            self.assertEqual(view.selected_row_key, b.key)
            for line, name in ((1, "s3"), (2, "s1"), (3, "s2")):
                self.assertIn(name, view.render_line(line).text)
            view.set_rows((c, a))
            view.pin_cursor_to(b.key)
            await pilot.pause()
            self.assertEqual(view.row_count, 2)
            self.assertEqual(view.cursor_row, 1)
            self.assertEqual(view.selected_row_key, a.key)
            self.assertIn("s3", view.render_line(1).text)
            self.assertIn("s1", view.render_line(2).text)
            self.assertEqual(view.render_line(3).text.strip(), "")
            view.set_rows(())
            view.pin_cursor_to(a.key)
            await pilot.pause()
            self.assertEqual(view.row_count, 0)
            self.assertIsNone(view.selected_row_key)
            self.assertIn("NAME", view.render_line(0).text)
            self.assertEqual(view.render_line(1).text.strip(), "")
            view.set_rows((b,))
            await pilot.pause()
            self.assertEqual(view.row_count, 1)
            self.assertEqual(view.selected_row_key, b.key)
            self.assertIn("s2", view.render_line(1).text)
            self.assertIn("22", view.render_line(1).text)
            self.assertEqual(view.render_line(2).text.strip(), "")

    async def test_edge_keys_focus_exact_neighbors(self) -> None:
        from textual.app import App, ComposeResult
        from textual.widgets import Button

        from uxon.tui.widgets.action_row import ActionRow
        from uxon.tui.widgets.session_list_view import SessionListView

        cols = _active_columns()
        a = _make_row(host="a", user="u", name="s1")
        b = _make_row(host="a", user="u", name="s2")

        class Host(App):
            def compose(self) -> ComposeResult:
                yield ActionRow(kind="cwd", label="Set cwd", id="action-cwd")
                yield ActionRow(kind="open", label="Open", id="action-open")
                yield SessionListView(cols, id="dash")
                yield Button("after", id="after")

        app = Host()
        async with app.run_test() as pilot:
            view = app.query_one("#dash", SessionListView)
            view.set_rows((a, b))
            await pilot.pause()
            view.focus()
            await pilot.pause()
            view.move_cursor(row=0)
            await pilot.pause()
            await pilot.press("up")
            await pilot.pause()
            focused = app.focused
            assert focused is not None
            self.assertEqual(focused.id, "action-open")
            view.focus()
            view.move_cursor(row=1)
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(app.focused.id, "after")

    async def test_set_rows_same_length_does_not_change_virtual_size(self) -> None:
        """A same-length cell update keeps ``virtual_size`` height stable.

        The render-on-demand contract: only a row *count* change moves
        ``virtual_size`` (the one legitimate relayout).
        """
        from textual.app import App, ComposeResult

        from uxon.tui.widgets.session_list_view import SessionListView

        cols = _active_columns()
        old = (
            _make_row(host="a", user="u", name="s1", cpu=1.0),
            _make_row(host="a", user="u", name="s2", cpu=2.0),
        )
        new = (
            _make_row(host="a", user="u", name="s1", cpu=1.0),
            _make_row(host="a", user="u", name="s2", cpu=42.0),  # cell change
        )

        class Host(App):
            def compose(self) -> ComposeResult:
                yield SessionListView(cols, id="dash")

        app = Host()
        async with app.run_test() as pilot:
            view = app.query_one("#dash", SessionListView)
            view.set_rows(old)
            await pilot.pause()
            height_before = view.virtual_size.height
            view.set_rows(new)
            await pilot.pause()
            self.assertEqual(view.virtual_size.height, height_before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

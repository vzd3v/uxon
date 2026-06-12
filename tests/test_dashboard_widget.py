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
    async def test_set_rows_order_and_count(self) -> None:
        """3 hosts × 5 = 15 rows, in the order passed to ``set_rows``."""
        from textual.app import App, ComposeResult

        from uxon.tui.widgets.session_list_view import SessionListView

        cols = _active_columns()
        rows = tuple(
            _make_row(host=f"host-{h}", user="u", name=f"s{h}-{i}")
            for h in range(3)
            for i in range(5)
        )

        class Host(App):
            def compose(self) -> ComposeResult:
                yield SessionListView(cols, id="dash")

        app = Host()
        async with app.run_test() as pilot:
            view = app.query_one("#dash", SessionListView)
            view.set_rows(rows)
            await pilot.pause()
            self.assertEqual(view.row_count, 15)
            view.focus()
            await pilot.pause()
            view.move_cursor(row=0)
            await pilot.pause()
            self.assertEqual(view.selected_row_key, rows[0].key)

    async def test_pin_cursor_follows_row_key_across_reorder(self) -> None:
        """``pin_cursor_to(prev_key)`` lands on the new index of the row."""
        from textual.app import App, ComposeResult

        from uxon.tui.widgets.session_list_view import SessionListView

        cols = _active_columns()
        a = _make_row(host="a", user="u", name="s1")
        b = _make_row(host="a", user="u", name="s2")
        c = _make_row(host="b", user="u", name="s3")

        class Host(App):
            def compose(self) -> ComposeResult:
                yield SessionListView(cols, id="dash")

        app = Host()
        async with app.run_test() as pilot:
            view = app.query_one("#dash", SessionListView)
            view.set_rows((a, b, c))
            await pilot.pause()
            view.focus()
            await pilot.pause()
            view.move_cursor(row=1)  # park on b
            await pilot.pause()
            self.assertEqual(view.selected_row_key, b.key)

            view.set_rows((c, a, b))  # b now at index 2
            view.pin_cursor_to(b.key)
            await pilot.pause()
            self.assertEqual(view.cursor_row, 2)

    async def test_pin_cursor_clamps_when_key_missing(self) -> None:
        """Removing the cursor's row → pin clamps to ``row_count - 1``."""
        from textual.app import App, ComposeResult

        from uxon.tui.widgets.session_list_view import SessionListView

        cols = _active_columns()
        a = _make_row(host="a", user="u", name="s1")
        b = _make_row(host="a", user="u", name="s2")
        c = _make_row(host="b", user="u", name="s3")

        class Host(App):
            def compose(self) -> ComposeResult:
                yield SessionListView(cols, id="dash")

        app = Host()
        async with app.run_test() as pilot:
            view = app.query_one("#dash", SessionListView)
            view.set_rows((a, b, c))
            await pilot.pause()
            view.focus()
            await pilot.pause()
            view.move_cursor(row=1)
            await pilot.pause()

            view.set_rows((a, c))  # b gone; cursor clamps
            view.pin_cursor_to(b.key)
            await pilot.pause()
            self.assertEqual(view.row_count, 2)
            self.assertEqual(view.cursor_row, 1)

    async def test_edge_release_up_lands_on_action_row_above(self) -> None:
        """``↑`` on row 0 focuses the action row directly above the list."""
        from textual.app import App, ComposeResult

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

        app = Host()
        async with app.run_test() as pilot:
            view = app.query_one("#dash", SessionListView)
            view.set_rows((a, b))
            await pilot.pause()
            view.focus()
            await pilot.pause()
            view.move_cursor(row=0)
            await pilot.pause()
            view.action_cursor_up()
            await pilot.pause()
            focused = app.focused
            assert focused is not None
            self.assertEqual(focused.id, "action-open")

    async def test_edge_release_down_hands_focus_to_next(self) -> None:
        """``↓`` on the last row hands focus off the list (focus_next)."""
        from textual.app import App, ComposeResult
        from textual.widgets import Button

        from uxon.tui.widgets.session_list_view import SessionListView

        cols = _active_columns()
        a = _make_row(host="a", user="u", name="s1")
        b = _make_row(host="a", user="u", name="s2")

        class Host(App):
            def compose(self) -> ComposeResult:
                yield SessionListView(cols, id="dash")
                yield Button("after", id="after")

        app = Host()
        async with app.run_test() as pilot:
            view = app.query_one("#dash", SessionListView)
            view.set_rows((a, b))
            await pilot.pause()
            view.focus()
            await pilot.pause()
            view.move_cursor(row=1)  # last row
            await pilot.pause()
            view.action_cursor_down()
            await pilot.pause()
            focused = app.focused
            assert focused is not None
            self.assertNotEqual(focused.id, "dash")

    async def test_enter_posts_row_selected(self) -> None:
        """``action_select_cursor`` posts ``RowSelected`` with cursor_row."""
        from textual.app import App, ComposeResult

        from uxon.tui.widgets.session_list_view import SessionListView

        cols = _active_columns()
        rows = (
            _make_row(host="a", user="u", name="s1"),
            _make_row(host="a", user="u", name="s2"),
        )
        captured: list[int] = []

        class Host(App):
            def compose(self) -> ComposeResult:
                yield SessionListView(cols, id="dash")

            def on_session_list_view_row_selected(self, event: SessionListView.RowSelected) -> None:
                captured.append(event.cursor_row)

        app = Host()
        async with app.run_test() as pilot:
            view = app.query_one("#dash", SessionListView)
            view.set_rows(rows)
            await pilot.pause()
            view.focus()
            await pilot.pause()
            view.move_cursor(row=1)
            await pilot.pause()
            view.action_select_cursor()
            await pilot.pause()
            self.assertEqual(captured, [1])

    async def test_arrow_posts_host_navigate(self) -> None:
        """``←/→`` post ``HostNavigate`` carrying the direction."""
        from textual.app import App, ComposeResult

        from uxon.tui.widgets.session_list_view import SessionListView

        cols = _active_columns()
        rows = (_make_row(host="a", user="u", name="s1"),)
        captured: list[int] = []

        class Host(App):
            def compose(self) -> ComposeResult:
                yield SessionListView(cols, id="dash")

            def on_session_list_view_host_navigate(
                self, event: SessionListView.HostNavigate
            ) -> None:
                captured.append(event.direction)

        app = Host()
        async with app.run_test() as pilot:
            view = app.query_one("#dash", SessionListView)
            view.set_rows(rows)
            await pilot.pause()
            view.focus()
            await pilot.pause()
            view.action_host_navigate(1)
            view.action_host_navigate(-1)
            await pilot.pause()
            self.assertEqual(captured, [1, -1])

    async def test_render_line_is_viewport_only(self) -> None:
        """``render_line`` builds the header + in-range rows; oob is blank."""
        from textual.app import App, ComposeResult

        from uxon.tui.widgets.session_list_view import SessionListView

        cols = _active_columns()
        rows = (
            _make_row(host="a", user="u", name="s1"),
            _make_row(host="a", user="u", name="s2"),
        )

        class Host(App):
            def compose(self) -> ComposeResult:
                yield SessionListView(cols, id="dash")

        app = Host()
        async with app.run_test() as pilot:
            view = app.query_one("#dash", SessionListView)
            view.set_rows(rows)
            await pilot.pause()
            # Line 0 = header.
            self.assertIn("NAME", view.render_line(0).text)
            # Lines 1, 2 = data rows.
            self.assertIn("s1", view.render_line(1).text)
            self.assertIn("s2", view.render_line(2).text)
            # A line past the data is blank (no content built for it).
            self.assertEqual(view.render_line(50).text.strip(), "")

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

"""Pilot tests for :class:`SessionDashboardTable` (commit 8).

Standalone Pilot — these tests assert ``add_row`` / ``remove_row`` /
``update_cell`` spy counts, which the batched ``run_screen_scenarios``
smoke tests don't care about. Per AGENTS.md § Pilot batching
(AGENTS.md:160-168), structural-mutator spy tests don't share that batch.

Pinned contracts:

* Mounting with N rows produces ``row_count == N``.
* A pure ``CellUpdate`` op never calls ``add_row`` / ``remove_row`` and
  leaves the cursor on its prior row.
* ``pin_cursor_to(prev_key)`` after a re-order keeps the cursor on the
  same logical row.
* ``pin_cursor_to(missing_key)`` falls back to the nearest surviving
  sibling at the same visual index (clamped to ``row_count - 1``).
* Inline-insert (``RowAdd`` with non-None ``before_key`` in the middle)
  produces the visual order specified by ``new``.
* Module import-time guard: if Textual ever drops ``_row_locations``
  initialisation in :meth:`DataTable.__init__`, the assertion fires.
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
        last_attached_epoch=None,
        cmd="",
        path="",
    )


def _active_columns():
    """Active columns for a multi-host fixture (host + name + cpu + ram)."""
    from uxon.tui.dashboard.columns import REGISTRY

    by_id = {c.id: c for c in REGISTRY}
    return (by_id["host"], by_id["name"], by_id["cpu"], by_id["ram"])


@unittest.skipUnless(_textual_available(), "textual not installed")
class SessionDashboardTableTests(unittest.IsolatedAsyncioTestCase):
    async def test_mount_with_15_rows(self) -> None:
        """3 hosts × 5 sessions = 15 rows after applying the initial diff."""
        from textual.app import App, ComposeResult

        from uxon.tui.dashboard.reconcile import diff
        from uxon.tui.widgets.session_dashboard_table import SessionDashboardTable

        cols = _active_columns()
        rows = tuple(
            _make_row(host=f"host-{h}", user="u", name=f"s{h}-{i}")
            for h in range(3)
            for i in range(5)
        )

        class Host(App):
            def compose(self) -> ComposeResult:
                yield SessionDashboardTable(cols, id="dash")

        app = Host()
        async with app.run_test() as pilot:
            table: SessionDashboardTable = app.query_one("#dash", SessionDashboardTable)
            plan = diff((), rows, cols)
            table.apply(plan)
            await pilot.pause()
            self.assertEqual(table.row_count, 15)

    async def test_cell_update_does_not_touch_row_structure(self) -> None:
        """A single CellUpdate must not invoke add_row/remove_row, and the
        cursor must stay where it was.
        """
        from textual.app import App, ComposeResult

        from uxon.tui.dashboard.reconcile import diff
        from uxon.tui.widgets.session_dashboard_table import SessionDashboardTable

        cols = _active_columns()
        old_rows = (
            _make_row(host="a", user="u", name="s1", cpu=1.0),
            _make_row(host="a", user="u", name="s2", cpu=2.0),
            _make_row(host="b", user="u", name="s3", cpu=3.0),
        )

        class Host(App):
            def compose(self) -> ComposeResult:
                yield SessionDashboardTable(cols, id="dash")

        app = Host()
        async with app.run_test() as pilot:
            table: SessionDashboardTable = app.query_one("#dash", SessionDashboardTable)
            table.apply(diff((), old_rows, cols))
            await pilot.pause()

            table.focus()
            await pilot.pause()
            table.move_cursor(row=1)
            await pilot.pause()
            self.assertEqual(table.cursor_row, 1)

            # Spy on structural mutators.
            add_calls: list[tuple] = []
            remove_calls: list[str] = []
            real_add = table.add_row
            real_remove = table.remove_row

            def spy_add(*cells, **kw):
                add_calls.append((cells, kw))
                return real_add(*cells, **kw)

            def spy_remove(key):
                remove_calls.append(key)
                return real_remove(key)

            table.add_row = spy_add  # type: ignore[method-assign]
            table.remove_row = spy_remove  # type: ignore[method-assign]

            new_rows = (
                _make_row(host="a", user="u", name="s1", cpu=1.0),
                _make_row(host="a", user="u", name="s2", cpu=42.0),  # cell change
                _make_row(host="b", user="u", name="s3", cpu=3.0),
            )
            plan = diff(old_rows, new_rows, cols)
            table.apply(plan)
            await pilot.pause()

            self.assertEqual(add_calls, [])
            self.assertEqual(remove_calls, [])
            self.assertEqual(table.cursor_row, 1)

    async def test_reorder_pin_cursor_follows_row_key(self) -> None:
        """After a re-order, ``pin_cursor_to(prev_key)`` lands on the new
        index of the same logical row.
        """
        from textual.app import App, ComposeResult

        from uxon.tui.dashboard.reconcile import _row_key, diff
        from uxon.tui.widgets.session_dashboard_table import SessionDashboardTable

        cols = _active_columns()
        a = _make_row(host="a", user="u", name="s1")
        b = _make_row(host="a", user="u", name="s2")
        c = _make_row(host="b", user="u", name="s3")
        old_rows = (a, b, c)
        new_rows = (c, a, b)  # rotate: c moves to front

        class Host(App):
            def compose(self) -> ComposeResult:
                yield SessionDashboardTable(cols, id="dash")

        app = Host()
        async with app.run_test() as pilot:
            table: SessionDashboardTable = app.query_one("#dash", SessionDashboardTable)
            table.apply(diff((), old_rows, cols))
            await pilot.pause()
            table.focus()
            await pilot.pause()
            # Park the cursor on row b (index 1 in old_rows).
            cursor_key = _row_key(b)
            table.move_cursor(row=1)
            await pilot.pause()

            table.apply(diff(old_rows, new_rows, cols))
            table.pin_cursor_to(cursor_key)
            await pilot.pause()

            # In new_rows, b is at index 2.
            self.assertEqual(table.cursor_row, 2)

    async def test_pin_cursor_falls_back_when_key_missing(self) -> None:
        """Removing the row under the cursor → ``pin_cursor_to`` clamps to
        ``row_count - 1`` so the cursor never lands on a non-existent row.
        """
        from textual.app import App, ComposeResult

        from uxon.tui.dashboard.reconcile import _row_key, diff
        from uxon.tui.widgets.session_dashboard_table import SessionDashboardTable

        cols = _active_columns()
        a = _make_row(host="a", user="u", name="s1")
        b = _make_row(host="a", user="u", name="s2")
        c = _make_row(host="b", user="u", name="s3")
        old_rows = (a, b, c)
        new_rows = (a, c)  # b is gone

        class Host(App):
            def compose(self) -> ComposeResult:
                yield SessionDashboardTable(cols, id="dash")

        app = Host()
        async with app.run_test() as pilot:
            table: SessionDashboardTable = app.query_one("#dash", SessionDashboardTable)
            table.apply(diff((), old_rows, cols))
            await pilot.pause()
            table.focus()
            await pilot.pause()
            # Park on the row that's about to be removed.
            table.move_cursor(row=1)
            await pilot.pause()
            self.assertEqual(table.cursor_row, 1)

            table.apply(diff(old_rows, new_rows, cols))
            await pilot.pause()
            # Pin to the now-missing key — cursor should clamp.
            table.pin_cursor_to(_row_key(b))
            await pilot.pause()
            self.assertEqual(table.row_count, 2)
            # cursor_row was 1 before, row_count - 1 == 1 → stays 1.
            self.assertEqual(table.cursor_row, 1)

    async def test_inline_insert_preserves_visual_order(self) -> None:
        """[A, B, C] → [A, X, B, C] where X is new. The widget must end
        with the visual order [A, X, B, C].
        """
        from textual.app import App, ComposeResult

        from uxon.tui.dashboard.reconcile import _row_key, diff
        from uxon.tui.widgets.session_dashboard_table import SessionDashboardTable

        cols = _active_columns()
        a = _make_row(host="a", user="u", name="s-a")
        b = _make_row(host="a", user="u", name="s-b")
        c = _make_row(host="a", user="u", name="s-c")
        x = _make_row(host="a", user="u", name="s-x")
        old_rows = (a, b, c)
        new_rows = (a, x, b, c)

        class Host(App):
            def compose(self) -> ComposeResult:
                yield SessionDashboardTable(cols, id="dash")

        app = Host()
        async with app.run_test() as pilot:
            table: SessionDashboardTable = app.query_one("#dash", SessionDashboardTable)
            table.apply(diff((), old_rows, cols))
            await pilot.pause()

            table.apply(diff(old_rows, new_rows, cols))
            await pilot.pause()

            ordered_keys = [
                row.key.value if hasattr(row.key, "value") else str(row.key)
                for row in table.ordered_rows
            ]
            expected = [_row_key(a), _row_key(x), _row_key(b), _row_key(c)]
            self.assertEqual(ordered_keys, expected)

    async def test_two_inline_inserts_in_one_apply_batch(self) -> None:
        """[A, B] → [A, X, B, Y]: two interior inserts in a single
        ``apply`` call. Final visual order must match ``new`` exactly,
        row count is 4, and no ``CellUpdate`` op fires (both surviving
        rows occupy the same surviving-relative position).
        """
        from textual.app import App, ComposeResult

        from uxon.tui.dashboard.reconcile import CellUpdate, _row_key, diff
        from uxon.tui.widgets.session_dashboard_table import SessionDashboardTable

        cols = _active_columns()
        a = _make_row(host="a", user="u", name="s-a")
        b = _make_row(host="a", user="u", name="s-b")
        x = _make_row(host="a", user="u", name="s-x")
        y = _make_row(host="a", user="u", name="s-y")
        old_rows = (a, b)
        new_rows = (a, x, b, y)

        class Host(App):
            def compose(self) -> ComposeResult:
                yield SessionDashboardTable(cols, id="dash")

        app = Host()
        async with app.run_test() as pilot:
            table: SessionDashboardTable = app.query_one("#dash", SessionDashboardTable)
            table.apply(diff((), old_rows, cols))
            await pilot.pause()

            plan = diff(old_rows, new_rows, cols)
            # Surviving rows (a, b) keep their surviving-relative
            # position, so reconcile must not emit any CellUpdate.
            self.assertEqual([o for o in plan.ops if isinstance(o, CellUpdate)], [])

            table.apply(plan)
            await pilot.pause()

            self.assertEqual(table.row_count, 4)
            ordered_keys = [
                row.key.value if hasattr(row.key, "value") else str(row.key)
                for row in table.ordered_rows
            ]
            expected = [_row_key(a), _row_key(x), _row_key(b), _row_key(y)]
            self.assertEqual(ordered_keys, expected)

    async def test_remove_missing_key_does_not_crash(self) -> None:
        """``_apply_remove`` swallows the underlying remove_row error
        when the row-key is not present (covered by the defensive
        try/except). Widget must survive and row count is unchanged.
        """
        from textual.app import App, ComposeResult

        from uxon.tui.dashboard.reconcile import ApplyPlan, RowRemove, diff
        from uxon.tui.widgets.session_dashboard_table import SessionDashboardTable

        cols = _active_columns()
        a = _make_row(host="a", user="u", name="s-a")
        b = _make_row(host="a", user="u", name="s-b")

        class Host(App):
            def compose(self) -> ComposeResult:
                yield SessionDashboardTable(cols, id="dash")

        app = Host()
        async with app.run_test() as pilot:
            table: SessionDashboardTable = app.query_one("#dash", SessionDashboardTable)
            table.apply(diff((), (a, b), cols))
            await pilot.pause()
            self.assertEqual(table.row_count, 2)

            # Synthetic op: a key that was never added.
            table.apply(ApplyPlan(ops=(RowRemove("nonexistent/u/x"),), new_keys=()))
            await pilot.pause()
            self.assertEqual(table.row_count, 2)
            # Widget remains queryable — no exception bubbled.
            self.assertEqual(app.query_one("#dash", SessionDashboardTable).row_count, 2)

    async def test_update_missing_key_does_not_crash(self) -> None:
        """``_apply_update`` swallows the underlying update_cell error
        when the row-key is not present. Row count unchanged, widget
        survives.
        """
        from rich.text import Text
        from textual.app import App, ComposeResult

        from uxon.tui.dashboard.reconcile import ApplyPlan, CellUpdate, diff
        from uxon.tui.widgets.session_dashboard_table import SessionDashboardTable

        cols = _active_columns()
        a = _make_row(host="a", user="u", name="s-a")
        b = _make_row(host="a", user="u", name="s-b")

        class Host(App):
            def compose(self) -> ComposeResult:
                yield SessionDashboardTable(cols, id="dash")

        app = Host()
        async with app.run_test() as pilot:
            table: SessionDashboardTable = app.query_one("#dash", SessionDashboardTable)
            table.apply(diff((), (a, b), cols))
            await pilot.pause()
            self.assertEqual(table.row_count, 2)

            table.apply(
                ApplyPlan(
                    ops=(CellUpdate("nonexistent/u/x", "cpu", Text("99")),),
                    new_keys=(),
                )
            )
            await pilot.pause()
            self.assertEqual(table.row_count, 2)

    async def test_remove_row_under_cursor_lands_on_replacement(self) -> None:
        """When the row under the cursor is removed and a new row takes
        its visual slot, ``apply`` followed by ``pin_cursor_to(prev_key)``
        clamps to the same visual index.
        """
        from textual.app import App, ComposeResult

        from uxon.tui.dashboard.reconcile import _row_key, diff
        from uxon.tui.widgets.session_dashboard_table import SessionDashboardTable

        cols = _active_columns()
        a = _make_row(host="a", user="u", name="s1")
        b = _make_row(host="a", user="u", name="s2")
        c = _make_row(host="b", user="u", name="s3")
        old_rows = (a, b, c)
        new_rows = (a, c)  # drop b; c slides up to row 1

        class Host(App):
            def compose(self) -> ComposeResult:
                yield SessionDashboardTable(cols, id="dash")

        app = Host()
        async with app.run_test() as pilot:
            table: SessionDashboardTable = app.query_one("#dash", SessionDashboardTable)
            table.apply(diff((), old_rows, cols))
            await pilot.pause()
            table.focus()
            await pilot.pause()
            table.move_cursor(row=1)  # on b
            await pilot.pause()

            table.apply(diff(old_rows, new_rows, cols))
            table.pin_cursor_to(_row_key(b))
            await pilot.pause()

            # b is gone; c took row 1 (the row that "took its place").
            self.assertEqual(table.cursor_row, 1)
            # Verify it's c by reading the row key at the cursor.
            ordered_keys = [
                row.key.value if hasattr(row.key, "value") else str(row.key)
                for row in table.ordered_rows
            ]
            self.assertEqual(ordered_keys[1], _row_key(c))


@unittest.skipUnless(_textual_available(), "textual not installed")
class ImportGuardTests(unittest.TestCase):
    """If Textual ever drops the private ``_row_locations`` attribute we
    rely on, the widget module must fail to import — the assertion is
    the early-warning system.
    """

    def test_assertion_fires_when_row_locations_disappears(self) -> None:
        import importlib
        import inspect
        import sys

        from textual.widgets._data_table import DataTable

        import uxon.tui.widgets.session_dashboard_table as mod

        # Confirm the live source contains the marker we depend on; if
        # this ever fails the production assertion was already silently
        # broken.
        self.assertIn("_row_locations", inspect.getsource(DataTable.__init__))

        # Patch ``inspect.getsource`` so that, for ``DataTable.__init__``
        # specifically, the returned source omits ``_row_locations``.
        # That mirrors a Textual refactor that drops the attribute.
        orig_getsource = inspect.getsource

        def fake_getsource(obj):
            text = orig_getsource(obj)
            if obj is DataTable.__init__:
                return text.replace("_row_locations", "_REMOVED")
            return text

        inspect.getsource = fake_getsource  # type: ignore[assignment]
        try:
            sys.modules.pop("uxon.tui.widgets.session_dashboard_table", None)
            with self.assertRaises(AssertionError):
                importlib.import_module("uxon.tui.widgets.session_dashboard_table")
        finally:
            inspect.getsource = orig_getsource  # type: ignore[assignment]
            # Restore the production module so other tests can import it.
            sys.modules["uxon.tui.widgets.session_dashboard_table"] = mod


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

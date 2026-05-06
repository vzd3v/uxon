"""Tests for :mod:`uxon.tui.dashboard.reconcile`.

These tests pin the *exact* op stream produced by the diff for a small
set of representative cases. Because the widget (commit 8) replays
the stream verbatim, structural correctness is not enough — the
sequence (removes first, then walked-new-order) is part of the
contract.
"""

from __future__ import annotations

import unittest

from rich.text import Text

from uxon.tui.dashboard.columns import REGISTRY
from uxon.tui.dashboard.reconcile import (
    CellUpdate,
    RowAdd,
    RowRemove,
    diff,
)
from uxon.tui.dashboard.row import SessionRow


def _row(
    *,
    name: str,
    host: str | None = None,
    user: str = "alice",
    cpu_pct: float = 5.0,
    rss_kib: int = 100 * 1024,
    attached: bool = False,
    cmd: str = "claude",
    path: str = "/srv/foo",
) -> SessionRow:
    return SessionRow(
        host=host,
        user=user,
        name=name,
        short=name.split("uxon-")[-1] if name.startswith("uxon-") else name,
        agent="claude",
        attached=attached,
        legacy=False,
        pid=123,
        cpu_pct=cpu_pct,
        rss_kib=rss_kib,
        created_epoch=None,
        last_attached_epoch=None,
        cmd=cmd,
        path=path,
    )


def _cols(*ids: str) -> tuple:
    by_id = {c.id: c for c in REGISTRY}
    return tuple(by_id[i] for i in ids)


# A reasonable column tuple that exercises Text + str cell types.
_COLUMNS = _cols("name", "agent", "cpu", "ram", "cmd", "path")


class IdenticalModelsTests(unittest.TestCase):
    def test_identical_models_yield_zero_ops(self) -> None:
        t = (_row(name="uxon-a@claude"), _row(name="uxon-b@claude"))
        self.assertEqual(diff(t, t, _COLUMNS), ())

    def test_diff_is_pure(self) -> None:
        a = (_row(name="uxon-a@claude"),)
        b = (_row(name="uxon-a@claude", cpu_pct=99.0),)
        self.assertEqual(diff(a, b, _COLUMNS), diff(a, b, _COLUMNS))


class CellUpdateTests(unittest.TestCase):
    def test_cell_only_change_yields_one_cell_update(self) -> None:
        old = (_row(name="uxon-a@claude", cpu_pct=5.0),)
        new = (_row(name="uxon-a@claude", cpu_pct=99.0),)
        ops = diff(old, new, _COLUMNS)
        self.assertEqual(len(ops), 1)
        op = ops[0]
        self.assertIsInstance(op, CellUpdate)
        assert isinstance(op, CellUpdate)
        self.assertEqual(op.row_key, "local/alice/uxon-a@claude")
        self.assertEqual(op.col_id, "cpu")

    def test_change_in_one_cell_does_not_emit_for_unchanged_cells(self) -> None:
        # Multi-column row; only path changes.
        old = (_row(name="uxon-a@claude", path="/srv/old"),)
        new = (_row(name="uxon-a@claude", path="/srv/new"),)
        ops = diff(old, new, _COLUMNS)
        # Exactly one op, for the "path" column.
        self.assertEqual(len(ops), 1)
        assert isinstance(ops[0], CellUpdate)
        self.assertEqual(ops[0].col_id, "path")

    def test_rich_text_equality_used_for_cell_compare(self) -> None:
        # Same logical row → no CellUpdate. The "name" and "cpu"
        # columns return Text; if Text equality were broken we would
        # see spurious updates.
        a = _row(name="uxon-a@claude", cpu_pct=12.5, attached=True)
        b = _row(name="uxon-a@claude", cpu_pct=12.5, attached=True)
        self.assertEqual(diff((a,), (b,), _COLUMNS), ())
        # Sanity: Text equality holds for identical formatter output.
        self.assertEqual(Text("12.5", style="yellow"), Text("12.5", style="yellow"))


class AddRemoveTests(unittest.TestCase):
    def test_add_row_at_end(self) -> None:
        a = _row(name="uxon-a@claude")
        b = _row(name="uxon-b@claude")
        c = _row(name="uxon-c@claude")
        ops = diff((a, b), (a, b, c), _COLUMNS)
        self.assertEqual(len(ops), 1)
        op = ops[0]
        self.assertIsInstance(op, RowAdd)
        assert isinstance(op, RowAdd)
        self.assertEqual(op.row_key, "local/alice/uxon-c@claude")
        self.assertIsNone(op.before_key)

    def test_add_row_at_front(self) -> None:
        a = _row(name="uxon-a@claude")
        b = _row(name="uxon-b@claude")
        c = _row(name="uxon-c@claude")
        ops = diff((b, c), (a, b, c), _COLUMNS)
        self.assertEqual(len(ops), 1)
        op = ops[0]
        self.assertIsInstance(op, RowAdd)
        assert isinstance(op, RowAdd)
        self.assertEqual(op.row_key, "local/alice/uxon-a@claude")
        self.assertEqual(op.before_key, "local/alice/uxon-b@claude")

    def test_remove_row_at_middle(self) -> None:
        a = _row(name="uxon-a@claude")
        b = _row(name="uxon-b@claude")
        c = _row(name="uxon-c@claude")
        ops = diff((a, b, c), (a, c), _COLUMNS)
        self.assertEqual(len(ops), 1)
        op = ops[0]
        self.assertIsInstance(op, RowRemove)
        assert isinstance(op, RowRemove)
        self.assertEqual(op.row_key, "local/alice/uxon-b@claude")


class ReorderTests(unittest.TestCase):
    def test_swap_two_rows(self) -> None:
        a = _row(name="uxon-a@claude")
        b = _row(name="uxon-b@claude")
        ops = diff((a, b), (b, a), _COLUMNS)
        # Both rows' surviving-positions changed → both moved.
        # Algorithm walks NEW in order: b first, then a.
        # Removes-first pass emits nothing (no removed keys).
        # Then for b: RowRemove(b), RowAdd(b, before=a).
        # Then for a: RowRemove(a), RowAdd(a, before=None).
        self.assertEqual(len(ops), 4)
        self.assertEqual(ops[0], RowRemove("local/alice/uxon-b@claude"))
        op1 = ops[1]
        self.assertIsInstance(op1, RowAdd)
        assert isinstance(op1, RowAdd)
        self.assertEqual(op1.row_key, "local/alice/uxon-b@claude")
        self.assertEqual(op1.before_key, "local/alice/uxon-a@claude")
        self.assertEqual(ops[2], RowRemove("local/alice/uxon-a@claude"))
        op3 = ops[3]
        self.assertIsInstance(op3, RowAdd)
        assert isinstance(op3, RowAdd)
        self.assertEqual(op3.row_key, "local/alice/uxon-a@claude")
        self.assertIsNone(op3.before_key)


if __name__ == "__main__":
    unittest.main()

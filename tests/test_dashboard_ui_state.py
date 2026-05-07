"""Tests for :mod:`uxon.tui.dashboard.ui_state`.

Each reducer is pure and deterministic. The contract this file pins:

* Calling a reducer twice with the same input produces equal output.
* On a real change, a fresh instance is returned (``is`` identity
  differs).
* On a no-op the SAME object comes back (``is`` identity preserved),
  so identity-keyed memoisation downstream stays correct.
"""

from __future__ import annotations

import dataclasses
import unittest

from uxon.tui.dashboard.columns import REGISTRY, ColumnSpec
from uxon.tui.dashboard.ui_state import (
    DashboardUiState,
    cycle_sort,
    set_filter,
    toggle_sort_dir,
)


def _columns(*ids: str) -> tuple[ColumnSpec, ...]:
    """Build a column tuple from REGISTRY filtered to ``ids``."""
    by_id = {c.id: c for c in REGISTRY}
    return tuple(by_id[i] for i in ids)


class DashboardUiStateShapeTests(unittest.TestCase):
    def test_defaults(self) -> None:
        ui = DashboardUiState()
        self.assertEqual(ui.sort_by, "cpu")
        self.assertEqual(ui.sort_dir, "desc")
        self.assertEqual(ui.filter_text, "")

    def test_frozen(self) -> None:
        ui = DashboardUiState()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            ui.sort_by = "ram"  # type: ignore[misc]


class CycleSortTests(unittest.TestCase):
    def test_pure(self) -> None:
        ui = DashboardUiState(sort_by="cpu")
        cols = _columns("name", "cpu", "ram", "last")
        a = cycle_sort(ui, columns=cols)
        b = cycle_sort(ui, columns=cols)
        # Same input → equal output.
        self.assertEqual(a, b)

    def test_advances_to_next_in_cycle(self) -> None:
        cols = _columns("name", "cpu", "ram", "last")
        ui = DashboardUiState(sort_by="cpu")
        nxt = cycle_sort(ui, columns=cols)
        self.assertEqual(nxt.sort_by, "ram")
        self.assertEqual(nxt.sort_dir, ui.sort_dir)
        self.assertEqual(nxt.filter_text, ui.filter_text)
        # Real change → new instance.
        self.assertIsNot(nxt, ui)

    def test_wraps_around(self) -> None:
        cols = _columns("name", "cpu", "ram", "last")
        ui = DashboardUiState(sort_by="name")
        nxt = cycle_sort(ui, columns=cols)
        self.assertEqual(nxt.sort_by, "cpu")

    def test_skips_hidden_columns(self) -> None:
        # ``ram`` not visible → cycle skips from cpu directly to last.
        cols = _columns("name", "cpu", "last")
        ui = DashboardUiState(sort_by="cpu")
        nxt = cycle_sort(ui, columns=cols)
        self.assertEqual(nxt.sort_by, "last")

    def test_no_candidates_returns_same_object(self) -> None:
        # No cycle id is visible → no-op; SAME object back.
        cols = _columns("path", "cmd")  # neither is in _SORT_CYCLE
        ui = DashboardUiState(sort_by="cpu")
        nxt = cycle_sort(ui, columns=cols)
        self.assertIs(nxt, ui)

    def test_single_candidate_returns_same_object(self) -> None:
        # Only one cycle id is visible AND the current sort_by matches:
        # the only entry is already active → SAME object back.
        cols = _columns("path", "cpu")  # of cycle ids only "cpu" is here
        ui = DashboardUiState(sort_by="cpu")
        nxt = cycle_sort(ui, columns=cols)
        self.assertIs(nxt, ui)

    def test_sort_by_outside_cycle_jumps_to_first(self) -> None:
        # Operator pinned ``path`` (not in cycle); cycle should land
        # on the first available cycle entry under the active flags.
        cols = _columns("name", "cpu", "ram", "path")
        ui = DashboardUiState(sort_by="path")
        nxt = cycle_sort(ui, columns=cols)
        self.assertEqual(nxt.sort_by, "cpu")
        self.assertIsNot(nxt, ui)


class ToggleSortDirTests(unittest.TestCase):
    def test_pure(self) -> None:
        ui = DashboardUiState(sort_dir="desc")
        self.assertEqual(toggle_sort_dir(ui), toggle_sort_dir(ui))

    def test_desc_to_asc(self) -> None:
        ui = DashboardUiState(sort_dir="desc")
        nxt = toggle_sort_dir(ui)
        self.assertEqual(nxt.sort_dir, "asc")
        self.assertIsNot(nxt, ui)
        # Other fields untouched.
        self.assertEqual(nxt.sort_by, ui.sort_by)
        self.assertEqual(nxt.filter_text, ui.filter_text)

    def test_asc_to_desc(self) -> None:
        ui = DashboardUiState(sort_dir="asc")
        nxt = toggle_sort_dir(ui)
        self.assertEqual(nxt.sort_dir, "desc")

    def test_double_toggle_round_trips_value(self) -> None:
        # Pure: two toggles equal the original by value (identity differs
        # since toggle always changes the value, never a no-op).
        ui = DashboardUiState(sort_dir="desc")
        round_tripped = toggle_sort_dir(toggle_sort_dir(ui))
        self.assertEqual(round_tripped, ui)


class SetFilterTests(unittest.TestCase):
    def test_pure(self) -> None:
        ui = DashboardUiState()
        self.assertEqual(set_filter(ui, "abc"), set_filter(ui, "abc"))

    def test_empty_to_value(self) -> None:
        ui = DashboardUiState(filter_text="")
        nxt = set_filter(ui, "alice")
        self.assertEqual(nxt.filter_text, "alice")
        self.assertIsNot(nxt, ui)
        # Other fields untouched.
        self.assertEqual(nxt.sort_by, ui.sort_by)
        self.assertEqual(nxt.sort_dir, ui.sort_dir)

    def test_value_to_empty(self) -> None:
        ui = DashboardUiState(filter_text="alice")
        nxt = set_filter(ui, "")
        self.assertEqual(nxt.filter_text, "")
        self.assertIsNot(nxt, ui)

    def test_no_op_returns_same_object(self) -> None:
        ui = DashboardUiState(filter_text="alice")
        nxt = set_filter(ui, "alice")
        # Identity preserved: hot-path reducer must not break
        # downstream identity-keyed memoisation on no-ops.
        self.assertIs(nxt, ui)

    def test_no_op_empty_returns_same_object(self) -> None:
        ui = DashboardUiState()  # filter_text=""
        nxt = set_filter(ui, "")
        self.assertIs(nxt, ui)


if __name__ == "__main__":
    unittest.main()

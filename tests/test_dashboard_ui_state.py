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

from uxon.tui.dashboard.ui_state import (
    DashboardUiState,
    set_filter,
    set_view_mode,
)


class DashboardUiStateShapeTests(unittest.TestCase):
    def test_defaults(self) -> None:
        ui = DashboardUiState()
        self.assertEqual(ui.view_mode, "flat")
        self.assertEqual(ui.filter_text, "")

    def test_frozen(self) -> None:
        ui = DashboardUiState()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            ui.view_mode = "flat"  # type: ignore[misc]


def test_set_view_mode_returns_identity_on_noop():
    ui = DashboardUiState()
    assert set_view_mode(ui, "flat") is ui


def test_set_view_mode_flips():
    ui = DashboardUiState()
    out = set_view_mode(ui, "by_host")
    assert out.view_mode == "by_host" and out is not ui


def test_set_filter_identity_on_noop():
    ui = DashboardUiState(filter_text="kris")
    assert set_filter(ui, "kris") is ui


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
        self.assertEqual(nxt.view_mode, ui.view_mode)

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

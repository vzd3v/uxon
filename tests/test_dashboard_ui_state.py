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


class MainScreenUiStateSeenUsersTests(unittest.TestCase):
    """``seen_users`` is the monotonic accumulator that drives the USER
    column's cross_user-latch contract. Once two distinct users have
    been observed in this process, the bit stays set — auto-shrinking
    on filter / refresh would hide the column the operator was just
    using, which is the broken behaviour the latch exists to prevent.
    """

    def test_default_is_empty_set(self) -> None:
        from uxon.tui.dashboard.ui_state import MainScreenUiState

        ui = MainScreenUiState()
        self.assertEqual(ui.seen_users, set())

    def test_field_is_mutable_set(self) -> None:
        from uxon.tui.dashboard.ui_state import MainScreenUiState

        ui = MainScreenUiState()
        ui.seen_users.add("alice")
        ui.seen_users.add("bob")
        self.assertEqual(ui.seen_users, {"alice", "bob"})

    def test_independent_instances_do_not_share_set(self) -> None:
        # default_factory contract — two instances must get separate sets
        # so a test mutating one doesn't contaminate the next.
        from uxon.tui.dashboard.ui_state import MainScreenUiState

        a = MainScreenUiState()
        b = MainScreenUiState()
        a.seen_users.add("alice")
        self.assertEqual(b.seen_users, set())


if __name__ == "__main__":
    unittest.main()

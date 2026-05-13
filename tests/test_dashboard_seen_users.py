"""Tests for :mod:`uxon.tui.dashboard.seen_users`.

``collect_user_set`` walks the same three sources the dashboard model
folds together (``state.main.sessions``, ``state.main.other_sessions``,
``state.remote[*].snapshot.sessions``) and returns the deduped set of
non-empty usernames present in that snapshot.

``cross_user_latched`` reads the App-owned monotonic accumulator on
:class:`MainScreenUiState`. It is the single source of truth for the
``LayoutFlags.cross_user`` bit and for the cross_user position of the
layout signature — defined as «have two distinct users been observed
in this process».
"""

from __future__ import annotations

import unittest
from typing import Any

from uxon.remote_collector import RemoteSnapshot
from uxon.tui.context import ServerStatus, SudoCapability, TuiSession
from uxon.tui.dashboard.ui_state import MainScreenUiState
from uxon.tui.main_data import MainData
from uxon.tui.slot_state import SlotState
from uxon.tui.tui_state import TuiState


def _session(user: str = "alice", name: str = "uxon-foo@claude") -> TuiSession:
    return TuiSession(
        name=name,
        short=name.split("uxon-")[-1],
        attached=False,
        pid="1",
        cpu="0",
        ram="0",
        created="0s",
        last_activity="0s",
        cmd="claude",
        path="/srv",
        user=user,
        stem=name.split("@")[0].removeprefix("uxon-"),
        agent="claude",
        legacy=False,
    )


def _main_data(
    *,
    sessions: tuple[TuiSession, ...] = (),
    other_sessions: tuple[TuiSession, ...] = (),
) -> MainData:
    return MainData(
        sessions=sessions,
        other_sessions=other_sessions,
        server_status=ServerStatus(),
        sudo_caps=SudoCapability(),
        scope_skipped_users=(),
        cwd="/",
        cwd_short="/",
        new_project_root="/",
        existing_projects=(),
        total_cpu="",
        total_ram="",
        version="",
    )


def _wire_rec(user: str = "bob", name: str = "uxon-x@claude") -> dict[str, Any]:
    return {
        "user": user,
        "name": name,
        "short_id": name.split("uxon-")[-1],
        "agent": "claude",
        "attached": False,
        "windows": "1",
        "created": "",
        "last_attached": "",
        "pane_pids": [],
        "active_pid": None,
        "active_cmd": "claude",
        "active_path": "/",
        "cpu_pct": 0.0,
        "rss_kib": 0,
        "legacy": False,
    }


def _snapshot(host: str, sessions: list[dict[str, Any]]) -> RemoteSnapshot:
    return RemoteSnapshot(
        host_name=host,
        fetched_at_epoch=0.0,
        from_cache=False,
        error=None,
        sessions=sessions,
    )


class CollectUserSetTests(unittest.TestCase):
    """``collect_user_set`` is pure: walks the three model sources,
    dedupes, returns frozenset of non-empty usernames. No state
    mutation, no I/O."""

    def test_empty_state_returns_empty(self) -> None:
        from uxon.tui.dashboard.seen_users import collect_user_set

        self.assertEqual(collect_user_set(TuiState()), frozenset())

    def test_main_sessions_contribute(self) -> None:
        from uxon.tui.dashboard.seen_users import collect_user_set

        state = TuiState(main=_main_data(sessions=(_session(user="alice"),)))
        self.assertEqual(collect_user_set(state), frozenset({"alice"}))

    def test_main_other_sessions_contribute(self) -> None:
        from uxon.tui.dashboard.seen_users import collect_user_set

        state = TuiState(main=_main_data(other_sessions=(_session(user="bob"),)))
        self.assertEqual(collect_user_set(state), frozenset({"bob"}))

    def test_remote_snapshot_contributes(self) -> None:
        from uxon.tui.dashboard.seen_users import collect_user_set

        state = TuiState()
        state.remote["h1"] = SlotState(value=_snapshot("h1", [_wire_rec(user="carol")]))
        self.assertEqual(collect_user_set(state), frozenset({"carol"}))

    def test_all_three_sources_folded(self) -> None:
        from uxon.tui.dashboard.seen_users import collect_user_set

        state = TuiState(
            main=_main_data(
                sessions=(_session(user="alice"),),
                other_sessions=(_session(user="bob", name="uxon-b@claude"),),
            )
        )
        state.remote["h1"] = SlotState(value=_snapshot("h1", [_wire_rec(user="carol")]))
        self.assertEqual(collect_user_set(state), frozenset({"alice", "bob", "carol"}))

    def test_duplicates_deduped(self) -> None:
        from uxon.tui.dashboard.seen_users import collect_user_set

        state = TuiState(
            main=_main_data(
                sessions=(
                    _session(user="alice", name="uxon-a1@claude"),
                    _session(user="alice", name="uxon-a2@claude"),
                ),
            )
        )
        state.remote["h1"] = SlotState(value=_snapshot("h1", [_wire_rec(user="alice")]))
        self.assertEqual(collect_user_set(state), frozenset({"alice"}))

    def test_empty_user_skipped(self) -> None:
        # An empty username is not a meaningful "second user" signal —
        # treating it as one would auto-flip the latch on garbage data.
        from uxon.tui.dashboard.seen_users import collect_user_set

        state = TuiState(main=_main_data(sessions=(_session(user=""),)))
        self.assertEqual(collect_user_set(state), frozenset())

    def test_no_main_no_remote_returns_empty(self) -> None:
        from uxon.tui.dashboard.seen_users import collect_user_set

        # state.main = None — never-loaded sentinel; no crash.
        state = TuiState()
        self.assertEqual(collect_user_set(state), frozenset())

    def test_remote_slot_value_none_skipped(self) -> None:
        # A SlotState with value=None (host probed but not yet landed)
        # must not crash the walk.
        from uxon.tui.dashboard.seen_users import collect_user_set

        state = TuiState()
        state.remote["h1"] = SlotState(value=None)
        self.assertEqual(collect_user_set(state), frozenset())


class CrossUserLatchedTests(unittest.TestCase):
    """``cross_user_latched`` is the single bool the layout signature
    and ``MainScreen.__init__`` both read. Predicate: len(seen) > 1."""

    def test_empty_set_returns_false(self) -> None:
        from uxon.tui.dashboard.seen_users import cross_user_latched

        ui = MainScreenUiState()
        self.assertFalse(cross_user_latched(ui))

    def test_single_user_returns_false(self) -> None:
        from uxon.tui.dashboard.seen_users import cross_user_latched

        ui = MainScreenUiState()
        ui.seen_users.add("alice")
        self.assertFalse(cross_user_latched(ui))

    def test_two_users_returns_true(self) -> None:
        from uxon.tui.dashboard.seen_users import cross_user_latched

        ui = MainScreenUiState()
        ui.seen_users.update({"alice", "bob"})
        self.assertTrue(cross_user_latched(ui))

    def test_three_users_returns_true(self) -> None:
        from uxon.tui.dashboard.seen_users import cross_user_latched

        ui = MainScreenUiState()
        ui.seen_users.update({"alice", "bob", "carol"})
        self.assertTrue(cross_user_latched(ui))


class MonotonicityTests(unittest.TestCase):
    """The latch must NEVER shrink: feeding a state with fewer users
    keeps the previously-seen ones in the accumulator. This is the
    «don't auto-hide the column» contract.
    """

    def test_set_grows_only(self) -> None:
        from uxon.tui.dashboard.seen_users import collect_user_set, cross_user_latched

        ui = MainScreenUiState()
        # First tick: two users present.
        state1 = TuiState(
            main=_main_data(
                sessions=(_session(user="alice"),),
                other_sessions=(_session(user="bob", name="uxon-b@claude"),),
            )
        )
        ui.seen_users |= collect_user_set(state1)
        self.assertTrue(cross_user_latched(ui))

        # Second tick: only alice remains (bob's session died).
        state2 = TuiState(main=_main_data(sessions=(_session(user="alice"),)))
        ui.seen_users |= collect_user_set(state2)

        # Latch stays True — bob was observed and we don't forget him.
        self.assertTrue(cross_user_latched(ui))
        self.assertIn("bob", ui.seen_users)


if __name__ == "__main__":
    unittest.main()

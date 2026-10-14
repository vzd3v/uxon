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

from uxon.domain.sudo import SudoCapability
from uxon.domain.wire_schema import RemoteSnapshot
from uxon.tui.context import ServerStatus, TuiSession
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
        error=None,
        sessions=sessions,
    )


def test_collector_unions_sources_ignores_blank_and_cold_slots() -> None:
    from uxon.tui.dashboard.seen_users import collect_user_set

    own = (_session("alice"), _session("alice", "uxon-second@claude"), _session(""))
    foreign = (_session("bob"),)
    records = [_wire_rec("carol"), _wire_rec("alice"), _wire_rec("")]
    state = TuiState(main=_main_data(sessions=own, other_sessions=foreign))
    state.remote["live"] = SlotState(value=_snapshot("live", records))
    state.remote["cold"] = SlotState()
    assert collect_user_set(state) == frozenset({"alice", "bob", "carol"})
    assert state.main.sessions == own
    assert state.main.other_sessions == foreign
    assert [r["user"] for r in records] == ["carol", "alice", ""]
    assert collect_user_set(TuiState(remote={"cold": SlotState()})) == frozenset()


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

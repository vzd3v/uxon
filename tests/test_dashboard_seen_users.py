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


if __name__ == "__main__":
    unittest.main()

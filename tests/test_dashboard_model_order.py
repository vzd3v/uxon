"""Hard sort contract: locals → cfg-order remotes → within-block by recency."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from uxon.domain.wire_schema import RemoteSnapshot
from uxon.tui.dashboard.model import select_dashboard_model
from uxon.tui.dashboard.ui_state import DashboardUiState
from uxon.tui.slot_state import SlotState


def _epoch_to_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def _local_session(name, last_attached, user="me", cpu=0.0):
    """Local TuiSession-shaped namespace; ``last_attached`` is epoch seconds."""
    return SimpleNamespace(
        name=name,
        short=name,
        attached=False,
        pid="1",
        cpu=str(cpu),
        ram="0K",
        created="",
        last_activity="",
        cmd="claude",
        path="/srv/work",
        user=user,
        stem=name,
        agent="claude",
        legacy=False,
        created_iso="",
        last_attached_iso=_epoch_to_iso(last_attached) if last_attached else "",
        exited=False,
        has_diagnostics=False,
        active_exit_status=None,
    )


def _wire(name, *, last_attached, user="me", cpu_pct=0.0):
    """Wire-record dict with ``last_attached`` provided as epoch seconds."""
    return {
        "user": user,
        "name": name,
        "short_id": name,
        "agent": "claude",
        "attached": False,
        "windows": "1",
        "created": "",
        "last_attached": _epoch_to_iso(last_attached) if last_attached else "",
        "pane_pids": [],
        "active_pid": None,
        "active_cmd": "claude",
        "active_path": "/srv/work",
        "cpu_pct": cpu_pct,
        "rss_kib": 0,
        "legacy": False,
    }


def test_locals_first_then_cfg_order_remotes_then_recency_within_block() -> None:
    locals_ = [
        _local_session("missing", None, cpu=999),
        _local_session("zulu", 200),
        _local_session("old", 100),
        _local_session("alpha", 200),
    ]
    foreign = [
        _local_session("foreign-new", 300, user="bob"),
        _local_session("foreign-missing", None, user="bob", cpu=999),
    ]
    state = SimpleNamespace(
        main=SimpleNamespace(sessions=locals_, other_sessions=foreign),
        remote={
            "kris": SlotState(
                value=RemoteSnapshot(
                    host_name="kris",
                    fetched_at_epoch=1.0,
                    error=None,
                    sessions=[
                        _wire("k-missing", last_attached=None, cpu_pct=999),
                        _wire("k-old", last_attached=10),
                        _wire("k-zulu", last_attached=500),
                        _wire("k-alpha", last_attached=500),
                    ],
                )
            ),
            "ada": SlotState(
                value=RemoteSnapshot(
                    host_name="ada",
                    fetched_at_epoch=1.0,
                    error=None,
                    sessions=[_wire("a1", last_attached=50)],
                )
            ),
        },
    )
    cfg = SimpleNamespace(remote_hosts=[SimpleNamespace(name="ada"), SimpleNamespace(name="kris")])
    out = select_dashboard_model(state, cfg, DashboardUiState())
    assert [(r.host, r.user, r.name) for r in out] == [
        (None, "bob", "foreign-new"),
        (None, "me", "alpha"),
        (None, "me", "zulu"),
        (None, "me", "old"),
        (None, "bob", "foreign-missing"),
        (None, "me", "missing"),
        ("ada", "me", "a1"),
        ("kris", "me", "k-alpha"),
        ("kris", "me", "k-zulu"),
        ("kris", "me", "k-old"),
        ("kris", "me", "k-missing"),
    ]
    assert [s.name for s in locals_] == ["missing", "zulu", "old", "alpha"]
    assert [s.name for s in foreign] == ["foreign-new", "foreign-missing"]
    assert list(state.remote) == ["kris", "ada"]
    assert [r["name"] for r in state.remote["kris"].value.sessions] == [
        "k-missing",
        "k-old",
        "k-zulu",
        "k-alpha",
    ]

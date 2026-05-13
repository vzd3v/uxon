"""Hard sort contract: locals → cfg-order remotes → within-block by recency."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from uxon.tui.dashboard import model as model_module
from uxon.tui.dashboard.model import select_dashboard_model
from uxon.tui.dashboard.ui_state import DashboardUiState


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


def test_locals_first_then_cfg_order_remotes_then_recency_within_block():
    model_module._LAST_OUTPUT = ()
    state = SimpleNamespace(
        main=SimpleNamespace(
            sessions=[_local_session("alpha", 200), _local_session("bravo", 100)],
            other_sessions=[],
        ),
        remote={
            "kris": SimpleNamespace(
                value=SimpleNamespace(
                    sessions=[
                        _wire("k-old", last_attached=10),
                        _wire("k-new", last_attached=500),
                    ]
                )
            ),
            "ada": SimpleNamespace(
                value=SimpleNamespace(
                    sessions=[
                        _wire("a1", last_attached=50),
                    ]
                )
            ),
        },
    )
    cfg = SimpleNamespace(remote_hosts=[SimpleNamespace(name="ada"), SimpleNamespace(name="kris")])
    ui = DashboardUiState()
    out = select_dashboard_model(state, cfg, ui)
    assert [r.host for r in out] == [None, None, "ada", "kris", "kris"]
    # Within locals: more-recent first ("alpha" 200 > "bravo" 100).
    assert [r.name for r in out[:2]] == ["alpha", "bravo"]
    # Within kris: 500 > 10.
    assert [r.name for r in out[3:]] == ["k-new", "k-old"]

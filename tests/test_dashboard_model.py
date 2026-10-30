"""Model content, per-host updates, and configured search through the screen projection."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from uxon.domain.wire_schema import RemoteSnapshot
from uxon.tui.context import TuiSession
from uxon.tui.dashboard.model import select_dashboard_model
from uxon.tui.dashboard.ui_state import DashboardUiState
from uxon.tui.main_data import MainData
from uxon.tui.slot_state import SlotState
from uxon.tui.tui_state import TuiState


def _session(
    *,
    name: str = "uxon-foo@claude",
    short: str | None = None,
    user: str = "alice",
    cpu: str = "5.0",
    ram: str = "100M",
    cmd: str = "claude",
    path: str = "/srv/foo",
    attached: bool = False,
    pid: str = "123",
) -> TuiSession:
    return TuiSession(
        name=name,
        short=short or name.split("uxon-")[-1],
        attached=attached,
        pid=pid,
        cpu=cpu,
        ram=ram,
        created="1m",
        last_activity="0m",
        cmd=cmd,
        path=path,
        user=user,
        stem=name.split("@")[0].removeprefix("uxon-"),
        agent="claude",
        legacy=False,
    )


def _wire_rec(
    *,
    name: str = "uxon-x@claude",
    user: str = "alice",
    cpu_pct: float = 9.9,
    cmd: str = "claude",
    path: str = "/",
) -> dict[str, Any]:
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
        "active_cmd": cmd,
        "active_path": path,
        "cpu_pct": cpu_pct,
        "rss_kib": 1000,
        "legacy": False,
    }


def _snapshot(host: str, sessions: list[dict[str, Any]]) -> RemoteSnapshot:
    return RemoteSnapshot(
        host_name=host,
        fetched_at_epoch=0.0,
        error=None,
        sessions=sessions,
    )


def _cfg(*, current_user: str = "alice", host_names: tuple[str, ...] = ()) -> Any:
    """Configured peer order for pure model landings."""
    hosts = tuple(SimpleNamespace(name=n) for n in host_names)
    return SimpleNamespace(current_user=current_user, remote_hosts=hosts)


def test_peer_update_preserves_other_host_values_and_placed_order() -> None:
    from uxon.tui.dashboard.order import place

    cfg = _cfg(host_names=("h1", "h2"))
    state = TuiState()
    state.remote["h1"] = SlotState(
        value=_snapshot(
            "h1",
            [
                _wire_rec(name="uxon-b@claude", cpu_pct=2),
                _wire_rec(name="uxon-a@claude", cpu_pct=1),
                _wire_rec(name="uxon-depart@claude", cpu_pct=3),
            ],
        )
    )
    state.remote["h2"] = SlotState(
        value=_snapshot("h2", [_wire_rec(name="uxon-other@claude", user="bob", cpu_pct=7)])
    )
    before = select_dashboard_model(state, cfg, DashboardUiState())
    persisted = tuple(r.key for r in before)
    unaffected = tuple(r for r in before if r.host == "h2")
    state.refresh_tick += 1
    assert place(persisted, select_dashboard_model(state, cfg, DashboardUiState())) == before
    state.remote["h1"] = SlotState(
        value=_snapshot(
            "h1",
            [
                _wire_rec(name="uxon-b@claude", cpu_pct=99),
                _wire_rec(name="uxon-a@claude", cpu_pct=1),
                _wire_rec(name="uxon-c@claude", cpu_pct=4),
            ],
        )
    )
    placed = place(persisted, select_dashboard_model(state, cfg, DashboardUiState()))
    assert [(r.host, r.user, r.name, r.cpu_pct) for r in placed] == [
        ("h1", "alice", "uxon-a@claude", 1.0),
        ("h1", "alice", "uxon-b@claude", 99.0),
        ("h1", "alice", "uxon-c@claude", 4.0),
        ("h2", "bob", "uxon-other@claude", 7.0),
    ]
    assert tuple(r for r in placed if r.host == "h2") == unaffected
    assert [(r.name, r.cpu_pct) for r in before[:3]] == [
        ("uxon-a@claude", 1.0),
        ("uxon-b@claude", 2.0),
        ("uxon-depart@claude", 3.0),
    ]


def test_search_fields_direct_and_context_projection() -> None:
    from uxon.infra.remote_hosts import RemoteHost
    from uxon.tui.context import TuiContext
    from uxon.tui.screens.dashboard_render import DashboardRender

    ctx = TuiContext(
        sessions=[_session(name="uxon-foo@claude", cmd="tool-local", path="/srv/local-needle")],
        other_sessions=[
            _session(name="uxon-bar@claude", user="bob", cmd="tool-other", path="/srv/other-needle")
        ],
        total_cpu="",
        total_ram="",
        version="",
        cwd="/srv",
        cwd_short="srv",
        new_project_root="/srv",
        existing_projects=[],
        current_user="alice",
        remote_hosts=(
            RemoteHost(name="peer-a", ssh_alias="peer-a", description="", remote_uxon="uxon"),
        ),
    )
    state = TuiState(main=MainData.from_context(ctx))
    state.remote["peer-a"] = SlotState(
        value=_snapshot(
            "peer-a",
            [
                _wire_rec(
                    name="uxon-remote@claude",
                    user="carol",
                    cmd="tool-remote",
                    path="/srv/remote-needle",
                ),
            ],
        )
    )
    renderer = DashboardRender(SimpleNamespace(cfg=ctx))
    all_keys = [
        (None, "bob", "uxon-bar@claude"),
        (None, "alice", "uxon-foo@claude"),
        ("peer-a", "carol", "uxon-remote@claude"),
    ]
    for fields, needle, expected in (
        (("name", "user"), "", all_keys),
        (("name", "user"), "   ", all_keys),
        (("name", "user"), "FOO", all_keys[1:2]),
        (("name", "user"), "bob", all_keys[:1]),
        (("name", "user"), "carol", all_keys[2:]),
        (("name", "user"), "absent", []),
        (("path",), "local-needle", all_keys[1:2]),
        (("path",), "remote-needle", all_keys[2:]),
        (("cmd",), "tool-other", all_keys[:1]),
        (("cmd",), "tool-remote", all_keys[2:]),
        (("host",), "peer-a", all_keys[2:]),
        (("host",), "local", all_keys[:2]),
        (("name", "user"), "local-needle", []),
        (("name", "user"), "remote-needle", []),
        (("name", "user"), "tool-remote", []),
        (("name", "user"), "peer-a", []),
        (("path",), "bob", []),
    ):
        ctx.tui_search_fields = fields
        ui = DashboardUiState(filter_text=needle)
        for cfg in (ctx, renderer.build_dashboard_cfg_view()):
            rows = select_dashboard_model(state, cfg, ui)
            assert [(r.host, r.user, r.name) for r in rows] == expected, (fields, needle)


def test_no_main_yields_only_remote_rows() -> None:
    cfg = _cfg(host_names=("h1",))
    state = TuiState(
        remote={"h1": SlotState(value=_snapshot("h1", [_wire_rec(name="uxon-x@claude")]))}
    )
    rows = select_dashboard_model(state, cfg, DashboardUiState())
    assert [(r.host, r.user, r.name) for r in rows] == [("h1", "alice", "uxon-x@claude")]

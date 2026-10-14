"""Rebuild snapshots remain independent of producer collections."""

from __future__ import annotations

import dataclasses

from uxon.tui.context import (
    LaunchRequest,
    ServerStatus,
    SudoCapability,
    TuiContext,
    TuiSession,
)
from uxon.tui.main_data import MainData


def _mk_ctx(**overrides) -> TuiContext:
    base = dict(
        sessions=[
            TuiSession(
                name="s1",
                short="s1",
                attached=False,
                pid="42",
                cpu="0",
                ram="0",
                created="now",
                last_activity="now",
                cmd="-",
                path="/srv/work",
                user="dana_agent",
            )
        ],
        total_cpu="13",
        total_ram="800M",
        version="0.0.0-test",
        cwd="/srv/work",
        cwd_short="work",
        new_project_root="/srv/work",
        existing_projects=[("proj-a", "1m"), ("proj-b", "5m")],
        server_status=ServerStatus(load="0.5"),
        cwd_writable=True,
        current_user="dana_agent",
        sudo_caps=SudoCapability(reachable_users=frozenset({"alice"})),
        scope_skipped_users=("bob",),
        other_sessions=[],
        on_attach=lambda u, n: LaunchRequest(cmd=("/bin/true",), label="attach"),
    )
    base.update(overrides)
    return TuiContext(**base)


def test_nondefault_snapshot_survives_producer_collection_mutations() -> None:
    from uxon.infra.probes import HostStatsResult

    stats = HostStatsResult(
        cpu_pct=12.0,
        mem_used_kib=1000,
        mem_total_kib=4000,
        loadavg_1m=0.5,
        uptime_s=3600,
        kernel="test",
    )
    ctx = _mk_ctx(scope_skipped_users=["bob"], host_stats=stats)
    ctx.other_sessions.append(dataclasses.replace(ctx.sessions[0], user="bob", name="foreign"))
    snapshot = MainData.from_context(ctx)

    def projection(md):
        return (
            tuple((s.name, s.user) for s in md.sessions),
            tuple((s.name, s.user) for s in md.other_sessions),
            md.server_status,
            md.sudo_caps,
            md.scope_skipped_users,
            md.cwd,
            md.cwd_short,
            md.new_project_root,
            md.existing_projects,
            md.total_cpu,
            md.total_ram,
            md.version,
            md.host_stats,
        )

    expected = (
        (("s1", "dana_agent"),),
        (("foreign", "bob"),),
        ServerStatus(load="0.5"),
        SudoCapability(reachable_users=frozenset({"alice"})),
        ("bob",),
        "/srv/work",
        "work",
        "/srv/work",
        (("proj-a", "1m"), ("proj-b", "5m")),
        "13",
        "800M",
        "0.0.0-test",
        stats,
    )
    assert projection(snapshot) == expected
    ctx.sessions.clear()
    ctx.other_sessions.clear()
    ctx.existing_projects.clear()
    ctx.scope_skipped_users.clear()
    assert projection(snapshot) == expected

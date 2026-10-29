"""Block navigation and honest local, host, and fleet status projections."""

from dataclasses import replace
from types import SimpleNamespace

from uxon.tui.dashboard.buckets import (
    FleetSummary,
    HostStatusLine,
    compute_block_starts,
    select_fleet_summary,
    select_host_buckets,
    select_host_status_block,
)
from uxon.tui.widgets.host_status_bar import _render


def _row(host, name, attached=False, cpu=0.0, user="alice"):
    return SimpleNamespace(host=host, name=name, attached=attached, cpu_pct=cpu, user=user)


def _line(label, *, sessions=0, mem_used=0, mem_total=0, state="", host_name="h", failure=""):
    return HostStatusLine(
        host_name=host_name,
        label=label,
        session_count=sessions,
        attached_count=0,
        cpu_pct_sum=0.0,
        mem_used_kib=mem_used,
        mem_total_kib=mem_total,
        loadavg_1m=None,
        uptime_s=None,
        state=state,
        failure=failure,
    )


def test_block_starts_follow_owner_transitions_and_peer_identity() -> None:
    own = (_row(None, "a"), _row(None, "b"))
    foreign = (_row(None, "c", user="bob"), _row(None, "d", user="carol"))
    peers = (_row("p", "p1"), _row("p", "p2", user="bob"), _row("q", "q1"))
    for rows, expected in (
        ((), ()),
        (own, (0,)),
        (own + foreign + peers, (0, 2, 4, 6)),
        ((own[0], foreign[0], own[1], foreign[1]), (0, 1, 2, 3)),
    ):
        assert compute_block_starts(rows, "alice") == expected


def test_buckets_in_cfg_order_with_locals_first_and_empty_kept() -> None:
    rows = (_row("kris", "k1"), _row(None, "a"), _row("kris", "k2"))
    cfg = SimpleNamespace(remote_hosts=[SimpleNamespace(name="kris"), SimpleNamespace(name="ada")])
    buckets = select_host_buckets(rows, cfg)
    assert [(b.host_name, b.label, b.rows) for b in buckets] == [
        (None, "local", (rows[1],)),
        ("kris", "kris", (rows[0], rows[2])),
        ("ada", "ada", ()),
    ]


def test_mixed_host_status_zeroes_failed_snapshot_metrics() -> None:
    names = ("live", "pending", "snapshot-error", "last-error", "first-error")
    cfg = SimpleNamespace(remote_hosts=tuple(SimpleNamespace(name=n) for n in names))
    stats = dict(mem_used_kib=4000, mem_total_kib=8000, loadavg_1m=0.35, uptime_s=3600)
    healthy = SimpleNamespace(error=None, host_stats=stats)
    failed = SimpleNamespace(error="timeout", error_code="timeout", host_stats=stats)
    state = SimpleNamespace(
        remote={
            "live": SimpleNamespace(value=healthy, last_error=None),
            "snapshot-error": SimpleNamespace(value=failed),
            "last-error": SimpleNamespace(value=healthy, last_error="failed"),
            "first-error": SimpleNamespace(value=None, last_error="failed"),
        }
    )
    rows = (
        _row(None, "a", cpu=10),
        _row(None, "b", attached=True, cpu=20),
        _row("live", "l", attached=True, cpu=9),
        _row("snapshot-error", "stale", attached=True, cpu=90),
        _row("last-error", "stale", attached=True, cpu=90),
    )
    local_stats = SimpleNamespace(**stats)
    lines = select_host_status_block(rows, state, local_stats, cfg)
    assert lines == (
        HostStatusLine(None, "local", 2, 1, 30.0, 4000, 8000, 0.35, 3600, ""),
        HostStatusLine("live", "live", 1, 1, 9.0, 4000, 8000, 0.35, 3600, ""),
        HostStatusLine("pending", "pending", 0, 0, 0.0, 0, 0, None, None, "pending…"),
        HostStatusLine(
            "snapshot-error",
            "snapshot-error",
            0,
            0,
            0.0,
            0,
            0,
            None,
            None,
            "unreachable",
            "snapshot-error · timeout",
        ),
        HostStatusLine(
            "last-error",
            "last-error",
            0,
            0,
            0.0,
            0,
            0,
            None,
            None,
            "unreachable",
            "last-error · unreachable · failed",
        ),
        HostStatusLine(
            "first-error",
            "first-error",
            0,
            0,
            0.0,
            0,
            0,
            None,
            None,
            "pending…",
            "first-error · unreachable · failed",
        ),
    )


def test_status_text_has_exact_metrics_pending_and_failure_outputs() -> None:
    line = HostStatusLine(None, "local", 3, 1, 42.5, 8000000, 16000000, 0.42, 26 * 3600, "")
    assert _render(line) == "local · 3/1 sess · cpu 42% · mem 7.6/15G · up 1d2h"
    assert (
        _render(_line("warming", state="pending…"))
        == "warming · 0/0 sess · cpu 0% · mem — · up — · pending…"
    )
    assert _render(replace(line, failure="peer · timeout")) == "peer · timeout"


def test_fleet_counts_thresholds_and_alert_precedence() -> None:
    lines = (
        _line("local", sessions=2, host_name=None),
        _line("cool", sessions=3, mem_used=8900, mem_total=10000),
        _line("hot", mem_used=9000, mem_total=10000),
        _line("nodata", mem_used=5000),
        _line("warming", state="pending…"),
        _line("down", mem_used=9500, mem_total=10000, state="unreachable"),
        _line(
            "failed",
            mem_used=9500,
            mem_total=10000,
            state="unreachable",
            failure="failed · timeout",
        ),
    )
    assert select_fleet_summary(lines) == FleetSummary(7, 5, ("hot mem 90%", "down unreachable"))
    assert select_fleet_summary(()) == FleetSummary(0, 0, ())
    assert select_fleet_summary((lines[0], lines[4])) == FleetSummary(2, 2, ())

from __future__ import annotations

from types import SimpleNamespace

from uxon.tui.dashboard.buckets import (
    FleetSummary,
    HostStatusLine,
    compute_block_starts,
    select_fleet_summary,
    select_host_buckets,
    select_host_status_block,
)


def _row(host, name, attached=False, cpu=0.0, user="me"):
    return SimpleNamespace(host=host, name=name, attached=attached, cpu_pct=cpu, user=user)


def _line(label, *, sessions=0, mem_used=0, mem_total=0, state="", host_name="h"):
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
    )


# ── compute_block_starts ─────────────────────────────────────────────


def test_compute_block_starts_all_own_one_block():
    """Same (host, own/other) → exactly one block start (no false splits)."""
    rows = (_row(None, "a", user="me"), _row(None, "b", user="me"), _row(None, "c", user="me"))
    assert compute_block_starts(rows, "me") == (0,)


def test_compute_block_starts_own_then_other_user_split():
    """Own vs other-user inside the local host produces two blocks."""
    rows = (
        _row(None, "a", user="me"),
        _row(None, "b", user="me"),
        _row(None, "c", user="alice"),
    )
    assert compute_block_starts(rows, "me") == (0, 2)


def test_compute_block_starts_own_other_remote_three_blocks():
    """Full mix: own → other-user → per-remote-host. Remotes do NOT split on user."""
    rows = (
        _row(None, "a", user="me"),
        _row(None, "b", user="alice"),
        _row("kris", "k1", user="me"),
        _row("kris", "k2", user="alice"),
        _row("ada", "a1", user="me"),
    )
    assert compute_block_starts(rows, "me") == (0, 1, 2, 4)


def test_buckets_in_cfg_order_with_locals_first_and_empty_kept():
    rows = (_row(None, "a"), _row("kris", "k1"), _row("kris", "k2"))
    cfg = SimpleNamespace(remote_hosts=[SimpleNamespace(name="kris"), SimpleNamespace(name="ada")])
    buckets = select_host_buckets(rows, cfg)
    assert [b.host_name for b in buckets] == [None, "kris", "ada"]
    assert [len(b.rows) for b in buckets] == [1, 2, 0]


def test_status_block_aggregates_per_host():
    from uxon.probes import HostStatsResult

    rows = (_row(None, "a", cpu=10), _row(None, "b", cpu=20, attached=True))
    cfg = SimpleNamespace(remote_hosts=[])
    stats = HostStatsResult(
        cpu_pct=0.0,
        mem_used_kib=6_193_872,
        mem_total_kib=16_376_344,
        loadavg_1m=0.35,
        uptime_s=7_835_551,
        kernel="test",
    )
    state = SimpleNamespace(main=SimpleNamespace(host_stats=stats))
    lines = select_host_status_block(rows, state, host_stats_local=stats, cfg=cfg)
    local_line = lines[0]
    assert local_line.host_name is None
    assert local_line.session_count == 2
    assert local_line.attached_count == 1
    assert abs(local_line.cpu_pct_sum - 30.0) < 1e-6
    assert local_line.mem_used_kib == 6_193_872


def test_status_block_marks_pending_when_no_snapshot():
    rows = ()
    cfg = SimpleNamespace(remote_hosts=[SimpleNamespace(name="kris")])
    state = SimpleNamespace(remote={}, main=None)
    lines = select_host_status_block(rows, state, host_stats_local=None, cfg=cfg)
    kris = next(line for line in lines if line.host_name == "kris")
    assert kris.state == "pending…"


def test_status_block_marks_cached_when_snapshot_from_cache():
    rows = ()
    cfg = SimpleNamespace(remote_hosts=[SimpleNamespace(name="kris")])
    snap = SimpleNamespace(from_cache=True, host_stats=None)
    slot = SimpleNamespace(value=snap, consecutive_failures=0)
    state = SimpleNamespace(remote={"kris": slot}, main=None)
    lines = select_host_status_block(rows, state, host_stats_local=None, cfg=cfg)
    kris = next(line for line in lines if line.host_name == "kris")
    assert kris.state == "(cached)"


def test_status_block_marks_unreachable_after_consecutive_failures():
    rows = ()
    cfg = SimpleNamespace(remote_hosts=[SimpleNamespace(name="kris")])
    snap = SimpleNamespace(from_cache=False, host_stats=None)
    # Threshold mirrors BreakerSpec.trip_after default of 3.
    slot = SimpleNamespace(value=snap, consecutive_failures=3)
    state = SimpleNamespace(remote={"kris": slot}, main=None)
    lines = select_host_status_block(rows, state, host_stats_local=None, cfg=cfg)
    kris = next(line for line in lines if line.host_name == "kris")
    assert kris.state == "unreachable"


def test_host_status_bar_renders_a_line():
    from uxon.tui.widgets.host_status_bar import _render

    line = HostStatusLine(
        host_name=None,
        label="local",
        session_count=3,
        attached_count=1,
        cpu_pct_sum=42.5,
        mem_used_kib=8_000_000,
        mem_total_kib=16_000_000,
        loadavg_1m=0.42,
        uptime_s=3600 * 26,
        state="",
    )
    rendered = _render(line)
    # Compact contract: sessions fold into "N/M sess", CPU is a bare
    # percent, mem is "U/TG". `la` (load average) is no longer rendered
    # (FleetStatusBar redesign, 2026-05-30) — kept in the dataclass,
    # dropped from the line.
    assert "local" in rendered
    assert "3/1 sess" in rendered
    assert "cpu 42%" in rendered
    assert "mem 7.6/15G" in rendered
    assert "la" not in rendered


# ── select_fleet_summary ─────────────────────────────────────────────


def test_fleet_summary_counts_hosts_and_sessions():
    lines = (
        _line("local", sessions=2, host_name=None),
        _line("dev-wes", sessions=3),
        _line("gpu-box", sessions=0),
    )
    summary = select_fleet_summary(lines)
    assert isinstance(summary, FleetSummary)
    assert summary.host_count == 3
    assert summary.session_count == 5
    assert summary.alerts == ()


def test_fleet_summary_mem_pressure_alert_at_90pct():
    # 9.0/10.0 == 90% → alert; 8.9/10.0 → no alert.
    lines = (
        _line("hot", mem_used=9_000, mem_total=10_000),
        _line("cool", mem_used=8_900, mem_total=10_000),
    )
    summary = select_fleet_summary(lines)
    assert summary.alerts == ("hot mem 90%",)


def test_fleet_summary_missing_mem_never_alerts():
    # mem_total == 0 (cold start / old peer with no host_stats): excluded.
    lines = (_line("nodata", mem_used=0, mem_total=0),)
    assert select_fleet_summary(lines).alerts == ()


def test_fleet_summary_unreachable_alerts_but_pending_and_cached_do_not():
    lines = (
        _line("down", state="unreachable"),
        _line("warming", state="pending…"),
        _line("stale", state="(cached)"),
    )
    assert select_fleet_summary(lines).alerts == ("down unreachable",)


def test_fleet_summary_unreachable_subsumes_stale_mem_for_same_host():
    # A box that went unreachable while its last snapshot showed high mem
    # must emit ONE token (unreachable), not two — else one host fills the cap.
    lines = (_line("gpu", mem_used=9_500, mem_total=10_000, state="unreachable"),)
    assert select_fleet_summary(lines).alerts == ("gpu unreachable",)


def test_fleet_summary_cold_start_all_pending_is_quiet():
    lines = (
        _line("local", host_name=None),
        _line("a", state="pending…"),
        _line("b", state="pending…"),
    )
    assert select_fleet_summary(lines).alerts == ()

from __future__ import annotations

from types import SimpleNamespace

from uxon.tui.dashboard.buckets import (
    HostStatusLine,
    select_host_buckets,
    select_host_status_block,
)


def _row(host, name, attached=False, cpu=0.0, user="me"):
    return SimpleNamespace(host=host, name=name, attached=attached, cpu_pct=cpu, user=user)


def test_buckets_in_cfg_order_with_locals_first_and_empty_kept():
    rows = (_row(None, "a"), _row("kris", "k1"), _row("kris", "k2"))
    cfg = SimpleNamespace(remote_hosts=[SimpleNamespace(name="kris"), SimpleNamespace(name="ada")])
    buckets = select_host_buckets(rows, cfg, state=SimpleNamespace())
    assert [b.host_name for b in buckets] == [None, "kris", "ada"]
    assert [len(b.rows) for b in buckets] == [1, 2, 0]


def test_status_block_aggregates_per_host():
    rows = (_row(None, "a", cpu=10), _row(None, "b", cpu=20, attached=True))
    cfg = SimpleNamespace(remote_hosts=[])
    state = SimpleNamespace(main=SimpleNamespace(host_stats=None))
    lines = select_host_status_block(rows, state, host_stats_local=None, cfg=cfg)
    local_line = lines[0]
    assert local_line.host_name is None
    assert local_line.session_count == 2
    assert local_line.attached_count == 1
    assert abs(local_line.cpu_pct_sum - 30.0) < 1e-6


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
    assert "local" in rendered and "3 sess" in rendered and "1 attached" in rendered

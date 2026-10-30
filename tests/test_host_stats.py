"""Host metrics consume actual proc files and known sample deltas."""

from __future__ import annotations

import platform

from uxon.infra.probes import read_host_stats


def test_host_stats_exact_delta_missing_memory_zero_delta_and_clamps(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    proc.mkdir()
    stat = proc / "stat"
    stat.write_text("cpu 100 0 50 1000 0\n")
    (proc / "meminfo").write_text("MemTotal: 16384000 kB\nMemAvailable: 8000000 kB\n")
    (proc / "loadavg").write_text("0.42 0.50 0.55 1/123 4567\n")
    (proc / "uptime").write_text("12345.67 99999\n")
    monkeypatch.setattr("uxon.infra.probes._PROC", str(proc))
    samples = iter(
        [
            "cpu 150 0 50 1050 0\n",
            "cpu 150 0 50 1050 0\n",
            "cpu 250 0 50 1000 0\n",
            "cpu 200 0 50 1100 0\n",
        ]
    )
    sleeps = []

    def sample(delay):
        sleeps.append(delay)
        stat.write_text(next(samples))

    monkeypatch.setattr("uxon.infra.probes.time.sleep", sample)
    stats = read_host_stats()
    assert (
        stats.cpu_pct,
        stats.mem_total_kib,
        stats.mem_used_kib,
        stats.loadavg_1m,
        stats.uptime_s,
        stats.kernel,
    ) == (50, 16384000, 8384000, 0.42, 12345, platform.release())
    (proc / "meminfo").unlink()
    stats = read_host_stats()
    assert stats.cpu_pct == stats.mem_total_kib == stats.mem_used_kib == 0
    assert read_host_stats().cpu_pct == 100
    assert read_host_stats().cpu_pct == 0
    assert sleeps == [0.05] * 4

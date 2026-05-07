"""Verify ``read_host_stats`` against fixture ``/proc/*`` files."""
from __future__ import annotations

from uxon.probes import read_host_stats


def test_read_host_stats_returns_sane_ranges(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "stat").write_text(
        "cpu  100 0 50 1000 0 0 0 0 0 0\n"
        "cpu0 100 0 50 1000 0 0 0 0 0 0\n",
    )
    (proc / "meminfo").write_text(
        "MemTotal:       16384000 kB\n"
        "MemAvailable:    8000000 kB\n"
    )
    (proc / "loadavg").write_text("0.42 0.50 0.55 1/123 4567\n")
    (proc / "uptime").write_text("12345.67 99999.00\n")
    monkeypatch.setattr("uxon.probes._PROC", str(proc))
    monkeypatch.setattr("uxon.probes._CPU_DELAY_S", 0.0)  # Avoid the 50 ms sleep.

    stats = read_host_stats()
    assert 0.0 <= stats.cpu_pct <= 100.0
    assert stats.mem_total_kib == 16_384_000
    assert stats.mem_used_kib == 16_384_000 - 8_000_000
    assert abs(stats.loadavg_1m - 0.42) < 1e-6
    assert stats.uptime_s == 12_345
    assert stats.kernel  # non-empty


def test_read_host_stats_handles_missing_meminfo(tmp_path, monkeypatch):
    """Absence is reported as zero — never raises."""
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "stat").write_text("cpu  100 0 50 1000 0 0 0 0 0 0\n")
    (proc / "loadavg").write_text("0.0 0.0 0.0 1/1 1\n")
    (proc / "uptime").write_text("1 1\n")
    monkeypatch.setattr("uxon.probes._PROC", str(proc))
    monkeypatch.setattr("uxon.probes._CPU_DELAY_S", 0.0)
    stats = read_host_stats()
    assert stats.mem_total_kib == 0
    assert stats.mem_used_kib == 0

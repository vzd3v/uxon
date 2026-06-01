# SPDX-License-Identifier: MIT
"""Pure formatting helpers for timestamps, durations, and resource usage."""

from __future__ import annotations

from datetime import UTC, datetime


def fmt_epoch(ts: str) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), tz=UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def compact_time(iso_ts: str) -> str:
    if not iso_ts:
        return "-"
    try:
        dt = datetime.fromisoformat(iso_ts)
    except ValueError:
        return "-"
    now = datetime.now(tz=dt.tzinfo) if dt.tzinfo else datetime.now()
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    return dt.strftime("%m-%d")


def format_rss_kib(rss_kib: int) -> str:
    if rss_kib <= 0:
        return "-"
    if rss_kib < 1024:
        return f"{rss_kib}K"
    mib = rss_kib / 1024
    if mib < 1024:
        return f"{mib:.0f}M"
    gib = mib / 1024
    return f"{gib:.1f}G"


def format_cpu_pct(cpu_pct: float) -> str:
    if cpu_pct <= 0:
        return "-"
    if cpu_pct >= 100:
        return f"{cpu_pct:.0f}"
    return f"{cpu_pct:.1f}"


def _format_bytes(num_bytes: int) -> str:
    if num_bytes <= 0:
        return "-"
    value = float(num_bytes)
    for suffix in ("B", "K", "M", "G", "T"):
        if value < 1024 or suffix == "T":
            if suffix == "B":
                return f"{int(value)}B"
            if value >= 10:
                return f"{value:.0f}{suffix}"
            return f"{value:.1f}{suffix}"
        value /= 1024
    return f"{value:.0f}T"


def _pct(used: int, total: int) -> str:
    if total <= 0:
        return "-"
    return f"{(used / total) * 100:.0f}%"


def _compact_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"

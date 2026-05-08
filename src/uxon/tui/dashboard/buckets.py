"""Pure selectors for by-host views: buckets + status lines.

Two selectors layered on the unified row tuple from
:func:`select_dashboard_model`. Both consume the *result*, never the
selector input — the row tuple is the contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .row import SessionRow


@dataclass(frozen=True, slots=True)
class HostBucket:
    """One per configured host plus locals; preserved across empty hosts."""

    host_name: str | None  # None == locals
    label: str  # "local" or RemoteHost.name
    rows: tuple[SessionRow, ...]


@dataclass(frozen=True, slots=True)
class HostStatusLine:
    """Aggregated status of one bucket; rendered by HostStatusBar."""

    host_name: str | None
    label: str
    session_count: int
    attached_count: int
    cpu_pct_sum: float
    mem_used_kib: int
    mem_total_kib: int
    loadavg_1m: float | None
    uptime_s: int | None
    state: str  # "" | "(cached)" | "pending…" | "unreachable"


def select_host_buckets(
    rows: tuple[SessionRow, ...],
    cfg,
    state,
) -> tuple[HostBucket, ...]:
    grouped: dict[str | None, list] = {None: []}
    for host in cfg.remote_hosts:
        grouped[host.name] = []
    for row in rows:
        grouped.setdefault(row.host, []).append(row)
    out: list[HostBucket] = [HostBucket(None, "local", tuple(grouped.get(None, ())))]
    for host in cfg.remote_hosts:
        out.append(HostBucket(host.name, host.name, tuple(grouped.get(host.name, ()))))
    return tuple(out)


def _bucket_state(host_name: str | None, state) -> str:
    if host_name is None:
        return ""
    remote = getattr(state, "remote", {}) or {}
    slot = remote.get(host_name) if hasattr(remote, "get") else None
    if slot is None or getattr(slot, "value", None) is None:
        # No snapshot yet → pending.
        return "pending…"
    snap = slot.value
    if getattr(snap, "from_cache", False):
        return "(cached)"
    # The breaker is owned by the scheduler and never round-trips into
    # the slot, so we read ``consecutive_failures`` directly from
    # :class:`SlotState`. ``BreakerSpec.trip_after`` defaults to 3, so
    # we mirror that threshold here — keeping the two in sync is a
    # known coupling, called out in :class:`BreakerSpec`'s docstring.
    if getattr(slot, "consecutive_failures", 0) >= 3:
        return "unreachable"
    return ""


def _hs_field(stats: Any, key: str, default: Any = 0) -> Any:
    if stats is None:
        return default
    if isinstance(stats, dict):
        return stats.get(key, default)
    return getattr(stats, key, default)


def select_host_status_block(
    rows: tuple[SessionRow, ...],
    state,
    host_stats_local: Any,
    cfg,
) -> tuple[HostStatusLine, ...]:
    buckets = select_host_buckets(rows, cfg, state)
    out: list[HostStatusLine] = []
    for bucket in buckets:
        if bucket.host_name is None:
            stats = host_stats_local  # may be None on cold start
        else:
            remote = getattr(state, "remote", {}) or {}
            slot = remote.get(bucket.host_name) if hasattr(remote, "get") else None
            snap = getattr(slot, "value", None) if slot else None
            stats = getattr(snap, "host_stats", None) if snap else None
        cpu_sum = sum(getattr(r, "cpu_pct", 0.0) or 0.0 for r in bucket.rows)
        attached = sum(1 for r in bucket.rows if getattr(r, "attached", False))
        out.append(
            HostStatusLine(
                host_name=bucket.host_name,
                label=bucket.label,
                session_count=len(bucket.rows),
                attached_count=attached,
                cpu_pct_sum=cpu_sum,
                mem_used_kib=_hs_field(stats, "mem_used_kib", 0),
                mem_total_kib=_hs_field(stats, "mem_total_kib", 0),
                loadavg_1m=_hs_field(stats, "loadavg_1m", None),
                uptime_s=_hs_field(stats, "uptime_s", None),
                state=_bucket_state(bucket.host_name, state),
            )
        )
    return tuple(out)

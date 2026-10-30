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


def compute_block_starts(
    rows: tuple[SessionRow, ...],
    current_user: str,
) -> tuple[int, ...]:
    """Row indices where the (host, own/other) key changes.

    Used by ←/→ on the dashboard in flat view: each press jumps the
    cursor cyclically to the next/previous block start.

    Block key:

    * Local rows (``host is None``): ``(None, "own")`` for the current
      user, ``(None, "other")`` otherwise.
    * Remote rows: ``(host, "")`` — remotes do not split on user.

    Note that the dashboard model selector sorts the local block by
    recency, **not** by user. Own and other-user rows can therefore
    interleave, in which case this function legitimately emits more
    than one boundary inside the local host. The contract is "any
    transition is a jump point", not "exactly three sections".

    Pure function — kept here so unit tests don't need to construct
    a Textual screen. ``rows`` is the identity-stable tuple emitted
    by :func:`select_dashboard_model`; ``current_user`` is the string
    on the active :class:`uxon.tui.context.TuiContext`.
    """
    starts: list[int] = []
    prev_key: object = object()  # unique sentinel — first row always starts a block
    for i, row in enumerate(rows):
        if row.host is None:
            key: tuple[str | None, str] = (None, "own" if row.user == current_user else "other")
        else:
            key = (row.host, "")
        if key != prev_key:
            starts.append(i)
            prev_key = key
    return tuple(starts)


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
    loadavg_1m: float | None  # computed, intentionally unrendered (FleetStatusBar redesign)
    uptime_s: int | None
    state: str  # "" | "pending…" | "unreachable"
    failure: str = ""  # Literal, bounded diagnostic line; always shown below the table.


# Memory-pressure alert threshold: used/total at or above this fraction.
_MEM_PRESSURE = 0.90


@dataclass(frozen=True, slots=True)
class FleetSummary:
    """Fleet-level rollup for the collapsed FleetStatusBar.

    Only honest aggregates: counts (orientation / scale) and alerts.
    No cpu/mem sums across heterogeneous boxes — that would be garbage.
    """

    host_count: int
    session_count: int
    alerts: tuple[str, ...]  # bare tokens, e.g. "gpu-box mem 92%", "dev-nadia unreachable"


def select_fleet_summary(lines: tuple[HostStatusLine, ...]) -> FleetSummary:
    """Roll per-host status lines into counts + alert tokens.

    Alert rules (deliberately quiet — see the design doc):

    * memory pressure: a host with ``mem_total > 0`` and
      ``mem_used / mem_total >= 0.90``. Hosts with no mem data are
      excluded — never alert on missing data.
    * reachability: only ``unreachable``. ``pending…`` is benign and
      must NOT flip the bar to a warning state
      (the whole fleet is ``pending…`` on cold start).
    """
    alerts: list[str] = []
    for line in lines:
        if line.failure:
            continue
        if line.state == "unreachable":
            # Unreachable subsumes mem pressure for the same host: the
            # mem figure is stale (last snapshot before the host dropped)
            # and one host must not emit two tokens — that would exhaust
            # the collapsed-line cap and hide every other degraded host.
            alerts.append(f"{line.label} unreachable")
            continue
        if line.mem_total_kib > 0 and line.mem_used_kib / line.mem_total_kib >= _MEM_PRESSURE:
            pct = round(line.mem_used_kib / line.mem_total_kib * 100)
            alerts.append(f"{line.label} mem {pct}%")
    return FleetSummary(
        host_count=len(lines),
        session_count=sum(line.session_count for line in lines),
        alerts=tuple(alerts),
    )


def select_host_buckets(
    rows: tuple[SessionRow, ...],
    cfg,
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
    if getattr(slot, "last_error", None) or getattr(slot.value, "error", None):
        return "unreachable"
    return ""


def _display_token(value: str, limit: int) -> str:
    """Bound peer-controlled text and remove control characters."""
    return "".join(c for c in value[:limit] if c.isprintable())


def _failure_line(label: str, slot) -> str:
    snap = getattr(slot, "value", None)
    if snap is None and not getattr(slot, "last_error", None):
        return ""
    if not (getattr(slot, "last_error", None) or getattr(snap, "error", None)):
        return ""
    code = getattr(snap, "error_code", None)
    captions = {
        "update_remote": "update remote",
        "update_local": "update local",
        "incompatible": "incompatible",
        "timeout": "timeout",
        "ssh_error": "SSH failed",
        "invalid_response": "invalid response",
        "remote_error": "remote error",
        "fetch_error": "unreachable",
    }
    parts = [_display_token(label, 64)]
    peer = getattr(snap, "peer", {}) or {}
    version = _display_token(peer.get("uxon_version", ""), 32)
    if version:
        parts.append(f"uxon {version}")
    parts.append(captions.get(code or "", "unreachable"))
    detail = _display_token(getattr(snap, "error", None) or getattr(slot, "last_error", ""), 160)
    if detail and detail != parts[-1]:
        parts.append(detail)
    return " · ".join(parts)


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
    buckets = select_host_buckets(rows, cfg)
    out: list[HostStatusLine] = []
    for bucket in buckets:
        failure = ""
        if bucket.host_name is None:
            stats = host_stats_local  # may be None on cold start
        else:
            remote = getattr(state, "remote", {}) or {}
            slot = remote.get(bucket.host_name) if hasattr(remote, "get") else None
            snap = getattr(slot, "value", None) if slot else None
            failure = _failure_line(bucket.label, slot)
            stats = getattr(snap, "host_stats", None) if snap and not failure else None
        confirmed_rows = () if failure else bucket.rows
        cpu_sum = sum(getattr(r, "cpu_pct", 0.0) or 0.0 for r in confirmed_rows)
        attached = sum(1 for r in confirmed_rows if getattr(r, "attached", False))
        out.append(
            HostStatusLine(
                host_name=bucket.host_name,
                label=bucket.label,
                session_count=len(confirmed_rows),
                attached_count=attached,
                cpu_pct_sum=cpu_sum,
                mem_used_kib=_hs_field(stats, "mem_used_kib", 0),
                mem_total_kib=_hs_field(stats, "mem_total_kib", 0),
                loadavg_1m=_hs_field(stats, "loadavg_1m", None),
                uptime_s=_hs_field(stats, "uptime_s", None),
                state=_bucket_state(bucket.host_name, state),
                failure=failure,
            )
        )
    return tuple(out)

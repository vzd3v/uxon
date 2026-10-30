# SPDX-License-Identifier: MIT
"""Pure host-side workload-telemetry resolvers (no I/O).

The verified launch record identifies the workload cgroup. Its membership
and process-environment launch nonces are read inside the selected execution
boundary. Attribution always uses nonces, even when only one session is visible
on this controller. This module only groups and sums already-read values.
"""

from __future__ import annotations


def parse_cgroup_procs(content: str) -> list[int]:
    """Parse a ``cgroup.procs`` file body into the host PIDs it lists.

    The file is one decimal PID per line. Blank lines and any non-numeric
    line are skipped (defensive — the kernel only ever writes clean numeric
    lines, but a truncated read must never raise). Order is preserved and
    duplicates are kept out via first-seen dedupe so a caller can rely on a
    stable, unique set.
    """
    pids: list[int] = []
    seen: set[int] = set()
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pid = int(line)
        except ValueError:
            continue
        if pid in seen:
            continue
        seen.add(pid)
        pids.append(pid)
    return pids


def sum_usage_for_pids(
    pids: list[int],
    proc_rows: dict[int, tuple[int, int, float]],
) -> tuple[int, float]:
    """Sum RSS (KiB) and CPU% over ``pids`` from a ``ps`` table.

    ``proc_rows`` maps ``pid → (ppid, rss_kib, cpu_pct)`` — the single
    ``ps -eo pid=,ppid=,rss=,%cpu=`` table the probe already collects. A PID
    absent from the table (exited between the cgroup read and the ``ps``
    snapshot) contributes nothing. Negative values are clamped to zero,
    mirroring the OS-level pane-walk path. Returns ``(rss_kib, cpu_pct)``.

    ``cgroup.procs`` already lists the full workload process set including
    ``--init``-reparented descendants, so no child-walk is needed here — the
    cgroup is the authoritative membership boundary.
    """
    total_rss_kib = 0
    total_cpu_pct = 0.0
    for pid in pids:
        proc = proc_rows.get(pid)
        if proc is None:
            continue
        _, rss_kib, cpu_pct = proc
        total_rss_kib += max(rss_kib, 0)
        total_cpu_pct += max(cpu_pct, 0.0)
    return total_rss_kib, total_cpu_pct


def group_pids_by_session(
    cgroup_pids: list[int],
    pid_to_session: dict[int, str],
) -> dict[str, list[int]]:
    """Split a workload resource's host PIDs into per-session sets.

    ``cgroup_pids`` is the resource's full host-PID set (from
    ``cgroup.procs``); ``pid_to_session`` maps each readable PID to its
    launch nonce (from ``/proc/<pid>/environ``). Returns
    ``{nonce: [pids]}`` for every non-empty marker seen, so each session's
    sum reflects **only its own** process set — a runaway in session A never
    reddens session B.

    A PID with no marker (empty value, or absent from ``pid_to_session``
    because the process exited) is dropped: it belongs to no known
    session. The per-resource degrade (every sharing session shows the shared
    total) is the caller's fallback when the marker read fails wholesale — it
    is *not* expressed here, where a clean per-session split is the goal.
    """
    groups: dict[str, list[int]] = {}
    for pid in cgroup_pids:
        session = pid_to_session.get(pid, "")
        if not session:
            continue
        groups.setdefault(session, []).append(pid)
    return groups


def per_session_usage(
    cgroup_pids: list[int],
    pid_to_session: dict[int, str],
    proc_rows: dict[int, tuple[int, int, float]],
) -> dict[str, tuple[int, float]]:
    """Per-session ``(rss_kib, cpu_pct)`` for one resource's PID set.

    Composes :func:`group_pids_by_session` + :func:`sum_usage_for_pids`. The
    Each session, keyed by its launch nonce, gets the sum over only its own PIDs.
    """
    groups = group_pids_by_session(cgroup_pids, pid_to_session)
    return {session: sum_usage_for_pids(pids, proc_rows) for session, pids in groups.items()}

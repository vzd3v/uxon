# SPDX-License-Identifier: MIT
"""Pure host-side container-telemetry resolvers (no I/O).

Phase-2 observability attributes an in-container agent's CPU/RAM back to its
tmux session. The mechanism (locked by
``docs/decisions/2026-06-15-container-identity-resolution.md``):

1. Each enabled session carries the host-side cgroup path of its container in
   the ``UXON_CONTAINER_CGROUP`` session-env marker (stashed at launch).
2. ``/sys/fs/cgroup/<cgroup-path>/cgroup.procs`` lists **all** in-container
   host PIDs (pid1 plus children / ``--init``-reparented descendants).
3. For ≥2 sessions sharing one container, ``/proc/<hostpid>/environ`` carries
   the per-session ``UXON_SESSION`` marker that splits the shared PID set.

This module holds the **pure** logic only — the parsers and the grouping /
summation given already-read strings and tables. The impure ``/proc`` +
``cgroup.procs`` reads and the privileged ``environ`` shell-out live in
:mod:`uxon.infra.sessions_probe`. Keeping the core pure lets it be
host-free unit-tested against a fabricated tree, and keeps ``domain`` free of
subprocess per the layering rule.
"""

from __future__ import annotations

# Agent-process environment marker (also defined in :mod:`uxon.domain.container`
# as ``SESSION_ENV``; re-stated here as the parse key so this pure module needs
# no cross-import). Host-side telemetry reads it from ``/proc/<pid>/environ`` to
# attribute each in-container host PID to its tmux session.
_SESSION_ENV_KEY = "UXON_SESSION"


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


def parse_environ_session(environ_blob: str) -> str:
    """Extract ``UXON_SESSION``'s value from a ``/proc/<pid>/environ`` blob.

    ``/proc/<pid>/environ`` is NUL-separated ``KEY=value`` entries. Returns
    the session name, or ``""`` when the marker is absent (a non-uxon process
    sharing the container, or one launched before the marker existed). Never
    raises.
    """
    for entry in environ_blob.split("\0"):
        key, sep, value = entry.partition("=")
        if sep and key == _SESSION_ENV_KEY:
            return value
    return ""


def parse_sudo_environ_lines(stdout: str) -> dict[int, str]:
    """Parse the batched privileged-read output → ``{hostpid: session}``.

    The privileged helper emits one ``<hostpid> <UXON_SESSION value>`` line
    per readable PID (see :mod:`uxon.infra.sessions_probe`). A PID whose
    ``environ`` carried no marker is emitted with an empty value (``<pid> ``)
    or omitted entirely; either way it maps to ``""``. Malformed lines are
    skipped. Pure and defensive — never raises.
    """
    out: dict[int, str] = {}
    for line in stdout.splitlines():
        line = line.rstrip("\n")
        if not line.strip():
            continue
        pid_s, _, session = line.partition(" ")
        try:
            pid = int(pid_s.strip())
        except ValueError:
            continue
        out[pid] = session.strip()
    return out


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

    ``cgroup.procs`` already lists the full in-container process set including
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
    """Split a container's host PIDs into per-session sets (AC-P1.6).

    ``cgroup_pids`` is the container's full host-PID set (from
    ``cgroup.procs``); ``pid_to_session`` maps each readable PID to its
    ``UXON_SESSION`` marker (from ``/proc/<pid>/environ``). Returns
    ``{session: [pids]}`` for every non-empty marker seen, so each session's
    sum reflects **only its own** process set — a runaway in session A never
    reddens session B.

    A PID with no marker (empty value, or absent from ``pid_to_session``
    because its ``environ`` was unreadable) is dropped: it belongs to no known
    session. The per-container degrade (every sharing session shows the shared
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
    """Per-session ``(rss_kib, cpu_pct)`` for one container's PID set.

    Composes :func:`group_pids_by_session` + :func:`sum_usage_for_pids`. The
    pure core of the ≥2-sessions-per-container split: each session keyed by its
    ``UXON_SESSION`` marker gets the sum over only its own PIDs.
    """
    groups = group_pids_by_session(cgroup_pids, pid_to_session)
    return {session: sum_usage_for_pids(pids, proc_rows) for session, pids in groups.items()}

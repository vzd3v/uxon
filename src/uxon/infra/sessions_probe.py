# SPDX-License-Identifier: MIT
"""Session probes: read the live tmux session list and enrich it with
/proc usage.

Impure adapter: shells out to ``tmux`` / ``ps``. The local host's
server/SSH-link health readers live in
:mod:`uxon.infra.host_status_probe` so collection and host-status
reading stay single-responsibility.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Any

from uxon.domain.authz import canonical
from uxon.domain.config import Config
from uxon.domain.container import (
    CONTAINER_CGROUP_ENV,
    CONTAINER_NAME_ENV,
    ContainerConfig,
)
from uxon.domain.container_usage import (
    parse_cgroup_procs,
    parse_sudo_environ_lines,
    per_session_usage,
    sum_usage_for_pids,
)
from uxon.domain.format import fmt_epoch
from uxon.domain.session import (
    SessionInfo,
    compatible_indexed_sessions,
    parse_session_name,
    session_stem_for_path,
)
from uxon.errors import fail
from uxon.infra import demo, tmux
from uxon.infra.config_loader import normalize_user_list
from uxon.infra.container import CONTAINER_CMD_TIMEOUT_SEC
from uxon.infra.process import run_cmd


def enrich_session_usage(
    sessions: list[SessionInfo],
    *,
    container_cfg: ContainerConfig | None = None,
    launch_user: str = "",
) -> None:
    """Fill each session's ``cpu_pct`` / ``rss_kib`` from a single ``ps`` table.

    A **non-container** session (empty ``UXON_CONTAINER`` marker) takes the
    unchanged pane-PID child-walk — the byte-for-byte pre-container path, with
    **zero** added subprocess or ``/proc`` read (AC-P0.4). Every new
    branch below is gated on a non-empty ``session.container`` marker, so a
    deployment with no container sessions never reaches the container code.

    A **container** session is attributed from its container's cgroup
    membership (``container_cgroup`` → ``cgroup.procs``) rather than the pane
    walk (the pane's child is the runtime client, not the in-container agent).
    Per-distinct-container work is done once (AC-P1.5): the cgroup read, the
    optional privileged ``environ`` split, and — only when the cgroup is empty —
    the ``is_running_cmd`` liveness probe. Any unresolvable piece degrades that
    session to ``0``/``—`` (AC-P1.3), never raising.
    """
    if not sessions:
        return

    cp = subprocess.run(["ps", "-eo", "pid=,ppid=,rss=,%cpu="], text=True, capture_output=True)
    if cp.returncode != 0:
        return

    proc_rows: dict[int, tuple[int, int, float]] = {}
    children: dict[int, list[int]] = {}
    for row in cp.stdout.splitlines():
        parts = row.split(None, 3)
        if len(parts) != 4:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            rss_kib = int(parts[2])
            cpu_pct = float(parts[3])
        except ValueError:
            continue
        proc_rows[pid] = (ppid, rss_kib, cpu_pct)
        children.setdefault(ppid, []).append(pid)

    # Partition: marker-carrying sessions take the container path, the rest the
    # unchanged pane walk. The split itself reads only the already-populated
    # ``container`` field — no I/O — so the off-invariant holds (AC-P0.4).
    container_sessions = [s for s in sessions if s.container]
    for session in sessions:
        if session.container:
            continue
        _enrich_via_pane_walk(session, proc_rows, children)

    if container_sessions:
        _enrich_container_sessions(
            container_sessions, proc_rows, container_cfg=container_cfg, launch_user=launch_user
        )


def _enrich_via_pane_walk(
    session: SessionInfo,
    proc_rows: dict[int, tuple[int, int, float]],
    children: dict[int, list[int]],
) -> None:
    """The unchanged OS-level path: sum the pane-PID process subtree."""
    total_rss_kib = 0
    total_cpu_pct = 0.0
    seen: set[int] = set()
    stack = list(session.pane_pids)
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        proc = proc_rows.get(pid)
        if proc is None:
            continue
        _, rss_kib, cpu_pct = proc
        total_rss_kib += max(rss_kib, 0)
        total_cpu_pct += max(cpu_pct, 0.0)
        stack.extend(children.get(pid, []))
    session.rss_kib = total_rss_kib
    session.cpu_pct = total_cpu_pct


def _enrich_container_sessions(
    container_sessions: list[SessionInfo],
    proc_rows: dict[int, tuple[int, int, float]],
    *,
    container_cfg: ContainerConfig | None,
    launch_user: str,
) -> None:
    """Attribute in-container CPU/RAM for the marker-carrying sessions.

    Work is bounded by the number of **distinct cgroups** (AC-P1.5): each
    cgroup's ``cgroup.procs`` is read once; the per-session ``UXON_SESSION``
    split (a single privileged ``environ`` batch) runs only when ≥2 sessions
    share one cgroup; the ``is_running_cmd`` liveness probe runs only when a
    cgroup is empty/unresolvable.
    """
    # Group the marker-carrying sessions by their stashed cgroup path. A
    # session with an empty ``container_cgroup`` (identity unresolved at launch,
    # the degrade) gets no cgroup attribution — it falls to the down/zero path.
    by_cgroup: dict[str, list[SessionInfo]] = {}
    for session in container_sessions:
        # Default to 0/— first; the cgroup path overwrites on success.
        session.cpu_pct = 0.0
        session.rss_kib = 0
        if session.container_cgroup:
            by_cgroup.setdefault(session.container_cgroup, []).append(session)

    for cgroup_path, group in by_cgroup.items():
        cgroup_pids = _read_cgroup_procs(cgroup_path)
        if not cgroup_pids:
            # Empty/absent cgroup → the container is stopped or its path went
            # stale (restart). Confirm with the operator's liveness probe,
            # bounded once per distinct container, only here.
            _mark_container_down(group, container_cfg=container_cfg, launch_user=launch_user)
            continue
        if len(group) == 1:
            # One session per container: cgroup.procs IS that session's set —
            # no environ read needed (the common case, fully unprivileged).
            rss_kib, cpu_pct = sum_usage_for_pids(cgroup_pids, proc_rows)
            group[0].rss_kib = rss_kib
            group[0].cpu_pct = cpu_pct
            continue
        # ≥2 sessions share this cgroup → split by the per-process
        # ``UXON_SESSION`` marker (privileged environ read, batched once).
        pid_to_session = _read_pid_sessions(cgroup_pids)
        if pid_to_session is None:
            # No privilege / unreadable → degrade to the per-container SHARED
            # figure: every sharing session shows the summed total (AC-P1.6
            # degrade), never an error.
            shared_rss, shared_cpu = sum_usage_for_pids(cgroup_pids, proc_rows)
            for session in group:
                session.rss_kib = shared_rss
                session.cpu_pct = shared_cpu
            continue
        usage = per_session_usage(cgroup_pids, pid_to_session, proc_rows)
        for session in group:
            # A session with no marker-carrying PID (e.g. processes that
            # predate the UXON_SESSION marker) shows 0 here — the read
            # succeeded, so this is an honest per-session split, not the
            # wholesale-failure shared-total degrade above.
            rss_kib, cpu_pct = usage.get(session.name, (0, 0.0))
            session.rss_kib = rss_kib
            session.cpu_pct = cpu_pct


def _mark_container_down(
    group: list[SessionInfo],
    *,
    container_cfg: ContainerConfig | None,
    launch_user: str,
) -> None:
    """Flag a group's sessions container-down iff ``is_running_cmd`` confirms it.

    Called only when the cgroup is empty/unresolvable. The liveness probe runs
    once per distinct container (AC-P1.5). Without an ``is_running_cmd`` template
    (or with no container config threaded in) uxon cannot confirm down-state, so
    the sessions stay at the already-set ``0``/``—`` degrade rather than
    asserting a "down" it cannot verify.
    """
    if container_cfg is None or not container_cfg.is_running_cmd:
        return
    name = group[0].container
    if not _container_is_running(container_cfg, name, launch_user):
        for session in group:
            session.container_down = True


def _read_cgroup_procs(cgroup_path: str) -> list[int]:
    """Read ``/sys/fs/cgroup/<cgroup_path>/cgroup.procs`` → host PIDs (or []).

    ``cgroup_path`` is the kernel-reported path stashed at launch (leading-slash
    rooted at the cgroup v2 mount). World-readable, unprivileged. Any read
    failure (absent path = stopped container, permission, race) → ``[]``
    (degrade) — never raises.
    """
    rel = cgroup_path.lstrip("/")
    try:
        content = Path("/sys/fs/cgroup") / rel / "cgroup.procs"
        return parse_cgroup_procs(content.read_text())
    except OSError:
        return []


def _read_pid_sessions(pids: list[int]) -> dict[int, str] | None:
    """Batched privileged read of ``UXON_SESSION`` for ``pids`` → map, or None.

    ``/proc/<pid>/environ`` is not readable unprivileged under the distro
    default ``ptrace_scope=1`` (decision doc), so this issues a SINGLE
    non-interactive ``sudo -n`` shell-out per distinct container (NOT per
    session — AC-P1.5) that emits ``<pid> <UXON_SESSION value>`` lines, parsed
    host-side. Returns ``None`` on ANY failure (no sudo grant, non-zero,
    timeout, OSError) so the caller degrades to the per-container shared figure
    — never raises, never blocks the refresh.

    The helper reads each ``/proc/<pid>/environ`` and ``tr``-translates NULs to
    newlines, greps the marker, and prints ``<pid> <value>``; a PID whose
    environ is unreadable or carries no marker is simply omitted (maps to "").
    """
    if not pids:
        return {}
    pid_list = " ".join(str(p) for p in pids)
    # Pure POSIX sh: for each pid, pull UXON_SESSION out of its NUL-delimited
    # environ and print "<pid> <value>". Unreadable environ / absent marker →
    # no line for that pid. ``2>/dev/null`` keeps a single unreadable pid from
    # polluting the parsed output.
    script = (
        "for p in " + pid_list + "; do "
        'v=$(tr "\\0" "\\n" < /proc/$p/environ 2>/dev/null '
        "| sed -n 's/^UXON_SESSION=//p' | head -n1); "
        'if [ -n "$v" ]; then echo "$p $v"; fi; '
        "done"
    )
    try:
        cp = subprocess.run(
            ["sudo", "-n", "sh", "-c", script],
            text=True,
            capture_output=True,
            timeout=CONTAINER_CMD_TIMEOUT_SEC,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if cp.returncode != 0:
        return None
    return parse_sudo_environ_lines(cp.stdout)


def _container_is_running(container_cfg: ContainerConfig, name: str, launch_user: str) -> bool:
    """Run the operator's ``is_running_cmd`` for ``name`` → True iff it exits 0.

    Reuses the bounded as-user probe in :mod:`uxon.infra.container` (same
    rootless-daemon identity the agent execs under). Any failure to even run
    the probe is treated as "cannot confirm running" → returns the running
    interpretation conservatively False only when the probe cleanly says so;
    a render/exec error degrades to ``True`` (don't assert down on a probe we
    couldn't run). Bounded once per distinct container by the caller.
    """
    from uxon.infra.container import probe_container_running

    return probe_container_running(container_cfg, name, launch_user)


def collect_sessions_for_user(
    user: str,
    session_prefix: str,
    socket_path: str | None,
    *,
    legacy_prefixes: tuple[str, ...] = (),
    container_cfg: ContainerConfig | None = None,
) -> list[SessionInfo]:
    # Demo-mode short-circuit: when ``UXON_DEMO_HOSTS`` is set, bypass
    # tmux entirely and read the synthetic-local envelope. Returning
    # ``[]`` for an absent envelope mirrors production's "empty tmux
    # socket" path, so screenshots on a multi-tenant box don't leak the
    # caller's real sessions. The legacy_prefixes argument is irrelevant
    # in demo mode — scenario envelopes already carry fully-qualified
    # session names.
    _demo_dir = demo.demo_hosts_dir()
    if _demo_dir is not None:
        return demo.load_demo_local_sessions(_demo_dir, user)

    # Listing runs without a TTY (CLI ``list``, TUI background poll,
    # remote aggregator). Use the non-interactive sudo prefix so a
    # missing NOPASSWD grant returns non-zero immediately rather than
    # blocking on a hidden password prompt.
    base = tmux.tmux_base(user, socket_path, nonint=True)
    probe = subprocess.run(base + ["list-sessions"], text=True, capture_output=True)
    if probe.returncode != 0:
        return []

    # The trailing two ``#{E:...}`` fields read the container telemetry markers
    # straight from each session's environment in the SAME batch (tmux expands
    # ``#{E:VAR}`` to "" for an unset var — a non-container session). This is
    # the AC-P0.4/P1.5 zero-cost path: no per-session ``show-environment``, no
    # per-session subprocess.
    fmt = (
        "#{session_name}\t#{session_attached}\t#{session_windows}"
        "\t#{session_created}\t#{session_activity}"
        "\t#{E:" + CONTAINER_NAME_ENV + "}\t#{E:" + CONTAINER_CGROUP_ENV + "}"
    )
    rows = run_cmd(base + ["list-sessions", "-F", fmt]).stdout.splitlines()
    sessions: list[SessionInfo] = []
    known_prefixes = (session_prefix, *legacy_prefixes)
    for row in rows:
        parts = row.split("\t")
        if len(parts) != 7:
            continue
        name, attached, windows, created_ts, activity_ts, container, container_cgroup = parts
        if not any(name.startswith(p) for p in known_prefixes):
            continue

        pane_fmt = "#{pane_active}\t#{pane_pid}\t#{pane_current_command}\t#{pane_current_path}"
        pane_rows = run_cmd(
            base + ["list-panes", "-t", name, "-F", pane_fmt], check=False
        ).stdout.splitlines()
        pane_pids: list[int] = []
        active_pid: int | None = None
        active_cmd = ""
        active_path = ""
        for prow in pane_rows:
            pparts = prow.split("\t")
            if len(pparts) != 4:
                continue
            is_active, pid_s, cmd, path = pparts
            try:
                pane_pid = int(pid_s)
            except ValueError:
                pane_pid = None
            if pane_pid is not None:
                pane_pids.append(pane_pid)
            if is_active != "1":
                continue
            active_pid = pane_pid
            active_cmd = cmd
            active_path = path

        _parsed = parse_session_name(name, prefix=session_prefix, legacy_prefixes=legacy_prefixes)
        if _parsed is None:
            continue  # dual-prefix filter matched but parser disagreed — skip
        _, _agent, _, _legacy = _parsed
        # Preserve whatever the session-name parser extracted — the catalog
        # is config-driven, so a custom agent id (e.g. ``aider``) must reach
        # ``SessionInfo.agent`` intact, not be collapsed to ``"unknown"``.
        sessions.append(
            SessionInfo(
                user=user,
                name=name,
                attached=attached,
                windows=windows,
                created=fmt_epoch(created_ts),
                last_attached=fmt_epoch(activity_ts),
                pane_pids=tuple(pane_pids),
                active_pid=active_pid,
                active_cmd=active_cmd,
                active_path=active_path,
                agent=_agent,
                legacy=_legacy,
                container=container,
                container_cgroup=container_cgroup,
            )
        )
    enrich_session_usage(sessions, container_cfg=container_cfg, launch_user=user)
    return sessions


def collect_sessions(users: list[str], cfg: Config) -> list[SessionInfo]:
    sessions: list[SessionInfo] = []
    for user in normalize_user_list(users):
        sessions.extend(
            collect_sessions_for_user(
                user,
                cfg.session_prefix,
                tmux.tmux_socket_path(cfg, user),
                legacy_prefixes=cfg.legacy_session_prefixes,
                container_cfg=cfg.container if cfg.container.enabled else None,
            )
        )
    return sessions


def resolve_session(
    identifier: str,
    sessions: list[SessionInfo],
    prefix: str,
    *,
    legacy_prefixes: tuple[str, ...] = (),
) -> SessionInfo:
    if not sessions:
        fail(f"no {prefix}* sessions found")

    known_prefixes = (prefix, *legacy_prefixes)

    # 1) exact name
    exact = [s for s in sessions if s.name == identifier]
    if len(exact) == 1:
        return exact[0]

    # 2) normalized with current or any legacy prefix
    candidates: list[SessionInfo] = []
    for candidate_prefix in known_prefixes:
        normalized = (
            identifier
            if identifier.startswith(candidate_prefix)
            else f"{candidate_prefix}{identifier}"
        )
        candidates.extend(s for s in sessions if s.name == normalized)
    uniq: dict[str, SessionInfo] = {s.name: s for s in candidates}
    if len(uniq) == 1:
        return next(iter(uniq.values()))
    if len(uniq) > 1:
        fail(f"ambiguous identifier '{identifier}': {', '.join(sorted(uniq))}")

    # 3) stem match across all agents (both legacy and new)
    stem_hits: list[SessionInfo] = []
    for s in sessions:
        parsed = parse_session_name(s.name, prefix=prefix, legacy_prefixes=legacy_prefixes)
        if parsed is None:
            continue
        p_stem, _agent, _idx, _legacy = parsed
        if p_stem == identifier:
            stem_hits.append(s)
    if len(stem_hits) == 1:
        return stem_hits[0]
    if len(stem_hits) > 1:
        fail(
            f"ambiguous stem '{identifier}' matches multiple agents: "
            + ", ".join(sorted(s.name for s in stem_hits))
        )

    # 4) unique prefix match (as before, all known prefix variants)
    pref: list[SessionInfo] = []
    for s in sessions:
        short = s.name
        for p in known_prefixes:
            if short.startswith(p):
                short = short[len(p) :]
                break
        if s.name.startswith(identifier) or short.startswith(identifier):
            pref.append(s)
    uniq2: dict[str, SessionInfo] = {s.name: s for s in pref}
    if len(uniq2) == 1:
        return next(iter(uniq2.values()))
    if len(uniq2) > 1:
        fail(f"ambiguous identifier '{identifier}': {', '.join(sorted(uniq2))}")

    # 5) active pane pid
    if identifier.isdigit():
        pid = int(identifier)
        pid_hits = [s for s in sessions if s.active_pid == pid]
        if len(pid_hits) == 1:
            return pid_hits[0]
        if len(pid_hits) > 1:
            fail(
                f"pid '{identifier}' matches multiple sessions: {', '.join(s.name for s in pid_hits)}"
            )

    fail(f"no session match for '{identifier}'")
    raise AssertionError("unreachable")


def _resolve_or_audit_not_found(
    identifier: str,
    sessions: list[SessionInfo],
    cfg: Config,
    *,
    audit_event: str | None,
    target_user: str,
    session_field: str = "session",
    extra: dict[str, Any] | None = None,
) -> SessionInfo:
    """Resolve a session and, on no-match failure, emit the ``not_found``
    audit outcome before re-raising.

    The audit contract enumerates
    ``outcome ∈ {"ok", "denied", "error", "not_found"}`` for
    ``session.attach``, ``session.kill``, and their peer-inbound
    replacements ``attach.remote.in`` / ``kill.remote.in``. Without
    this wrapper the ``not_found`` outcome would never appear, because
    :func:`resolve_session` raises :class:`SystemExit` via :func:`fail`
    before any caller-side audit fires.

    ``session_field`` selects the key under which the identifier is
    recorded: ``"session"`` for ``session.attach`` / ``session.kill``,
    ``"target_session"`` for ``attach.remote.in`` / ``kill.remote.in``
    (peer-inbound branches — the spec uses different field names on
    the two sides of the wire).

    Pass ``audit_event=None`` to skip the emit entirely.
    """
    try:
        return resolve_session(
            identifier,
            sessions,
            cfg.session_prefix,
            legacy_prefixes=cfg.legacy_session_prefixes,
        )
    except SystemExit:
        if audit_event is None:
            raise
        from uxon.infra import audit as _audit

        fields: dict[str, Any] = {
            session_field: identifier or "",
            "target_user": target_user,
        }
        if extra:
            fields.update(extra)
        _audit.audit(audit_event, outcome="not_found", **fields)
        raise


def legacy_compatible_sessions(
    cfg: Config, launch_user: str, stem: str, compatibility_root: str
) -> list[SessionInfo]:
    sessions = collect_sessions_for_user(
        launch_user,
        cfg.session_prefix,
        socket_path=None,
        legacy_prefixes=cfg.legacy_session_prefixes,
    )
    return compatible_indexed_sessions(
        stem,
        cfg.default_agent,
        compatibility_root,
        sessions,
        prefix=cfg.session_prefix,
        legacy_prefixes=cfg.legacy_session_prefixes,
    )


def legacy_socket_conflict_hint(cfg: Config, launch_user: str, existing: list[SessionInfo]) -> str:
    attach_cmd = shlex.join(
        tmux.tmux_base(launch_user) + ["attach-session", "-t", existing[0].name]
    )
    session_names = ", ".join(session.name for session in existing)
    return (
        f"compatible session(s) exist on the legacy default tmux socket: {session_names}. "
        f"Current uxon config uses dedicated socket {tmux.tmux_socket_path(cfg, launch_user)}. "
        f"Run 'uxon doctor' for details, attach manually with '{attach_cmd}', or clear/migrate the legacy session first."
    )


def repeat_guardrail_for_legacy_socket(
    cfg: Config,
    launch_user: str,
    stem: str,
    compatibility_root: str,
) -> None:
    legacy = legacy_compatible_sessions(cfg, launch_user, stem, compatibility_root)
    if legacy:
        fail(legacy_socket_conflict_hint(cfg, launch_user, legacy))


def probe_tui_compatible_sessions(
    cfg: Config,
    launch_user: str,
    target_dir: str,
    agent_id: str,
    *,
    stem: str | None = None,
    compatibility_root: str | None = None,
) -> tuple[SessionInfo, ...]:
    """Return launch_user's sessions compatible with ``target_dir`` + ``agent_id``.

    Pure side-effect-free read of the live tmux session list (modulo the
    path-safety ``fail()`` inherited from :func:`compatible_indexed_sessions`,
    which is the same invariant the planner enforces — surfacing here too
    keeps the TUI honest). Returns an empty tuple when no compatible
    session exists. Used by the TUI to decide whether to push the
    SessionChoiceScreen modal before a launch action commits.

    ``stem`` and ``compatibility_root`` default to the basename-derived
    stem and the target dir (the unchanged primary/non-worktree path). For
    a worktree target the caller passes the repo-qualified
    :func:`session_stem_for_worktree` and the worktree path so the probe
    derives the *same* stem the planner used (§2.5) — generalising here
    rather than always deriving from the basename is the fix that keeps
    the attach guard reliable across repos.
    """
    target_canonical = canonical(target_dir)
    session_stem = stem if stem is not None else session_stem_for_path(target_canonical)
    root = canonical(compatibility_root) if compatibility_root is not None else target_canonical
    sessions = collect_sessions([launch_user], cfg)
    matches = compatible_indexed_sessions(
        session_stem,
        agent_id,
        root,
        sessions,
        prefix=cfg.session_prefix,
        legacy_prefixes=cfg.legacy_session_prefixes,
    )
    return tuple(matches)

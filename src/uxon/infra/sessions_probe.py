# SPDX-License-Identifier: MIT
"""Session probes: read the live tmux session list and enrich it with
/proc usage.

Impure adapter: shells out to ``tmux`` / ``ps``. The local host's
server/SSH-link health readers live in
:mod:`uxon.infra.host_status_probe` so collection and host-status
reading stay single-responsibility.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from uxon.domain.config import Config
from uxon.domain.format import fmt_epoch
from uxon.domain.runtime import (
    RUNTIME_CGROUP_ENV,
    RUNTIME_RESOURCE_ENV,
    WorkloadRuntimeSpec,
)
from uxon.domain.runtime_usage import per_session_usage, sum_usage_for_pids
from uxon.domain.session import (
    SessionInfo,
    compatible_indexed_sessions,
    parse_session_name,
    session_stem_for_path,
)
from uxon.errors import fail
from uxon.infra import demo, tmux
from uxon.infra.config_loader import normalize_user_list
from uxon.infra.launch_records import (
    LAUNCH_NONCE_ENV,
    TmuxSessionMetadata,
    garbage_collect_records,
    read_verified_record,
)
from uxon.infra.process import run_cmd
from uxon.infra.run import run_query


@dataclass(frozen=True, slots=True)
class UserSessionSnapshot:
    """One target user's sessions plus the independently observed server state."""

    user: str
    server_state: Literal["absent", "running"]
    sessions: tuple[SessionInfo, ...]


def _gc_launch_records(
    cfg: Config,
    launch_user: str,
    socket_path: str,
    live: set[tuple[str, str, str]],
) -> None:
    garbage_collect_records(
        live,
        override_dir=Path(cfg.launch_record_dir) if cfg.launch_record_dir else None,
        shared=bool(cfg.launch_record_dir),
        launch_user=launch_user,
    )


def enrich_session_usage(
    cfg: Config,
    sessions: list[SessionInfo],
    *,
    runtimes: dict[str, WorkloadRuntimeSpec] | None = None,
    launch_user: str = "",
) -> None:
    """Fill each session's ``cpu_pct`` / ``rss_kib`` from a single ``ps`` table.

    A direct-runtime session takes the unchanged pane-PID child-walk, with
    **zero** added subprocess or ``/proc`` read. Every new
    branch below is gated on a non-empty ``session.runtime_resource`` marker, so a
    deployment with no command-runtime sessions never reaches workload telemetry.

    A command-runtime session is attributed from its resource's cgroup
    membership (``runtime_cgroup`` → ``cgroup.procs``) rather than the pane
    walk (the pane's child is the runtime client, not the workload agent).
    Per-distinct-resource work is done once: the cgroup read, the
    optional privileged ``environ`` split, and — only when the cgroup is empty —
    the ``ready_command`` liveness probe. Any unresolvable piece degrades that
    session to ``0``/``—``, never raising.
    """
    if not sessions:
        return

    from uxon.infra.execution import wrap_command

    ps_cmd = ["ps", "-eo", "pid=,ppid=,rss=,%cpu="]
    cp = run_query(
        wrap_command(cfg, launch_user, ps_cmd, interactive=False) if launch_user else ps_cmd
    )
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

    # Partition: marker-carrying sessions take the runtime path, the rest the
    # unchanged pane walk. The split itself reads only the already-populated
    # ``runtime_resource`` field — no I/O — so the off-invariant holds.
    runtime_sessions = [s for s in sessions if s.runtime_resource]
    for session in sessions:
        if session.runtime_resource:
            continue
        _enrich_via_pane_walk(session, proc_rows, children)

    if runtime_sessions:
        _enrich_runtime_sessions(
            cfg,
            runtime_sessions,
            proc_rows,
            runtimes=runtimes,
            launch_user=launch_user,
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


def _enrich_runtime_sessions(
    cfg: Config,
    runtime_sessions: list[SessionInfo],
    proc_rows: dict[int, tuple[int, int, float]],
    *,
    runtimes: dict[str, WorkloadRuntimeSpec] | None,
    launch_user: str,
) -> None:
    """Attribute workload CPU/RAM for the marker-carrying sessions.

    Work is bounded by the number of **distinct cgroups**: each
    cgroup's ``cgroup.procs`` is read once; the per-session ``UXON_SESSION``
    split (a single privileged ``environ`` batch) runs only when ≥2 sessions
    share one cgroup; the ``ready_command`` liveness probe runs only when a
    cgroup is empty/unresolvable.
    """
    # Group the marker-carrying sessions by their stashed cgroup path. A
    # session with an empty ``runtime_cgroup`` (identity unresolved at launch,
    # the degrade) gets no cgroup attribution — it falls to the down/zero path.
    by_cgroup: dict[str, list[SessionInfo]] = {}
    down_probe_groups: dict[tuple[str, str], list[SessionInfo]] = {}
    for session in runtime_sessions:
        # Default to 0/— first; the cgroup path overwrites on success.
        session.cpu_pct = 0.0
        session.rss_kib = 0
        identity_state = _runtime_identity_state(
            cfg, session, runtimes=runtimes, launch_user=launch_user
        )
        if identity_state == "current" and session.runtime_cgroup:
            by_cgroup.setdefault(session.runtime_cgroup, []).append(session)
        elif identity_state == "unresolved":
            _append_down_probe_group(down_probe_groups, session)

    for cgroup_path, group in by_cgroup.items():
        cgroup_pids = _read_cgroup_procs(cfg, launch_user, cgroup_path)
        if not cgroup_pids:
            # Empty/absent cgroup → the workload is stopped or its path went
            # stale (restart). Confirm with the operator's liveness probe,
            # bounded once per distinct resource, only here.
            _mark_runtime_down(
                cfg,
                group,
                runtimes=runtimes,
                launch_user=launch_user,
            )
            continue
        if len(group) == 1:
            # One session per resource: cgroup.procs IS that session's set —
            # no environ read needed (the common case, fully unprivileged).
            rss_kib, cpu_pct = sum_usage_for_pids(cgroup_pids, proc_rows)
            group[0].rss_kib = rss_kib
            group[0].cpu_pct = cpu_pct
            continue
        # ≥2 sessions share this cgroup → split by the per-process
        # ``UXON_SESSION`` marker (privileged environ read, batched once).
        pid_to_session = _read_pid_sessions(cfg, launch_user, cgroup_pids)
        if pid_to_session is None:
            # No privilege / unreadable → degrade to the per-resource SHARED
            # figure: every sharing session shows the summed total, never an
            # error.
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

    for group in down_probe_groups.values():
        _mark_runtime_down(
            cfg,
            group,
            runtimes=runtimes,
            launch_user=launch_user,
        )


def _append_down_probe_group(
    down_probe_groups: dict[tuple[str, str], list[SessionInfo]], session: SessionInfo
) -> None:
    if not session.runtime or not session.runtime_resource:
        return
    down_probe_groups.setdefault((session.runtime, session.runtime_resource), []).append(session)


def _mark_runtime_down(
    cfg: Config,
    group: list[SessionInfo],
    *,
    runtimes: dict[str, WorkloadRuntimeSpec] | None,
    launch_user: str,
) -> None:
    """Flag a group's sessions runtime-down iff ``ready_command`` confirms it.

    Called only when the cgroup is empty/unresolvable. The liveness probe runs
    once per distinct resource. The session's runtime is
    looked up via ``runtimes``; without an ``ready_command`` template
    (or with no profile threaded in) uxon cannot confirm down-state, so the
    sessions stay at the already-set ``0``/``—`` degrade rather than asserting a
    "down" it cannot verify.
    """
    name = group[0].runtime_resource
    if runtimes is not None and group[0].runtime:
        profile = runtimes.get(group[0].runtime)
        if profile is None or not profile.ready_command:
            return
        from uxon.infra.runtime import probe_runtime_state_for_profile

        if probe_runtime_state_for_profile(cfg, profile, name, launch_user)[0] == "no":
            for session in group:
                session.runtime_down = True
        return


RuntimeIdentityState = Literal["current", "unresolved", "stale"]


def _runtime_identity_state(
    cfg: Config,
    session: SessionInfo,
    *,
    runtimes: dict[str, WorkloadRuntimeSpec] | None,
    launch_user: str,
) -> RuntimeIdentityState:
    if not session.launch_record_verified:
        return "stale"
    if not session.runtime or not session.runtime_resource:
        return "stale"
    if not session.runtime_id or not session.runtime_epoch:
        return "stale"
    if runtimes is None:
        return "stale"
    profile = runtimes.get(session.runtime)
    if profile is None:
        return "stale"
    if profile.fingerprint != session.runtime_fingerprint:
        return "stale"
    from uxon.infra.runtime import current_runtime_identity_for_profile

    live = current_runtime_identity_for_profile(cfg, profile, session.runtime_resource, launch_user)
    if live is None:
        return "unresolved"
    if live.id == session.runtime_id and live.epoch == session.runtime_epoch:
        return "current"
    return "stale"


def _read_cgroup_procs(cfg: Config, launch_user: str, cgroup_path: str) -> list[int]:
    """Read one runtime cgroup from inside the selected execution boundary."""
    from uxon.infra.runtime_telemetry import read_cgroup_members

    return read_cgroup_members(cfg, launch_user, cgroup_path)


def _read_pid_sessions(cfg: Config, launch_user: str, pids: list[int]) -> dict[int, str] | None:
    """Read workload markers inside the selected execution boundary."""
    if not pids:
        return {}
    from uxon.infra.runtime_telemetry import read_session_markers

    return read_session_markers(cfg, launch_user, pids)


def collect_session_snapshot_for_user(
    cfg: Config,
    user: str,
    session_prefix: str,
    socket_path: str | None,
    *,
    legacy_prefixes: tuple[str, ...] = (),
    runtimes: dict[str, WorkloadRuntimeSpec] | None = None,
) -> UserSessionSnapshot:
    # Demo-mode short-circuit: when ``UXON_DEMO_HOSTS`` is set, bypass
    # tmux entirely and read the synthetic-local envelope. Returning
    # ``[]`` for an absent envelope mirrors production's "empty tmux
    # socket" path, so screenshots on a multi-tenant box don't leak the
    # caller's real sessions. The legacy_prefixes argument is irrelevant
    # in demo mode — scenario envelopes already carry fully-qualified
    # session names.
    _demo_dir = demo.demo_hosts_dir()
    if _demo_dir is not None:
        demo_sessions = tuple(demo.load_demo_local_sessions(_demo_dir, user))
        return UserSessionSnapshot(
            user=user,
            server_state="running" if demo_sessions else "absent",
            sessions=demo_sessions,
        )

    # Listing runs without a TTY (CLI ``list``, TUI background poll,
    # remote aggregator). Use the non-interactive sudo prefix so a
    # missing NOPASSWD grant returns non-zero immediately rather than
    # blocking on a hidden password prompt.
    server = tmux.probe_tmux_server(cfg, user, socket_path)
    if server.state == "absent":
        if socket_path is not None:
            _gc_launch_records(cfg, user, socket_path, set())
        return UserSessionSnapshot(user=user, server_state="absent", sessions=())
    if server.state == "unreachable":
        location = socket_path or "the default socket"
        fail(f"tmux server for {user!r} is unreachable at {location}: {server.error}")
    base = tmux.tmux_base(cfg, user, socket_path, nonint=True)

    # Environment markers are diagnostic only. The finalized launch record is
    # the authority for profile, agent, and workload identity.
    fmt = (
        "#{session_name}\t#{session_id}\t#{session_attached}\t#{session_windows}"
        "\t#{session_created}\t#{session_activity}"
        "\t#{E:" + LAUNCH_NONCE_ENV + "}\t#{E:" + RUNTIME_RESOURCE_ENV + "}"
        "\t#{E:" + RUNTIME_CGROUP_ENV + "}"
    )
    rows = run_cmd(base + ["list-sessions", "-F", fmt]).stdout.splitlines()
    sessions: list[SessionInfo] = []
    live_record_keys: set[tuple[str, str, str]] = set()
    known_prefixes = (session_prefix, *legacy_prefixes)
    for row in rows:
        parts = row.split("\t")
        if len(parts) != 9:
            continue
        (
            name,
            session_id,
            attached,
            windows,
            created_ts,
            activity_ts,
            launch_nonce,
            runtime_marker,
            _runtime_cgroup_marker,
        ) = parts
        if not any(name.startswith(p) for p in known_prefixes):
            continue
        if socket_path is not None and launch_nonce:
            live_record_keys.add((socket_path, name, launch_nonce))

        pane_fmt = "#{pane_active}\t#{pane_pid}\t#{pane_current_command}\t#{pane_current_path}"
        pane_result = run_cmd(base + ["list-panes", "-t", name, "-F", pane_fmt], check=False)
        if pane_result.returncode != 0:
            detail = (pane_result.stderr or pane_result.stdout or "tmux list-panes failed").strip()
            fail(f"unable to inspect tmux session {name!r}: {detail}")
        pane_rows = pane_result.stdout.splitlines()
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
        _, _profile, _, _legacy = _parsed
        record = None
        if socket_path is not None:
            record = read_verified_record(
                socket_path,
                TmuxSessionMetadata(
                    session_id=session_id,
                    created=created_ts,
                    name=name,
                    launch_nonce=launch_nonce,
                ),
                override_dir=Path(cfg.launch_record_dir) if cfg.launch_record_dir else None,
                require_owner=not bool(cfg.launch_record_dir),
                shared=bool(cfg.launch_record_dir),
                launch_user=user,
            )
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
                agent=str(record.get("agent", "")) if record else "",
                profile=str(record.get("profile") or _profile) if record else _profile,
                legacy=_legacy,
                tmux_session_id=session_id,
                tmux_session_created=created_ts,
                launch_nonce=launch_nonce,
                launch_record_verified=record is not None,
                launch_user=str(record.get("launch_user", "")) if record else "",
                execution_backend=(str(record.get("execution_backend", "")) if record else ""),
                execution_fingerprint=(
                    str(record.get("execution_fingerprint", "")) if record else ""
                ),
                runtime=str(record.get("runtime", "")) if record else "",
                runtime_kind=str(record.get("runtime_kind", "")) if record else "",
                runtime_fingerprint=(str(record.get("runtime_fingerprint", "")) if record else ""),
                runtime_resource=str(record.get("runtime_resource", "")) if record else "",
                runtime_cgroup=str(record.get("runtime_cgroup", "")) if record else "",
                runtime_id=str(record.get("runtime_id", "")) if record else "",
                runtime_epoch=str(record.get("runtime_epoch", "")) if record else "",
                runtime_marker=runtime_marker,
            )
        )
    if socket_path is not None:
        _gc_launch_records(cfg, user, socket_path, live_record_keys)
    enrich_session_usage(
        cfg,
        sessions,
        runtimes=runtimes,
        launch_user=user,
    )
    return UserSessionSnapshot(user=user, server_state="running", sessions=tuple(sessions))


def collect_sessions_for_user(
    cfg: Config,
    user: str,
    session_prefix: str,
    socket_path: str | None,
    *,
    legacy_prefixes: tuple[str, ...] = (),
    runtimes: dict[str, WorkloadRuntimeSpec] | None = None,
) -> list[SessionInfo]:
    """Return only session rows for callers that do not need server liveness."""
    return list(
        collect_session_snapshot_for_user(
            cfg,
            user,
            session_prefix,
            socket_path,
            legacy_prefixes=legacy_prefixes,
            runtimes=runtimes,
        ).sessions
    )


def collect_current_session_snapshot(cfg: Config, user: str) -> UserSessionSnapshot:
    """Collect the configured dedicated socket once for launch planning."""
    return collect_session_snapshot_for_user(
        cfg,
        user,
        cfg.session_prefix,
        tmux.tmux_socket_path(cfg, user),
        legacy_prefixes=cfg.legacy_session_prefixes,
        runtimes=cfg.runtimes,
    )


def collect_sessions(users: list[str], cfg: Config) -> list[SessionInfo]:
    sessions: list[SessionInfo] = []
    for user in normalize_user_list(users):
        sessions.extend(collect_current_session_snapshot(cfg, user).sessions)
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
    extra: dict[str, Any] | None = None,
) -> SessionInfo:
    """Resolve a session and, on no-match failure, emit the ``not_found``
    audit outcome before re-raising.

    The audit contract enumerates
    ``outcome ∈ {"ok", "denied", "error", "not_found"}`` for
    ``session.attach.dispatch`` and ``session.kill``. Without this wrapper the
    ``not_found`` outcome would never appear, because
    :func:`resolve_session` raises :class:`SystemExit` via :func:`fail`
    before any caller-side audit fires.

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
            "session": identifier or "",
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
        cfg,
        launch_user,
        cfg.session_prefix,
        socket_path=None,
        legacy_prefixes=cfg.legacy_session_prefixes,
    )
    return compatible_indexed_sessions(
        stem,
        cfg.launch.default_profile,
        compatibility_root,
        sessions,
        prefix=cfg.session_prefix,
        legacy_prefixes=cfg.legacy_session_prefixes,
        require_verified=False,
    )


def legacy_socket_conflict_hint(cfg: Config, launch_user: str, existing: list[SessionInfo]) -> str:
    attach_cmd = shlex.join(
        tmux.tmux_base(cfg, launch_user) + ["attach-session", "-t", existing[0].name]
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
    profile_id: str,
    *,
    stem: str | None = None,
    compatibility_root: str | None = None,
) -> tuple[SessionInfo, ...]:
    """Return launch user's sessions compatible with a target and launch profile.

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
    derives the *same* stem the planner used — generalising here
    rather than always deriving from the basename is the fix that keeps
    the attach guard reliable across repos.
    """
    target_lexical = os.path.normpath(target_dir)
    session_stem = stem if stem is not None else session_stem_for_path(target_lexical)
    root = (
        os.path.normpath(compatibility_root) if compatibility_root is not None else target_lexical
    )
    sessions = collect_sessions([launch_user], cfg)
    matches = compatible_indexed_sessions(
        session_stem,
        profile_id,
        root,
        sessions,
        prefix=cfg.session_prefix,
        legacy_prefixes=cfg.legacy_session_prefixes,
    )
    return tuple(matches)

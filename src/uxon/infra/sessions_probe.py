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
from typing import Any

from uxon.domain.authz import canonical
from uxon.domain.config import Config
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
from uxon.infra.process import run_cmd


def enrich_session_usage(sessions: list[SessionInfo]) -> None:
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

    for session in sessions:
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


def collect_sessions_for_user(
    user: str,
    session_prefix: str,
    socket_path: str | None,
    *,
    legacy_prefixes: tuple[str, ...] = (),
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

    fmt = "#{session_name}\t#{session_attached}\t#{session_windows}\t#{session_created}\t#{session_activity}"
    rows = run_cmd(base + ["list-sessions", "-F", fmt]).stdout.splitlines()
    sessions: list[SessionInfo] = []
    known_prefixes = (session_prefix, *legacy_prefixes)
    for row in rows:
        parts = row.split("\t")
        if len(parts) != 5:
            continue
        name, attached, windows, created_ts, activity_ts = parts
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
        if _agent not in ("claude", "codex", "cursor"):
            _agent = "unknown"
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
            )
        )
    enrich_session_usage(sessions)
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

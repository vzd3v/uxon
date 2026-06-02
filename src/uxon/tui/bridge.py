# SPDX-License-Identifier: MIT
"""TUI bridge: wires live session data + command callbacks into a TuiContext.

:class:`TuiBridge` replaces the former ``cli._build_tui_context`` god-closure.
The stable inputs (``cfg`` / ``launch_user`` / ``cwd``) are instance attributes;
every former nested ``on_*`` closure is now a method that delegates to the
``app.*`` use-cases and ``infra.*`` adapters. Per-build state (the probed
``sudo_caps``, the fleet fetch semaphore, the per-host circuit breakers) is
constructed in :func:`build_tui_context` and stashed on the bridge instance —
a fresh bridge is built on every refresh, so this state never outlives one
``TuiContext``.

This module lives in ``tui/`` and may import ``domain`` / ``infra`` / ``app``;
it must NOT import ``uxon.cli`` (the composition root imports *this*, lazily).
"""

from __future__ import annotations

import contextlib
import io as _io
import os
import shlex
import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

import uxon.app.agent_select as agent_select
import uxon.app.attach as attach_app
import uxon.app.launch as launch_app
import uxon.app.new as new_app
from uxon.domain.args import ParsedArgs
from uxon.domain.authz import canonical
from uxon.domain.config import DEFAULT_CONFIG, Config
from uxon.domain.format import compact_time, fmt_epoch, format_cpu_pct, format_rss_kib
from uxon.domain.session import (
    SessionInfo,
    allocate_session_name,
    compatible_indexed_sessions,
    parse_session_name,
    session_stem_for_path,
    session_stem_for_worktree,
)
from uxon.errors import fail
from uxon.infra import (
    config_loader,
    git,
    host_status_probe,
    identity,
    process,
    sessions_probe,
    tmux,
    version_probe,
)

if TYPE_CHECKING:
    from uxon.domain.session import TuiSession
    from uxon.domain.sudo import SudoCapability
    from uxon.tui.context import TuiContext


# ── callback-stderr plumbing (TUI-only; moved out of cli with the bridge) ──


def _sanitize_callback_stderr(raw: str) -> str:
    """Strip boilerplate (``uxon:`` prefix, trailing blank lines) from
    captured stderr so it reads cleanly on a TUI status line.

    Keeps multi-line lists intact (e.g. allowed-roots bullets) with their
    indentation normalised to two spaces. Called by
    :func:`_wrap_tui_callback`.
    """
    out: list[str] = []
    for line in raw.splitlines():
        line = line.rstrip()
        if not line:
            continue
        if line.startswith("uxon:   - "):
            out.append("  - " + line[len("uxon:   - ") :])
        elif line.startswith("uxon: "):
            out.append(line[len("uxon: ") :])
        else:
            out.append(line)
    return "\n".join(out)


def _wrap_tui_callback(fn: Any, callback_error_cls: type[Exception]) -> Any:
    """Wrap a callback so exceptions surface on the TUI status line.

    Captures anything the callback writes to ``stderr`` (e.g. the message
    :func:`fail` prints before ``raise SystemExit``), and on exception
    raises ``callback_error_cls`` with the captured text as its payload.
    A plain return is passed through untouched.

    This is the single place that converts uxon's ``fail() → SystemExit``
    style into a structured error the TUI can render in red without the
    blessed fullscreen context swallowing the message.
    """

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        buf = _io.StringIO()
        try:
            with contextlib.redirect_stderr(buf):
                return fn(*args, **kwargs)
        except SystemExit as exc:
            msg = _sanitize_callback_stderr(buf.getvalue())
            if not msg:
                code = exc.code if exc.code is not None else "?"
                msg = f"command exited with code {code}"
            raise callback_error_cls(msg) from exc
        except callback_error_cls:
            raise
        except Exception as exc:  # pragma: no cover — defensive
            detail = _sanitize_callback_stderr(buf.getvalue())
            head = str(exc) or exc.__class__.__name__
            msg = f"{head}\n{detail}" if detail else head
            raise callback_error_cls(msg) from exc

    wrapper.__name__ = getattr(fn, "__name__", "wrapped_callback")
    return wrapper


# ── module-level helpers (no per-build state) ──


def _list_existing_projects(root: str) -> list[tuple[str, str]]:
    """List ``(name, compact_mtime)`` under ``new_project_root``, sorted by name.

    ``compact_mtime`` uses :func:`compact_time`: ``HH:MM`` if the
    directory was last modified today, ``MM-DD`` otherwise. ``"-"``
    when the stat call fails.
    """
    try:
        entries = [
            (e.name, str(e))
            for e in Path(root).iterdir()
            if e.is_dir() and not e.name.startswith(".")
        ]
    except OSError:
        return []
    entries.sort()
    result: list[tuple[str, str]] = []
    for name, path in entries:
        try:
            mtime = int(os.stat(path).st_mtime)
            mtime_str = compact_time(fmt_epoch(str(mtime)))
        except OSError:
            mtime_str = "-"
        result.append((name, mtime_str))
    return result


def _to_tui_session(
    s: SessionInfo, prefix: str, legacy_prefixes: tuple[str, ...] = ()
) -> TuiSession:
    from uxon.domain.session import TuiSession  # noqa: PLC0415

    short = s.name[len(prefix) :] if s.name.startswith(prefix) else s.name
    for lp in legacy_prefixes:
        if s.name.startswith(lp):
            short = s.name[len(lp) :]
            break
    parsed = parse_session_name(s.name, prefix=prefix, legacy_prefixes=legacy_prefixes)
    if parsed is not None:
        stem, agent, _idx, legacy = parsed
    else:
        stem, agent, legacy = s.name, "unknown", False
    return TuiSession(
        name=s.name,
        short=short,
        attached=s.attached == "1",
        pid=str(s.active_pid) if s.active_pid is not None else "-",
        cpu=format_cpu_pct(s.cpu_pct),
        ram=format_rss_kib(s.rss_kib),
        created=compact_time(s.created),
        last_activity=compact_time(s.last_attached),
        cmd=s.active_cmd or "-",
        path=s.active_path or "-",
        user=s.user,
        stem=stem,
        agent=agent,
        legacy=legacy,
        created_iso=s.created,
        last_attached_iso=s.last_attached,
    )


def _load_settings_sources(cwd: str) -> tuple[dict, dict, Path | None]:
    """Load raw repo + project config data (unmerged) plus the project path.

    Used by the TUI settings screen so it can show each value's origin and
    write back only to the repo-level file.
    """
    repo_cfg = config_loader.repo_config_path()
    repo_data = config_loader.load_toml(repo_cfg)
    seed_allowed = [
        canonical(p) for p in repo_data.get("allowed_roots", DEFAULT_CONFIG["allowed_roots"])
    ]
    proj_cfg = config_loader.find_project_config(cwd, seed_allowed)
    proj_data = config_loader.load_toml(proj_cfg) if proj_cfg else {}
    return repo_data, proj_data, proj_cfg


def _plan_tui_run_agent(
    cfg: Config,
    launch_user: str,
    cwd: str,
    agent_id: str,
    mode_id: str,
    worktree: tuple[str, str] | None = None,
):
    """Build a LaunchRequest for the TUI "New session in current folder" action.

    Mirrors :func:`do_run` minus the terminal handoff: gates via
    :func:`ensure_launch_target_allowed` (writable + ``allowed_roots``
    whitelist when configured), allocates a session name, returns a
    LaunchRequest. The agent and permission mode are picked by the TUI
    callback before this is called — no probe needed here.

    When ``worktree`` is ``(repo_root, branch)`` the session stem is the
    repo-qualified :func:`session_stem_for_worktree` (§2.5) — identical to
    the stem the worktree-aware probe derives — instead of the
    basename-only :func:`session_stem_for_path`. ``cwd`` is the worktree
    path in that case; for a plain (primary / non-git) target ``worktree``
    is ``None`` and the basename stem is used unchanged.
    """
    launch_app.ensure_launch_target_allowed(cfg, launch_user, cwd)
    target_dir = cwd
    if worktree is not None:
        repo_root, branch = worktree
        session_stem = session_stem_for_worktree(repo_root, branch)
    else:
        session_stem = session_stem_for_path(target_dir)
    sessions = sessions_probe.collect_sessions([launch_user], cfg)
    session = allocate_session_name(
        session_stem, agent_id, target_dir, sessions, prefix=cfg.session_prefix
    )
    args = ParsedArgs(action="run", agent=agent_id, permission_mode=mode_id)
    return tmux._build_tmux_launch_request(
        target_dir, session, args, cfg, None, launch_user, server_running=bool(sessions)
    )


def _resolve_tui_project_dir(cfg: Config, launch_user: str, name: str) -> str:
    """Shared validation + directory creation for both TUI project flows.

    Returns the canonical absolute path; raises via ``fail()`` if ``name``
    is malformed, the parent is not writable, or the path violates a
    non-empty ``allowed_roots`` whitelist.
    """
    if "/" in name or name in (".", ".."):
        fail(f"invalid name: {name}")
    project_dir = canonical(os.path.join(cfg.new_project_root, name))
    new_app.ensure_new_project_target_allowed(cfg, launch_user, project_dir)
    process.run_cmd(identity.command_prefix_for_user(launch_user) + ["mkdir", "-p", project_dir])
    return project_dir


def _plan_tui_existing_session_or_launch(
    cfg: Config,
    launch_user: str,
    project_dir: str,
    name: str,
    args: ParsedArgs,
):
    """Allocate + launch a fresh session under ``project_dir``.

    Shared tail of both TUI project flows. The TUI is the sole owner of
    the attach-vs-launch decision now: it probes via
    :func:`sessions_probe.probe_tui_compatible_sessions` after the operator picks
    agent+mode, surfaces the choice in a modal, and routes "attach" to
    :func:`on_attach` directly. By the time this planner runs we already
    know the operator wants a new (parallel) session — the only thing
    left to do is allocate the next available index and emit the launch
    request. :func:`compatible_indexed_sessions` is still called for its
    path-safety side effect (it ``fail()``-s if a same-named session
    points outside ``project_dir``).
    """
    session_stem = session_stem_for_path(project_dir)
    compatibility_root = project_dir
    _agent = agent_select.resolve_agent_id(
        cfg, launch_user, args.agent or None, report=args.host_report
    )
    args.agent = _agent
    sessions = sessions_probe.collect_sessions([launch_user], cfg)
    # Path-safety side effect — raises via fail() on a path mismatch.
    compatible_indexed_sessions(
        session_stem,
        _agent,
        compatibility_root,
        sessions,
        prefix=cfg.session_prefix,
        legacy_prefixes=cfg.legacy_session_prefixes,
    )
    sessions_probe.repeat_guardrail_for_legacy_socket(
        cfg, launch_user, session_stem, compatibility_root
    )
    session = allocate_session_name(
        session_stem, _agent, compatibility_root, sessions, prefix=cfg.session_prefix
    )
    return tmux._build_tmux_launch_request(
        project_dir, session, args, cfg, None, launch_user, server_running=bool(sessions)
    )


def _plan_tui_create_new_agent(
    cfg: Config,
    launch_user: str,
    name: str,
    agent_id: str,
    mode_id: str,
    git_profile: str,
):
    """Build a LaunchRequest for the TUI "Create new project" flow.

    Creates the project directory (if missing), optionally creates the
    git remote, and — when a compatible session already exists — forces
    ``attach`` semantics (the TUI cannot safely prompt via stdin inside
    a blessed context). ``git_profile`` is the (possibly empty) name of
    a ``[[git_remote_profiles]]`` entry; when set this calls
    :func:`_do_create_git_remote`. The "Open existing project" flow must
    never call this — see :func:`_plan_tui_open_existing_agent`.
    """
    project_dir = _resolve_tui_project_dir(cfg, launch_user, name)
    args = ParsedArgs(
        action="new",
        target_id=name,
        agent=agent_id,
        permission_mode=mode_id,
        git_remote=git_profile or None,
        repeat_mode="attach",
    )
    if args.git_remote:
        new_app._do_create_git_remote(args, cfg, launch_user, project_dir, name, None)
    return _plan_tui_existing_session_or_launch(cfg, launch_user, project_dir, name, args)


def _plan_tui_open_existing_agent(
    cfg: Config,
    launch_user: str,
    name: str,
    agent_id: str,
    mode_id: str,
):
    """Build a LaunchRequest for the TUI "Open existing project" flow.

    By construction this function has **no** ``git_profile`` parameter
    and never calls :func:`_do_create_git_remote`: opening an existing
    project must not have any git side effect, regardless of
    ``git_create_enabled`` or profile configuration.
    """
    project_dir = _resolve_tui_project_dir(cfg, launch_user, name)
    args = ParsedArgs(
        action="new",
        target_id=name,
        agent=agent_id,
        permission_mode=mode_id,
        git_remote=None,
        repeat_mode="attach",
    )
    return _plan_tui_existing_session_or_launch(cfg, launch_user, project_dir, name, args)


def _build_on_remote_attach_callback(cfg: Config):
    """Return the TUI on_remote_attach callback for the given cfg.

    Pulled out as a module-level factory so tests can construct it
    with a synthetic Config without spinning up the full
    build_tui_context machinery.
    """
    from uxon.domain.launch_request import LaunchRequest
    from uxon.infra.remote.collector import DEFAULT_CONNECT_TIMEOUT_SEC
    from uxon.infra.remote.ssh_argv import build_peer_ssh_argv
    from uxon.infra.remote_hosts import find_host
    from uxon.tui.context import CallbackError

    def on_remote_attach(host_name: str, user: str, name: str) -> LaunchRequest:
        import uuid as _uuid

        from uxon.infra import audit as _audit

        peer = find_host(cfg.remote_hosts, host_name)
        if peer is None:
            raise CallbackError(f"unknown remote host: {host_name}")
        # Pass correlation_id explicitly via kwargs rather than seeding
        # ``_audit._correlation_id``: the TUI process is long-lived, and a
        # left-behind global would leak into subsequent local audit events
        # (next session.attach / session.kill picked up the stale UUID).
        corr_id = str(_uuid.uuid4())
        _audit.audit(
            "attach.remote.out",
            peer_name=peer.name,
            ssh_alias=peer.ssh_alias,
            target_user=user,
            target_session=name,
            correlation_id=corr_id,
        )
        # target first (see _do_attach_remote for rationale).
        remote_cmd = (
            f"{shlex.quote(peer.remote_uxon)} attach {shlex.quote(name)} "
            f"--user {shlex.quote(user)} "
            f"--audit-correlation-id {shlex.quote(corr_id)}"
        )
        argv = build_peer_ssh_argv(
            command_template=peer.command_template,
            extra_ssh_options=peer.extra_ssh_options,
            ssh_alias=peer.ssh_alias,
            remote_uxon=peer.remote_uxon,
            remote_command=remote_cmd,
            allocate_tty=True,
            connect_timeout=DEFAULT_CONNECT_TIMEOUT_SEC,
            # See _do_attach_remote: interactive attach must not share
            # the poller's ControlMaster — a wedged master would hang
            # the TUI handoff at ``unix_wait_for_peer``.
            ssh_multiplex="off",
            ssh_control_persist_seconds=cfg.ssh_control_persist_seconds,
        )
        return LaunchRequest(cmd=tuple(argv), label=f"attach {name}@{host_name}")

    return on_remote_attach


class TuiBridge:
    """Holds the stable TUI inputs and the command callbacks.

    Every ``on_*`` method delegates to the ``app.*`` use-cases and
    ``infra.*`` adapters. Per-build state — the probed ``sudo_caps``, the
    fleet ``fetch`` semaphore, and the per-host circuit breakers — is
    populated by :func:`build_tui_context` before the context is wired,
    and lives only for the lifetime of one ``TuiContext`` (a fresh bridge
    is built on every refresh).
    """

    def __init__(self, cfg: Config, launch_user: str, cwd: str) -> None:
        self.cfg = cfg
        self.launch_user = launch_user
        self.cwd = cwd
        # Per-build state, set by build_tui_context. Empty placeholders
        # for the skeleton tick; real values once a non-skeleton build runs.
        self.sudo_caps: SudoCapability | None = None
        # Captured caps for on_refresh reuse: None on a skeleton ctx so the
        # first real load probes; the probed caps thereafter (one-shot).
        self.captured_sudo_caps: SudoCapability | None = None
        self._fetch_sem: threading.Semaphore | None = None
        self._host_breakers: dict[str, Any] = {}

    # ── attach / kill ──

    def on_attach(self, user: str, name: str):
        # TUI Enter on a local row dispatches a direct
        # ``tmux attach-session`` (no ``uxon`` wrapper) — emit
        # ``session.attach`` here so the operation is auditable.
        # ``do_attach``'s emit only covers the CLI-side ``uxon attach``
        # invocation; the TUI request bypasses that path entirely.
        from uxon.infra import audit as _audit

        fresh = sessions_probe.collect_sessions([user], self.cfg)
        target = sessions_probe._resolve_or_audit_not_found(
            name,
            fresh,
            self.cfg,
            audit_event="session.attach",
            target_user=user,
        )
        _audit.audit("session.attach", session=target.name, target_user=user)
        return tmux._build_tmux_attach_request(target, self.cfg, user)

    def on_kill(self, user: str, name: str) -> None:
        # TUI 'k' on a local row runs ``tmux kill-session`` directly
        # via ``process.run_cmd`` — emit ``session.kill`` after success so the
        # operation is auditable (mirrors do_kill same-user pattern).
        from uxon.infra import audit as _audit

        fresh = sessions_probe.collect_sessions([user], self.cfg)
        target = sessions_probe._resolve_or_audit_not_found(
            name,
            fresh,
            self.cfg,
            audit_event="session.kill",
            target_user=user,
            extra={"force": True, "dry_run": False},
        )
        # TUI-driven kill: no TTY available, use non-interactive sudo.
        full = tmux.configured_tmux_base(self.cfg, user, nonint=True) + [
            "kill-session",
            "-t",
            target.name,
        ]
        try:
            process.run_cmd(full, check=True)
        except subprocess.CalledProcessError as exc:
            _audit.audit(
                "session.kill",
                outcome="error",
                session=target.name,
                target_user=user,
                force=True,
                dry_run=False,
                rc=exc.returncode,
            )
            raise
        _audit.audit(
            "session.kill",
            session=target.name,
            target_user=user,
            force=True,
            dry_run=False,
        )

    def on_kill_all(self) -> None:
        # TUI 'D' / kill-all-mine. Mirrors ``on_kill_all_reachable``'s
        # audit shape (``target_users``, ``killed_count``, ``dry_run``)
        # for the single-user case.
        from uxon.infra import audit as _audit

        fresh = sessions_probe.collect_sessions([self.launch_user], self.cfg)
        killed_count = 0
        for s in fresh:
            full = tmux.configured_tmux_base(self.cfg, self.launch_user, nonint=True) + [
                "kill-session",
                "-t",
                s.name,
            ]
            cp = process.run_cmd(full, check=False)
            if cp.returncode == 0:
                killed_count += 1
        _audit.audit(
            "session.kill_all",
            outcome="ok" if killed_count == len(fresh) else "error",
            target_users=[self.launch_user],
            killed_count=killed_count,
            dry_run=False,
        )

    def on_remote_kill(self, host_name: str, user: str, name: str) -> None:
        """TUI dispatch: kill ``name`` belonging to ``user`` on peer ``host_name``.

        Reuses the same SSH gesture as the CLI's ``uxon kill --host
        <alias> --user <user> --force <id>``: the peer's own ``uxon
        kill`` runs the per-target sudo probe, so the local side does
        not need to know the peer's user table. ``--force`` is passed
        on the wire because confirmation is a local-UI concern (the TUI
        already prompted before this callback fires).

        Failures surface as :class:`CallbackError` via the
        ``_wrap_tui_callback`` shim — :meth:`MainScreen.action_kill`
        renders them as a red toast (the dashboard's ``d`` binding
        dispatches here when the cursor sits on a remote row).
        """
        import uuid as _uuid

        from uxon.infra import audit as _audit
        from uxon.infra.remote.collector import (
            DEFAULT_CONNECT_TIMEOUT_SEC,
            DEFAULT_TOTAL_TIMEOUT_SEC,
        )
        from uxon.infra.remote.master_recovery import recover_wedged_master
        from uxon.infra.remote.ssh_argv import build_peer_ssh_argv
        from uxon.infra.remote_hosts import find_host

        peer = find_host(self.cfg.remote_hosts, host_name)
        if peer is None:
            fail(f"unknown remote host: {host_name}", 1)
        # See on_remote_attach: TUI process outlives the dispatch, so we
        # avoid the module-level correlation_id global to keep state from
        # bleeding into the next local emit.
        corr_id = str(_uuid.uuid4())
        _audit.audit(
            "kill.remote.out",
            peer_name=peer.name,
            ssh_alias=peer.ssh_alias,
            target_user=user,
            target_session=name,
            force=True,
            dry_run=False,
            correlation_id=corr_id,
        )
        # target first (see _do_attach_remote for rationale).
        remote_cmd = (
            f"{shlex.quote(peer.remote_uxon)} kill {shlex.quote(name)} --force "
            f"--user {shlex.quote(user)} "
            f"--audit-correlation-id {shlex.quote(corr_id)}"
        )
        ssh_argv = build_peer_ssh_argv(
            command_template=peer.command_template,
            extra_ssh_options=peer.extra_ssh_options,
            ssh_alias=peer.ssh_alias,
            remote_uxon=peer.remote_uxon,
            remote_command=remote_cmd,
            allocate_tty=False,
            connect_timeout=DEFAULT_CONNECT_TIMEOUT_SEC,
            ssh_multiplex=self.cfg.ssh_multiplex,
            ssh_control_persist_seconds=self.cfg.ssh_control_persist_seconds,
        )

        # Mirrors ``_do_kill_remote::_emit_kill_remote_error`` (CLI
        # path) so the TUI and CLI failure trails are symmetric: an
        # operator querying ``EVENT=kill.remote.out OUTCOME=error``
        # finds TUI-originated ssh failures alongside CLI ones.
        def _emit_kill_remote_error(error: str, rc: int) -> None:
            _audit.audit(
                "kill.remote.out",
                outcome="error",
                peer_name=peer.name,
                ssh_alias=peer.ssh_alias,
                target_user=user,
                target_session=name,
                force=True,
                dry_run=False,
                correlation_id=corr_id,
                rc=rc,
                error=error[:256],
            )

        try:
            cp = subprocess.run(
                ssh_argv,
                capture_output=True,
                text=True,
                timeout=DEFAULT_TOTAL_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            if self.cfg.ssh_multiplex != "off":
                recover_wedged_master(peer)
            _emit_kill_remote_error("ssh timeout", 124)
            fail(f"ssh timeout after {DEFAULT_TOTAL_TIMEOUT_SEC}s talking to {host_name}", 1)
        except FileNotFoundError:
            _emit_kill_remote_error("ssh binary missing", 127)
            fail("ssh not installed on local host", 1)
        if cp.returncode != 0:
            stderr = (cp.stderr or "").strip().splitlines()
            tail = stderr[-1] if stderr else f"ssh exited {cp.returncode}"
            _emit_kill_remote_error(f"non-zero ssh rc: {tail}", cp.returncode)
            fail(f"remote kill on {host_name} failed: {tail}", 1)

    def on_kill_all_reachable(self) -> None:
        # Iterate the launch user plus every reachable peer user. An
        # empty ``reachable_users`` collapses to "kill all my own
        # sessions", which is the same behaviour the legacy
        # ``kill-all-global`` had when sudo was unavailable.
        reachable = self.sudo_caps.reachable_users if self.sudo_caps else frozenset()
        users = sorted({self.launch_user, *reachable})
        killed_count = 0
        attempted = 0
        for u in users:
            fresh = sessions_probe.collect_sessions([u], self.cfg)
            for s in fresh:
                full = tmux.configured_tmux_base(self.cfg, u, nonint=True) + [
                    "kill-session",
                    "-t",
                    s.name,
                ]
                cp = process.run_cmd(full, check=False)
                attempted += 1
                if cp.returncode == 0:
                    killed_count += 1
        # Operationally the most-significant kill_all path: cross-user
        # bulk kill from the TUI.  Audit emit covers the whole sweep,
        # not per-session — matches the spec's `target_users` /
        # `killed_count` shape.
        from uxon.infra import audit as _audit

        _audit.audit(
            "session.kill_all",
            outcome="ok" if killed_count == attempted else "error",
            target_users=users,
            killed_count=killed_count,
            dry_run=False,
        )

    # Legacy alias kept for any out-of-tree caller. The TUI dispatches
    # via ``on_kill_all_global`` (the field name on TuiContext); the
    # implementation now scopes to the reachable set.
    on_kill_all_global = on_kill_all_reachable

    # ── refresh / link health ──

    def on_refresh(self) -> TuiContext:
        # Re-read config so settings edits take effect immediately.
        # Always returns a fully loaded ctx (skeleton=False) — even when
        # the calling ctx was a skeleton, the caller wants real data.
        # We pass the captured caps (or None on the very first load)
        # so the probe runs at most once per process.
        fresh_cfg = config_loader.load_config(self.cwd)
        return build_tui_context(
            fresh_cfg,
            self.launch_user,
            self.cwd,
            sudo_caps_override=self.captured_sudo_caps,
        )

    def on_probe_link_health(self) -> object | None:
        return host_status_probe.read_ssh_link_health_status()

    # ── settings (superuser-only; safe to wire unconditionally) ──

    def get_settings_entries(self) -> list:
        from uxon.infra import settings as uxon_settings

        repo_data, proj_data, proj_cfg = _load_settings_sources(self.cwd)
        return uxon_settings.resolve_setting_entries(repo_data, proj_data, proj_cfg, DEFAULT_CONFIG)

    def on_setting_save(self, key: str, value: object) -> None:
        from uxon.infra import settings as uxon_settings

        uxon_settings.persist_repo_config_updates(config_loader.repo_config_path(), {key: value})

    def on_setting_remove(self, key: str) -> None:
        from uxon.infra import settings as uxon_settings

        uxon_settings.remove_repo_key(config_loader.repo_config_path(), key)

    def on_setting_save_mapping(self, key: str, mapping: dict) -> None:
        from uxon.infra import settings as uxon_settings

        uxon_settings.persist_repo_config_updates(config_loader.repo_config_path(), {key: mapping})

    def get_git_remote_profile_rows(self) -> list:
        return [
            (
                p.name,
                p.host,
                p.owner,
                p.auth,
                p.creds_user or self.launch_user,
                p.visibility,
                p.token_file or "-",
            )
            for p in self.cfg.git_remote_profiles
        ]

    # ── launch ──

    def on_launch_cwd(self, agent_id: str, mode_id: str):
        req = _plan_tui_run_agent(self.cfg, self.launch_user, self.cwd, agent_id, mode_id)
        # ``_plan_tui_run_agent`` only ever yields a launch (never an
        # attach), so this path is unconditional ``session.new``.
        from uxon.infra import audit as _audit

        _audit.audit(
            "session.new",
            agent=agent_id,
            project=self.cwd,
            branch="",
            session=attach_app._session_name_from_launch_label(req.label),
            dry_run=False,
        )
        return req

    def on_launch_new(self, name: str, agent_id: str, mode_id: str, git_profile: str):
        req = _plan_tui_create_new_agent(
            self.cfg, self.launch_user, name, agent_id, mode_id, git_profile
        )
        # The TUI planner no longer auto-attaches — every launch request
        # routed here is a fresh ``session.new``. The attach path is
        # owned by ``on_attach`` (which emits its own ``session.attach``
        # event when the operator picks "attach" in SessionChoiceScreen).
        project = canonical(os.path.join(self.cfg.new_project_root, name))
        from uxon.infra import audit as _audit

        _audit.audit(
            "session.new",
            agent=agent_id,
            project=project,
            branch="",
            session=attach_app._session_name_from_launch_label(req.label),
            dry_run=False,
        )
        return req

    def on_launch_existing(self, name: str, agent_id: str, mode_id: str):
        req = _plan_tui_open_existing_agent(self.cfg, self.launch_user, name, agent_id, mode_id)
        # Same as ``on_launch_new``: TUI owns attach decisions; this path
        # always emits ``session.new``.
        project = canonical(os.path.join(self.cfg.new_project_root, name))
        from uxon.infra import audit as _audit

        _audit.audit(
            "session.new",
            agent=agent_id,
            project=project,
            branch="",
            session=attach_app._session_name_from_launch_label(req.label),
            dry_run=False,
        )
        return req

    def on_probe_existing_sessions(
        self, target_dir: str, agent_id: str
    ) -> tuple[tuple[str, bool], ...]:
        """TUI probe: return (name, attached) pairs for launch_user's
        compatible sessions under ``target_dir`` + ``agent_id``.

        Called by the TUI between LaunchOptionsScreen (agent + mode pick)
        and the actual ``on_launch_*`` commit. An empty tuple means the
        TUI proceeds straight to launch; otherwise it pushes the
        SessionChoiceScreen modal to let the operator pick attach vs
        new-alongside.
        """
        matches = sessions_probe.probe_tui_compatible_sessions(
            self.cfg, self.launch_user, target_dir, agent_id
        )
        return tuple((s.name, s.attached == "1") for s in matches)

    def on_probe_worktrees(self, cwd_arg: str) -> list:
        """Workspaces for ``cwd_arg``'s repo (folders only). Non-git → [].

        Resolves ``cwd`` → primary repo root with the NON-interactive
        resolvers (Task 5) so the fullscreen TUI never blocks on a hidden
        ``sudo`` prompt, then lists worktrees under the same
        ``identity.nonint_command_prefix_for_user`` and parses with Task 2.
        """
        from uxon.infra.worktrees import parse_worktree_porcelain

        repo_root = git.git_repo_root_nonint_as_user(cwd_arg, self.launch_user)
        if not repo_root:
            return []
        primary = git.git_common_dir_root_as_user(cwd_arg, self.launch_user)
        if primary:
            repo_root = primary
        cp = subprocess.run(
            identity.nonint_command_prefix_for_user(self.launch_user)
            + ["git", "-C", repo_root, "worktree", "list", "--porcelain"],
            text=True,
            capture_output=True,
        )
        if cp.returncode != 0:
            return []
        return parse_worktree_porcelain(cp.stdout or "", repo_root=repo_root)

    def on_create_worktree(self, repo_root: str, branch: str, agent_id: str, mode_id: str):
        # plan_worktree_launch emits its own worktree.create + session.new
        # audit events. The TUI has no agent passthrough args (agent_args
        # defaults to None).
        return launch_app.plan_worktree_launch(
            self.cfg, self.launch_user, repo_root, branch, agent_id, mode_id
        )

    def on_launch_existing_worktree(
        self, repo_root: str, branch: str, worktree_path: str, agent_id: str, mode_id: str
    ):
        # Launch into an EXISTING worktree with the worktree-aware stem
        # (§2.5) — never re-creates the worktree.
        req = _plan_tui_run_agent(
            self.cfg,
            self.launch_user,
            worktree_path,
            agent_id,
            mode_id,
            worktree=(repo_root, branch),
        )
        from uxon.infra import audit as _audit

        _audit.audit(
            "session.new",
            agent=agent_id,
            project=worktree_path,
            branch=branch,
            session=attach_app._session_name_from_launch_label(req.label),
            dry_run=False,
        )
        return req

    def on_probe_existing_worktree_sessions(
        self, worktree_path: str, repo_root: str, branch: str, agent_id: str
    ) -> tuple[tuple[str, bool], ...]:
        matches = sessions_probe.probe_tui_compatible_sessions(
            self.cfg,
            self.launch_user,
            worktree_path,
            agent_id,
            stem=session_stem_for_worktree(repo_root, branch),
            compatibility_root=worktree_path,
        )
        return tuple((s.name, s.attached == "1") for s in matches)

    def on_probe_cwd_writable(self) -> bool:
        return launch_app.is_launch_target_allowed(self.cfg, self.launch_user, self.cwd)

    def on_probe_dir_launchable(self, target_dir: str) -> bool:
        # Same predicate as on_probe_cwd_writable, parameterised by target —
        # gates the "Open existing project" launch (no pre-probed slot).
        return launch_app.is_launch_target_allowed(self.cfg, self.launch_user, target_dir)


def build_tui_context(
    cfg: Config,
    launch_user: str,
    cwd: str,
    *,
    skeleton: bool = False,
    sudo_caps_override: SudoCapability | None = None,
) -> TuiContext:
    """Build a TuiContext from live session data.

    When ``skeleton=True`` we skip every blocking I/O call (tmux, sudo
    probes, project directory scans) and return a minimal context with
    ``loading=True``. The TUI mounts immediately and a background worker
    triggers ``on_refresh`` (which calls this again with
    ``skeleton=False``) to fill in the real data.

    ``sudo_caps_override`` lets the caller (typically ``on_refresh``)
    reuse a previously-probed :class:`SudoCapability` instead of
    re-running the probe. Probing is one-shot at startup — the spec
    forbids per-refresh re-probing because new sudo grants are picked
    up by restarting ``uxon``, not by polling. When ``None`` and
    ``skeleton=False``, the function probes once.
    """
    from uxon.domain.status import ServerStatus
    from uxon.domain.sudo import SudoCapability
    from uxon.infra.sudo_probe import probe_sudo_capability
    from uxon.tui.context import (  # noqa: PLC0415
        CallbackError,
        TuiContext,
    )

    bridge = TuiBridge(cfg, launch_user, cwd)

    if skeleton:
        # Skeleton ctx skips the per-target probe — it's the fast first
        # frame, and the real probe runs below when the worker calls
        # back with skeleton=False.
        sudo_caps = SudoCapability(reachable_users=frozenset(), can_root=False)
        own: list[SessionInfo] = []
        other: list[SessionInfo] = []
        skipped_users: tuple[str, ...] = ()
    else:
        from uxon.infra import demo as _uxon_demo_ctx  # noqa: PLC0415

        _demo_dir = _uxon_demo_ctx.demo_hosts_dir()
        if _demo_dir is not None:
            # Single demo seam for local sessions: pull the agent-user
            # scope and per-user records straight from _local.json.
            # Bypasses sudo probe + tmux.tmux_socket_path (the production
            # collectors reject synthetic users that don't exist as
            # OS accounts) and keeps every demo-only branch inside this
            # one block.
            scope_users = _uxon_demo_ctx.load_demo_local_scope_users(_demo_dir)
            own = _uxon_demo_ctx.load_demo_local_sessions(_demo_dir, launch_user)
            other = []
            for _u in scope_users:
                if _u == launch_user:
                    continue
                other.extend(_uxon_demo_ctx.load_demo_local_sessions(_demo_dir, _u))
            sudo_caps = SudoCapability(
                reachable_users=frozenset(u for u in scope_users if u != launch_user),
                can_root=False,
            )
            skipped_users = ()
        else:
            # One-shot probe: the candidate set is ``session_users \ {self}``.
            # Self is filtered before probing because ``sudo -niu <self>``
            # trivially succeeds and would inflate ``reachable_users``
            # with a meaningless entry.
            candidates = [
                u for u in identity.resolve_all_session_users(cfg, launch_user) if u != launch_user
            ]
            if sudo_caps_override is not None:
                sudo_caps = sudo_caps_override
            else:
                sudo_caps = probe_sudo_capability(candidates)
            own = sessions_probe.collect_sessions([launch_user], cfg)

            # Other-user sessions are scoped to the *reachable* subset.
            # Unreachable candidates are surfaced separately so the TUI
            # can show the "(2/4 users reachable)" hint.
            if sudo_caps.reachable_users:
                other = sessions_probe.collect_sessions(sorted(sudo_caps.reachable_users), cfg)
            else:
                other = []
            skipped_users = tuple(
                sorted(u for u in candidates if u not in sudo_caps.reachable_users)
            )
        # Spec line 223: ``list.peek`` fires when the TUI actually
        # enumerates cross-user sessions (gated by ``enable_all_users_list``
        # and ``reachable_users`` being non-empty).  CLI ``uxon list
        # --all-users`` emits its own ``list.peek`` from the list block;
        # the TUI refresh path is the second documented site and was
        # previously silent.
        if cfg.enable_all_users_list and sudo_caps.reachable_users:
            from uxon.infra import audit as _audit

            _audit.audit(
                "list.peek",
                scope_users=sorted({launch_user, *sudo_caps.reachable_users}),
                scope_skipped=list(skipped_users),
            )

        own.sort(key=lambda s: s.name)
        other.sort(key=lambda s: (s.user, s.name))

    bridge.sudo_caps = sudo_caps
    # Subtlety: a *skeleton* ctx has empty placeholder caps, not real
    # ones. If we captured those, the first real load would reuse the
    # empty placeholder and never probe. So skeleton's on_refresh passes
    # None, which forces the probe on the first non-skeleton load. Every
    # refresh after that reuses the captured real caps.
    bridge.captured_sudo_caps = None if skeleton else sudo_caps

    tui_own = [_to_tui_session(s, cfg.session_prefix, cfg.legacy_session_prefixes) for s in own]
    tui_other = [_to_tui_session(s, cfg.session_prefix, cfg.legacy_session_prefixes) for s in other]

    total_cpu = format_cpu_pct(sum(s.cpu_pct for s in own) + sum(s.cpu_pct for s in other))
    total_ram = format_rss_kib(sum(s.rss_kib for s in own) + sum(s.rss_kib for s in other))

    home = os.path.expanduser("~")
    cwd_short = cwd.replace(home, "~") if cwd.startswith(home) else cwd

    # Wrap all callbacks so failures surface on the TUI status line instead of
    # killing uxon silently (blessed's fullscreen context hides stderr + tracebacks).
    _CbErr = CallbackError
    on_attach = _wrap_tui_callback(bridge.on_attach, _CbErr)
    on_kill = _wrap_tui_callback(bridge.on_kill, _CbErr)
    on_kill_all = _wrap_tui_callback(bridge.on_kill_all, _CbErr)
    on_kill_all_global = _wrap_tui_callback(bridge.on_kill_all_global, _CbErr)
    on_remote_kill = _wrap_tui_callback(bridge.on_remote_kill, _CbErr)
    # on_remote_attach already raises CallbackError directly (see
    # _build_on_remote_attach_callback) — no _wrap_tui_callback shim
    # needed.
    on_remote_attach = _build_on_remote_attach_callback(cfg)
    on_refresh = _wrap_tui_callback(bridge.on_refresh, _CbErr)
    on_probe_link_health = _wrap_tui_callback(bridge.on_probe_link_health, _CbErr)
    on_probe_cwd_writable = _wrap_tui_callback(bridge.on_probe_cwd_writable, _CbErr)
    on_probe_dir_launchable = _wrap_tui_callback(bridge.on_probe_dir_launchable, _CbErr)
    on_launch_cwd = _wrap_tui_callback(bridge.on_launch_cwd, _CbErr)
    on_launch_new = _wrap_tui_callback(bridge.on_launch_new, _CbErr)
    on_launch_existing = _wrap_tui_callback(bridge.on_launch_existing, _CbErr)
    on_probe_existing_sessions = _wrap_tui_callback(bridge.on_probe_existing_sessions, _CbErr)
    on_probe_worktrees = _wrap_tui_callback(bridge.on_probe_worktrees, _CbErr)
    on_create_worktree = _wrap_tui_callback(bridge.on_create_worktree, _CbErr)
    on_launch_existing_worktree = _wrap_tui_callback(bridge.on_launch_existing_worktree, _CbErr)
    on_probe_existing_worktree_sessions = _wrap_tui_callback(
        bridge.on_probe_existing_worktree_sessions, _CbErr
    )
    get_settings_entries = _wrap_tui_callback(bridge.get_settings_entries, _CbErr)
    on_setting_save = _wrap_tui_callback(bridge.on_setting_save, _CbErr)
    on_setting_remove = _wrap_tui_callback(bridge.on_setting_remove, _CbErr)
    on_setting_save_mapping = _wrap_tui_callback(bridge.on_setting_save_mapping, _CbErr)
    get_git_remote_profile_rows = _wrap_tui_callback(bridge.get_git_remote_profile_rows, _CbErr)

    git_profile_options = [
        (
            p.name,
            f"{p.host}/{p.owner}  via {p.creds_user or launch_user} [{p.auth}]",
        )
        for p in cfg.git_remote_profiles
    ]

    # Reflects whether the "new session in current folder" row should be
    # enabled — same predicate the click handler will apply, so the row
    # state never lies. Same-user fast path runs synchronously (os.access
    # under the hood); cross-user case leaves the value None so the TUI
    # ships the first frame fast and an app worker probes via sudo
    # without blocking the event loop.
    if identity.process_user() == launch_user:
        cwd_writable: bool | None = launch_app.is_launch_target_allowed(cfg, launch_user, cwd)
    else:
        cwd_writable = None

    from uxon.infra import agents as _uxon_agents

    agent_availability = {
        aid: _uxon_agents.AgentAvailability(status="pending") for aid in cfg.enabled_agents
    }

    if skeleton:
        existing_projects: list[tuple[str, str]] = []
        server_status = ServerStatus()
    else:
        existing_projects = _list_existing_projects(cfg.new_project_root)
        server_status = host_status_probe.read_server_status(cfg.new_project_root)

    # Pluggable refresh sources. PR1 ships a single source that wraps
    # ``on_refresh()`` so the existing kick-refresh path runs through the
    # registry — same wall behaviour, but now extensible. PR3 adds one
    # source per configured remote host alongside this one.
    #
    # The skeleton ctx still gets the full source list. SourceSpec
    # construction is pure (just stores names + lambdas), no I/O, so
    # there is no cost to wiring it on the fast-path. The ctx is what
    # ``MainScreen.on_mount`` reads to fan out the initial refresh —
    # an empty list there means the "Loading sessions…" placeholder
    # never gets replaced.
    from uxon.domain.host_breaker import BreakerSpec, HostBreaker
    from uxon.domain.wire_schema import RemoteSnapshot
    from uxon.infra.remote.cache import read_cached_snapshot
    from uxon.infra.remote.collector import fetch_remote_snapshot
    from uxon.tui.refresh import SourceSpec

    # ``main_ctx_rebuild`` returns a fresh ``TuiContext``. The app's
    # source-result handler routes this into ``apply_loaded_ctx``,
    # which is the same swap-or-recompose path the legacy
    # ``_MainCtxLoaded`` message used.
    # The lambda captures ``on_refresh`` by name; by the time the
    # registry runs the fetch on a worker thread, ``on_refresh`` has
    # already been replaced (a few lines above) by its
    # ``_wrap_tui_callback`` shim. So a SystemExit / ``fail()`` from
    # inside the rebuild surfaces as ``CallbackError``, which
    # ``run_source`` captures into ``SourceResult.error`` for
    # fail-soft delivery.
    refresh_sources: list = [
        SourceSpec(
            name="main_ctx_rebuild",
            fetch=lambda: on_refresh(),
            cadence_seconds_attr="tui_refresh_interval_seconds",
            kick_on_mount=True,
        ),
    ]
    # One source per configured remote host. Each runs in its own
    # worker group (``refresh:remote:<name>``) so a slow / dead
    # peer can never stall the local-sessions stream or another
    # peer's poll. Cadence is the dedicated SSH interval — peers
    # are polled less aggressively than the local tmux stream.
    # Fleet-wide fetch-concurrency cap. Without this a 50-host peer
    # set recovering from an outage launches 50 concurrent
    # ``subprocess.Popen`` calls (each holding ≥3 pipe FDs), which
    # saturates the default 1024-FD ulimit before scheduling becomes
    # the bottleneck. Scope is per-``TuiContext`` instance — matches
    # the spec's "no worker survives App teardown" contract.
    fetch_sem = threading.Semaphore(cfg.fetch_concurrency)
    bridge._fetch_sem = fetch_sem

    # Per-host circuit breaker. One :class:`HostBreaker` per peer,
    # keyed by host name and captured by ``_make_remote_fetch``. The
    # breaker decides whether an SSH attempt fires; when open, the
    # fetcher short-circuits to a cache-only snapshot so the UI keeps
    # rendering the last good payload without the cost of yet another
    # doomed connect. ``BreakerSpec`` defaults are intentional — no
    # new config knobs in this commit. Wiring a per-host override is a
    # follow-up.
    host_breakers: dict[str, HostBreaker] = bridge._host_breakers

    def _make_remote_fetch(h, sem, multiplex, persist_seconds, breaker):
        def _fetch():
            # Breaker is the first gate: if it says "do not attempt",
            # skip the SSH layer entirely. We still produce a
            # ``RemoteSnapshot`` so the UI sees something — load the
            # last good cache if we have one; otherwise an empty
            # error snapshot. Either way the cadence-driven retry
            # path will get its next chance the moment the breaker
            # half-opens.
            if not breaker.should_attempt():
                cached = read_cached_snapshot(h.name)
                if cached is not None:
                    return cached
                return RemoteSnapshot(
                    host_name=h.name,
                    fetched_at_epoch=0.0,
                    from_cache=False,
                    error="circuit breaker open",
                    sessions=[],
                    cached_at_epoch=None,
                )
            breaker.mark_inflight()
            try:
                sem.acquire()
                try:
                    snap = fetch_remote_snapshot(
                        h,
                        ssh_multiplex=multiplex,
                        ssh_control_persist_seconds=persist_seconds,
                    )
                finally:
                    sem.release()
                # Translate the snapshot's success/failure into the
                # breaker's outcome reporting. A cache-fallback
                # snapshot (``from_cache=True``, ``error=<live>``)
                # means the live fetch did not succeed — count as
                # failure. ``error is None and not from_cache`` is a
                # real live success.
                if snap.error is None and not snap.from_cache:
                    breaker.on_success()
                else:
                    breaker.on_failure()
                return snap
            finally:
                # Defence in depth: if ``fetch_remote_snapshot`` ever
                # propagates (KeyboardInterrupt mid-tick, a test mock
                # that raises), the breaker must NOT stay in_flight=True
                # forever — that would permanently block the host's
                # next probe. ``on_success`` / ``on_failure`` already
                # clear the gate as a safety net, but the contract is
                # this finally.
                breaker.clear_inflight()

        return _fetch

    for host in cfg.remote_hosts:
        host_breakers[host.name] = HostBreaker(BreakerSpec())
        # Per-host cadence: ``host.interval`` (if set) wins over the
        # fleet-global ``tui_ssh_refresh_interval_seconds``. We pass
        # cadence_seconds_attr=None so the timer reads the explicit
        # value and does not fall through to the legacy attribute path.
        host_cadence = (
            float(host.interval)
            if host.interval is not None
            else float(cfg.tui_ssh_refresh_interval_seconds)
        )
        refresh_sources.append(
            SourceSpec(
                name=f"remote:{host.name}",
                fetch=_make_remote_fetch(
                    host,
                    fetch_sem,
                    cfg.ssh_multiplex,
                    cfg.ssh_control_persist_seconds,
                    host_breakers[host.name],
                ),
                cadence_seconds_attr=None,
                cadence_seconds=host_cadence,
                kick_on_mount=True,
            )
        )

    # Local /proc snapshot for the HostStatusBar's locals bucket. Skip
    # on the skeleton tick (first frame must paint immediately); on the
    # real refresh tick treat probe failure as "pending…" — the
    # selector renders the absence rather than the error.
    host_stats: Any = None
    if not skeleton:
        try:
            from uxon.infra.probes import read_host_stats

            host_stats = read_host_stats()
        except Exception:  # pragma: no cover — defensive
            host_stats = None

    return TuiContext(
        sessions=tui_own,
        total_cpu=total_cpu,
        total_ram=total_ram,
        version=version_probe.format_version(),
        cwd=cwd,
        cwd_short=cwd_short,
        new_project_root=cfg.new_project_root,
        existing_projects=existing_projects,
        server_status=server_status,
        loading=skeleton,
        host_stats=host_stats,
        tui_refresh_interval_seconds=cfg.tui_refresh_interval_seconds,
        tui_ssh_refresh_interval_seconds=cfg.tui_ssh_refresh_interval_seconds,
        ssh_multiplex=cfg.ssh_multiplex,
        ssh_control_persist_seconds=cfg.ssh_control_persist_seconds,
        fetch_concurrency=cfg.fetch_concurrency,
        cwd_writable=cwd_writable,
        current_user=launch_user,
        sudo_caps=sudo_caps,
        scope_skipped_users=skipped_users,
        other_sessions=tui_other,
        enabled_agents=cfg.enabled_agents,
        default_agent=cfg.default_agent,
        launch_user=launch_user,
        agent_availability=agent_availability,
        on_attach=on_attach,
        on_kill=on_kill,
        on_kill_all=on_kill_all,
        on_kill_all_global=on_kill_all_global,
        on_remote_kill=on_remote_kill,
        on_remote_attach=on_remote_attach,
        on_refresh=on_refresh,
        on_probe_link_health=on_probe_link_health,
        on_probe_cwd_writable=on_probe_cwd_writable,
        on_probe_dir_launchable=on_probe_dir_launchable,
        on_launch_cwd=on_launch_cwd,
        on_launch_new=on_launch_new,
        on_launch_existing=on_launch_existing,
        on_probe_existing_sessions=on_probe_existing_sessions,
        on_probe_worktrees=on_probe_worktrees,
        on_create_worktree=on_create_worktree,
        on_launch_existing_worktree=on_launch_existing_worktree,
        on_probe_existing_worktree_sessions=on_probe_existing_worktree_sessions,
        get_settings_entries=get_settings_entries,
        on_setting_save=on_setting_save,
        on_setting_remove=on_setting_remove,
        on_setting_save_mapping=on_setting_save_mapping,
        get_git_remote_profile_rows=get_git_remote_profile_rows,
        git_create_enabled=cfg.git_create_enabled,
        default_git_remote_profile=cfg.default_git_remote_profile,
        git_remote_profile_options=git_profile_options,
        refresh_sources=refresh_sources,
        remote_hosts=list(cfg.remote_hosts),
        tui_table_columns=cfg.tui_table_columns,
        tui_table_default_view=cfg.tui_table_default_view,
        tui_search_fields=cfg.tui_search_fields,
        tui_color_palette=cfg.tui_color_palette,
        local_host_color=cfg.local_host_color,
    )

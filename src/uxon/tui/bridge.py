# SPDX-License-Identifier: MIT
"""TUI bridge: the live callback set wired into a TuiContext.

:class:`TuiBridge` replaces the former ``cli._build_tui_context`` god-closure.
The stable inputs (``cfg`` / ``launch_user`` / ``cwd``) are instance attributes;
every former nested ``on_*`` closure is now a method that delegates to the
``app.*`` use-cases and ``infra.*`` adapters. Per-build state (the probed
``sudo_caps``, the fleet fetch semaphore, the per-host circuit breakers) is
constructed in :func:`uxon.tui.context_builder.build_tui_context` and stashed on
the bridge instance — a fresh bridge is built on every refresh, so this state
never outlives one ``TuiContext``.

This module lives in ``tui/`` and may import ``domain`` / ``infra`` / ``app``;
it must NOT import ``uxon.cli`` (the composition root imports *this*, lazily).
"""

from __future__ import annotations

import os
import shlex
import subprocess
import threading
from typing import TYPE_CHECKING, Any

import uxon.app.attach as attach_app
import uxon.app.launch as launch_app
import uxon.app.tui_planning as tui_planning
from uxon.domain.authz import canonical
from uxon.domain.config import Config
from uxon.domain.session import session_stem_for_worktree
from uxon.errors import fail
from uxon.infra import (
    config_loader,
    git,
    host_status_probe,
    identity,
    process,
    sessions_probe,
    tmux,
)

if TYPE_CHECKING:
    from uxon.domain.launch_request import LaunchRequest
    from uxon.domain.sudo import SudoCapability
    from uxon.tui.context import TuiContext


class TuiBridge:
    """Holds the stable TUI inputs and the command callbacks.

    Every ``on_*`` method delegates to the ``app.*`` use-cases and
    ``infra.*`` adapters. Per-build state — the probed ``sudo_caps``, the
    fleet ``fetch`` semaphore, and the per-host circuit breakers — is
    populated by :func:`uxon.tui.context_builder.build_tui_context` before
    the context is wired, and lives only for the lifetime of one
    ``TuiContext`` (a fresh bridge is built on every refresh).
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

    def on_remote_attach(self, host_name: str, user: str, name: str) -> LaunchRequest:
        """TUI dispatch: attach to ``name`` belonging to ``user`` on peer ``host_name``.

        Mirrors :meth:`on_remote_kill`'s SSH gesture but allocates a TTY
        and forces ``ssh_multiplex="off"`` — an interactive attach must
        not share the poller's ControlMaster (a wedged master would hang
        the TUI handoff at ``unix_wait_for_peer``). Raises
        :class:`CallbackError` directly (no ``_wrap_tui_callback`` shim)
        on an unknown host.
        """
        import uuid as _uuid

        from uxon.domain.launch_request import LaunchRequest
        from uxon.infra import audit as _audit
        from uxon.infra.remote.collector import DEFAULT_CONNECT_TIMEOUT_SEC
        from uxon.infra.remote.ssh_argv import build_peer_ssh_argv
        from uxon.infra.remote_hosts import find_host
        from uxon.tui.context import CallbackError

        peer = find_host(self.cfg.remote_hosts, host_name)
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
            ssh_control_persist_seconds=self.cfg.ssh_control_persist_seconds,
        )
        return LaunchRequest(cmd=tuple(argv), label=f"attach {name}@{host_name}")

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
        from uxon.tui.context_builder import build_tui_context

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
        from uxon.domain.config import DEFAULT_CONFIG
        from uxon.infra import settings as uxon_settings

        repo_data, proj_data, proj_cfg = uxon_settings.load_settings_sources(self.cwd)
        return uxon_settings.resolve_setting_entries(
            repo_data, proj_data, proj_cfg, DEFAULT_CONFIG, agent_ids=tuple(self.cfg.agents)
        )

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

    def on_launch_cwd(self, agent_id: str, mode_id: str, target_dir: str | None = None):
        # ``target_dir`` is the resolved primary ``repo_root`` for the
        # WORKSPACE primary row. It coincides with ``self.cwd`` when the TUI
        # was started at the primary repo root, and differs when it was
        # started anywhere else — a linked worktree OR a subdirectory of the
        # repo — so the primary row always launches into the primary tree
        # (matching its ``(primary)`` label), not wherever the TUI was opened.
        # ``None`` (the non-git / "launch in this folder" path) keeps
        # ``self.cwd`` unchanged. The plain path-based stem
        # (``session_stem_for_path``) is correct for the primary tree, so no
        # ``worktree=`` argument is threaded here.
        target = target_dir or self.cwd
        req = tui_planning._plan_tui_run_agent(
            self.cfg, self.launch_user, target, agent_id, mode_id
        )
        # ``_plan_tui_run_agent`` only ever yields a launch (never an
        # attach), so this path is unconditional ``session.new``.
        from uxon.infra import audit as _audit

        _audit.audit(
            "session.new",
            agent=agent_id,
            project=target,
            branch="",
            session=attach_app._session_name_from_launch_label(req.label),
            dry_run=False,
        )
        return req

    def on_launch_new(self, name: str, agent_id: str, mode_id: str, git_profile: str):
        req = tui_planning._plan_tui_create_new_agent(
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
        req = tui_planning._plan_tui_open_existing_agent(
            self.cfg, self.launch_user, name, agent_id, mode_id
        )
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
        """Workspaces for ``cwd_arg``'s repo (folders only).

        Resolves ``cwd`` → primary repo root with the NON-interactive
        resolvers (Task 5) so the fullscreen TUI never blocks on a hidden
        ``sudo`` prompt, then lists worktrees under the same
        ``identity.nonint_command_prefix_for_user`` and parses with Task 2.

        Two empty-ish outcomes are kept distinct so the WORKSPACE column can
        tell them apart: a folder that is **not a git repo** returns ``[]``
        (the benign "git not initialized" hint), whereas a folder that **is**
        a repo but whose ``git worktree list`` enumeration fails raises
        :class:`WorktreeProbeError` (an error row) — a real git failure must
        not masquerade as "no repo here".
        """
        from uxon.infra.worktrees import WorktreeProbeError, parse_worktree_porcelain

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
            raise WorktreeProbeError((cp.stderr or "").strip() or "git worktree list failed")
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
        req = tui_planning._plan_tui_run_agent(
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

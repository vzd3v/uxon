# SPDX-License-Identifier: MIT
"""TUI bridge: the live callback set wired into a TuiContext.

:class:`TuiBridge` replaces the former ``cli._build_tui_context`` god-closure.
The stable inputs (``cfg`` / ``caller_user`` / ``launch_user`` / ``cwd``) are
instance attributes; every former nested ``on_*`` closure is now a method that delegates to the
``app.*`` use-cases and ``infra.*`` adapters. Per-build state (the probed
``sudo_caps``) is
constructed in :func:`uxon.tui.context_builder.build_tui_context` and stashed on
the bridge instance — a fresh bridge is built on every refresh, so this state
never outlives one ``TuiContext``. The fleet semaphore is passed through
refreshes; retry admission is owned by the App's worker coordinator.

This module lives in ``tui/`` and may import ``domain`` / ``infra`` / ``app``;
it must NOT import ``uxon.cli`` (the composition root imports *this*, lazily).
"""

from __future__ import annotations

import os
import shlex
import threading
from typing import TYPE_CHECKING

import uxon.app.launch as launch_app
import uxon.app.launch_profile as launch_profile_app
import uxon.app.project_browsing as project_browsing
import uxon.app.tui_planning as tui_planning
from uxon.domain.config import Config
from uxon.domain.session import (
    BulkKillResult,
    KillResult,
    SessionIdentity,
    session_stem_for_worktree,
)
from uxon.errors import fail
from uxon.infra import (
    config_loader,
    git,
    host_status_probe,
    identity,
    sessions_probe,
    tmux,
)
from uxon.infra.run import run_query

if TYPE_CHECKING:
    from uxon.domain.launch_request import LaunchRequest
    from uxon.domain.sudo import SudoCapability
    from uxon.tui.context import TuiContext


class TuiBridge:
    """Holds the stable TUI inputs and the command callbacks.

    Every ``on_*`` method delegates to the ``app.*`` use-cases and
    ``infra.*`` adapters. Per-build state — the probed ``sudo_caps`` — is
    populated by :func:`uxon.tui.context_builder.build_tui_context` before
    the context is wired, and lives only for the lifetime of one
    ``TuiContext`` (a fresh bridge is built on every refresh).
    The fleet fetch semaphore is shared across those refreshes.
    """

    def __init__(self, cfg: Config, caller_user: str, launch_user: str, cwd: str) -> None:
        self.cfg = cfg
        self.caller_user = caller_user
        self.launch_user = launch_user
        self.cwd = cwd
        # Per-build state, set by build_tui_context. Empty placeholders
        # for the skeleton tick; real values once a non-skeleton build runs.
        self.sudo_caps: SudoCapability | None = None
        # Captured caps for on_refresh reuse: None on a skeleton ctx so the
        # first real load probes; the probed caps thereafter (one-shot).
        self.captured_sudo_caps: SudoCapability | None = None
        self._fetch_sem: threading.Semaphore | None = None

    # ── attach / kill ──

    def on_attach(self, user: str, name: str, expected: SessionIdentity | None = None):
        # TUI Enter on a local row dispatches a direct
        # ``tmux attach-session`` (no ``uxon`` wrapper) — emit
        # ``session.attach.dispatch`` here so the handoff is auditable.
        # ``do_attach``'s dispatch emit only covers the CLI-side ``uxon attach``
        # invocation; the TUI request bypasses that path entirely.
        from uxon.app.kill import _target_for_identity
        from uxon.infra import audit as _audit

        if expected is not None and expected.user != user:
            fail("selected session user differs from attach user", 1)

        fresh = sessions_probe.collect_sessions([user], self.cfg) if expected is None else []
        target = (
            _target_for_identity(self.cfg, expected, require_nonce=False)
            if expected is not None
            else sessions_probe._resolve_or_audit_not_found(
                name,
                fresh,
                self.cfg,
                audit_event="session.attach.dispatch",
                target_user=user,
                extra={"profile": "", "agent": ""},
            )
        )
        _audit.audit(
            "session.attach.dispatch",
            session=target.name,
            session_id=target.tmux_session_id,
            launch_nonce=target.launch_nonce,
            execution_backend=target.execution_backend,
            target_user=user,
            profile=target.profile,
            agent=target.agent,
            dry_run=False,
        )
        return tmux._build_tmux_attach_request(target, self.cfg, user)

    def on_kill(self, expected: SessionIdentity) -> KillResult:
        from uxon.app.kill import kill_expected

        allowed = {self.launch_user, *(self.sudo_caps.reachable_users if self.sudo_caps else ())}
        return kill_expected(self.cfg, expected, allowed_users=frozenset(allowed))

    def on_kill_all(self, targets: tuple[SessionIdentity, ...]) -> BulkKillResult:
        from uxon.app.kill import kill_snapshot

        return kill_snapshot(self.cfg, targets, allowed_users=frozenset({self.launch_user}))

    def on_remote_attach(
        self, host_name: str, user: str, name: str, expected: SessionIdentity | None = None
    ) -> LaunchRequest:
        """TUI dispatch: attach to ``name`` belonging to ``user`` on peer ``host_name``.

        Mirrors :meth:`on_remote_kill`'s SSH gesture but allocates a TTY
        and forces ``ssh_multiplex="off"`` — an interactive attach must
        not share the poller's ControlMaster (a wedged master would hang
        the TUI handoff at ``unix_wait_for_peer``). Raises
        :class:`CallbackError` on an unknown host; the bound callback also
        converts lower-layer failures into the same structured error.
        """
        import uuid as _uuid

        from uxon.domain.launch_request import LaunchRequest
        from uxon.infra import audit as _audit
        from uxon.infra.remote.collector import DEFAULT_CONNECT_TIMEOUT_SEC
        from uxon.infra.remote.ssh_argv import build_peer_ssh_argv
        from uxon.infra.remote_hosts import find_host
        from uxon.tui.context import CallbackError

        corr_id = str(_uuid.uuid4())
        peer = find_host(self.cfg.remote_hosts, host_name)
        if peer is None:
            _audit.audit(
                "attach.remote.out.dispatch",
                outcome="not_found",
                peer_name=host_name,
                ssh_alias="",
                target_user=user,
                target_session=name,
                dry_run=False,
                correlation_id=corr_id,
            )
            raise CallbackError(f"unknown remote host: {host_name}")
        from uxon.app.kill import resolve_remote_identity

        expected = expected or resolve_remote_identity(self.cfg, peer, name, user)
        expected.validate(require_nonce=False)
        if expected.user != user:
            fail("selected session user differs from attach user", 1)
        name = expected.name
        # target first (see _do_attach_remote for rationale).
        remote_cmd = (
            f"{shlex.quote(peer.remote_uxon)} attach {shlex.quote(name)} "
            f"--user {shlex.quote(user)} "
            f"--expect-session {shlex.quote(expected.to_json())} "
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
        # Pass correlation_id explicitly rather than seeding the process-global
        # value: the TUI outlives this dispatch. Emit only after argv planning
        # succeeds so outcome=ok means a concrete handoff request exists.
        _audit.audit(
            "attach.remote.out.dispatch",
            peer_name=peer.name,
            ssh_alias=peer.ssh_alias,
            target_user=user,
            target_session=name,
            session_id=expected.session_id,
            launch_nonce=expected.launch_nonce,
            execution_backend=expected.execution_backend,
            dry_run=False,
            correlation_id=corr_id,
        )
        return LaunchRequest(
            cmd=tuple(argv),
            label=f"attach {name}@{host_name}",
            identity=expected,
            correlation_id=corr_id,
            peer_name=host_name,
            capture_ssh_diagnostics=not peer.command_template,
        )

    def on_remote_kill(self, host_name: str, expected: SessionIdentity) -> KillResult:
        from uxon.app.kill import remote_kill_expected

        return remote_kill_expected(self.cfg, host_name, expected)

    def on_kill_all_reachable(self, targets: tuple[SessionIdentity, ...]) -> BulkKillResult:
        from uxon.app.kill import kill_snapshot

        allowed = {self.launch_user, *(self.sudo_caps.reachable_users if self.sudo_caps else ())}
        return kill_snapshot(self.cfg, targets, allowed_users=frozenset(allowed))

    # ── refresh / link health ──

    def on_refresh(self) -> TuiContext:
        # Re-read config so settings edits take effect immediately.
        # Always returns a fully loaded ctx (skeleton=False) — even when
        # the calling ctx was a skeleton, the caller wants real data.
        # We pass the captured caps (or None on the very first load)
        # so the probe runs at most once per process.
        from uxon.tui.context_builder import build_tui_context

        fresh_cfg = config_loader.load_config()
        return build_tui_context(
            fresh_cfg,
            self.caller_user,
            self.launch_user,
            self.cwd,
            sudo_caps_override=self.captured_sudo_caps,
            fetch_semaphore=self._fetch_sem,
        )

    def on_probe_link_health(self) -> object | None:
        return host_status_probe.read_ssh_link_health_status()

    # ── settings (superuser-only; safe to wire unconditionally) ──

    def get_settings_entries(self) -> list:
        from uxon.domain.config import DEFAULT_CONFIG
        from uxon.infra import settings as uxon_settings

        operator_data = uxon_settings.load_settings_source()
        return uxon_settings.resolve_setting_entries(operator_data, DEFAULT_CONFIG)

    def on_setting_save(self, key: str, value: object) -> None:
        from uxon.infra import settings as uxon_settings

        uxon_settings.persist_operator_config_updates(
            config_loader.operator_config_path(), {key: value}
        )

    def on_setting_remove(self, key: str) -> None:
        from uxon.infra import settings as uxon_settings

        uxon_settings.remove_operator_key(config_loader.operator_config_path(), key)

    def on_setting_save_mapping(self, key: str, mapping: dict) -> None:
        from uxon.infra import settings as uxon_settings

        uxon_settings.persist_operator_config_updates(
            config_loader.operator_config_path(), {key: mapping}
        )

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

    def on_launch_cwd(self, profile_id: str, mode_id: str, target_dir: str | None = None):
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
        req = tui_planning._plan_tui_run_profile(
            self.cfg, self.caller_user, self.launch_user, target, profile_id, mode_id
        )
        return req

    def on_launch_new(self, name: str, profile_id: str, mode_id: str, git_profile: str):
        req = tui_planning._plan_tui_create_new_profile(
            self.cfg, self.caller_user, self.launch_user, name, profile_id, mode_id, git_profile
        )
        return req

    def on_launch_existing(self, name: str, profile_id: str, mode_id: str):
        req = tui_planning._plan_tui_open_existing_profile(
            self.cfg, self.caller_user, self.launch_user, name, profile_id, mode_id
        )
        return req

    def on_list_project_directories(self, relative_path: str) -> list[tuple[str, str]]:
        """Browse under the project root using the context's launch user, off-loop."""
        return project_browsing.browse_project_directories(
            self.cfg, self.launch_user, relative_path
        )

    def on_probe_existing_sessions(
        self, target_dir: str, profile_id: str, mode_id: str
    ) -> tuple[tuple[str, str, bool, SessionIdentity], ...]:
        """TUI probe: return (name, attached) pairs for the profile's
        compatible sessions under ``target_dir``.

        Called by the TUI between LaunchOptionsScreen (agent + mode pick)
        and the actual ``on_launch_*`` commit. An empty tuple means the
        TUI proceeds straight to launch; otherwise it pushes the
        SessionChoiceScreen modal to let the operator pick attach vs
        new-alongside.
        """
        resolved = launch_profile_app.resolve_launch_profile(
            self.cfg,
            self.caller_user,
            profile_id,
            target_dir,
            mode_id,
            target_may_not_exist=False,
        )
        matches = sessions_probe.probe_tui_compatible_sessions(
            self.cfg, resolved.launch_user, resolved.canonical_target, resolved.profile.id
        )
        return tuple((s.user, s.name, s.attached == "1", s.identity) for s in matches)

    def on_git_remote_options(
        self, name: str, profile_id: str, mode_id: str
    ) -> tuple[list[tuple[str, str]], str]:
        """Effective git-remote choices for a new project/profile pair.

        Runs off the event loop from ``LaunchFlow`` after the operator picks a
        launch profile. Resolution applies path rules before the GitProfile
        screen is shown, so the modal never offers a remote that the later
        planner would reject.
        """
        target = os.path.normpath(os.path.join(self.cfg.new_project_root, name))
        resolved = launch_profile_app.resolve_launch_profile(
            self.cfg,
            self.caller_user,
            profile_id,
            target,
            mode_id,
            target_may_not_exist=True,
        )
        allowed = set(resolved.git_remote.allowed_profiles)
        options = [
            (
                p.name,
                f"{p.host}/{p.owner}  via {p.creds_user or resolved.launch_user} [{p.auth}]",
            )
            for p in self.cfg.git_remote_profiles
            if p.name in allowed
        ]
        return options, resolved.git_remote.default_profile

    def on_probe_worktrees(self, cwd_arg: str, profile_id: str, mode_id: str) -> list:
        """Validate the chosen profile's access and list its target-user worktrees.

        A non-git directory returns an empty list. Failed enumeration raises;
        it must not be treated as a non-git directory. Runs off the event loop.
        """
        from uxon.infra.worktrees import WorktreeProbeError, parse_worktree_porcelain

        resolved = launch_profile_app.resolve_launch_profile(
            self.cfg,
            self.caller_user,
            profile_id,
            cwd_arg,
            mode_id,
            target_may_not_exist=False,
        )
        user = resolved.launch_user
        cwd_arg = resolved.canonical_target
        launch_app.ensure_launch_target_allowed(self.cfg, user, cwd_arg)
        repo_root = git.git_repo_root_nonint_as_user(self.cfg, cwd_arg, user)
        if not repo_root:
            return []
        primary = git.git_common_dir_root_as_user(self.cfg, cwd_arg, user)
        if primary:
            repo_root = primary
        cp = run_query(
            identity.nonint_command_prefix_for_user(self.cfg, user)
            + ["git", "-C", repo_root, "worktree", "list", "--porcelain"],
            timeout=self.cfg.execution.backend_for_user(user).probe_timeout_seconds,
        )
        if cp.returncode != 0:
            raise WorktreeProbeError((cp.stderr or "").strip() or "git worktree list failed")
        return parse_worktree_porcelain(cp.stdout or "", repo_root=repo_root)

    def on_create_worktree(self, repo_root: str, branch: str, profile_id: str, mode_id: str):
        # plan_worktree_launch emits its own worktree.create + session.new
        # audit events. The TUI has no agent passthrough args (agent_args
        # defaults to None).
        from uxon.infra.worktrees import compute_worktree_path

        worktree_path = compute_worktree_path(
            repo_root=repo_root, branch=branch, worktree_root=self.cfg.worktree_root
        )
        resolved = launch_profile_app.resolve_launch_profile(
            self.cfg,
            self.caller_user,
            profile_id,
            worktree_path,
            mode_id,
            target_may_not_exist=True,
        )
        return launch_app.plan_worktree_launch(
            self.cfg,
            self.caller_user,
            resolved,
            repo_root,
            branch,
            requested_profile=profile_id,
        )

    def on_launch_existing_worktree(
        self, repo_root: str, branch: str, worktree_path: str, profile_id: str, mode_id: str
    ):
        # Launch into an EXISTING worktree with the worktree-aware stem
        # — never re-creates the worktree.
        req = tui_planning._plan_tui_run_profile(
            self.cfg,
            self.caller_user,
            self.launch_user,
            worktree_path,
            profile_id,
            mode_id,
            worktree=(repo_root, branch),
        )
        return req

    def on_probe_existing_worktree_sessions(
        self, worktree_path: str, repo_root: str, branch: str, profile_id: str, mode_id: str
    ) -> tuple[tuple[str, str, bool, SessionIdentity], ...]:
        resolved = launch_profile_app.resolve_launch_profile(
            self.cfg,
            self.caller_user,
            profile_id,
            worktree_path,
            mode_id,
            target_may_not_exist=False,
        )
        matches = sessions_probe.probe_tui_compatible_sessions(
            self.cfg,
            resolved.launch_user,
            resolved.canonical_target,
            resolved.profile.id,
            stem=session_stem_for_worktree(repo_root, branch),
            compatibility_root=resolved.canonical_target,
        )
        return tuple((s.user, s.name, s.attached == "1", s.identity) for s in matches)

    def on_runtime_gate(self, target_dir: str, profile_id: str, mode_id: str):
        """Probe the workload runtime for ``target_dir``; return the TUI gate or None.

        Shells out as the launch user under a bounded timeout — MUST run off
        the event loop (the caller dispatches it via ``run_off_loop``). None
        means "launch straight through" (disabled, or already running).
        """
        resolved = launch_profile_app.resolve_launch_profile(
            self.cfg,
            self.caller_user,
            profile_id,
            target_dir,
            mode_id,
            target_may_not_exist=False,
        )
        return launch_app.decide_runtime_gate(self.cfg, resolved.canonical_target, resolved)

    def on_probe_cwd_writable(self) -> bool:
        return launch_app.is_launch_target_allowed(self.cfg, self.launch_user, self.cwd)

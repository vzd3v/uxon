# SPDX-License-Identifier: MIT
"""Interactive tmux handoff shared by the CLI and the TUI's outer runner."""

from __future__ import annotations

import re
import shlex
import subprocess
import time

from uxon.app import kill
from uxon.domain.args import ParsedArgs
from uxon.domain.config import Config
from uxon.domain.launch_profiles import ResolvedLaunchProfile
from uxon.domain.launch_request import HandoffResult, LaunchRequest, TmuxHandoff
from uxon.errors import eprint, fail
from uxon.infra import launch_records, loop_guard, sessions_probe, tmux
from uxon.infra.process import run_cmd
from uxon.infra.tmux_diagnostics import DISMISS_EXIT_CODE


def _dismiss_diagnostics(context: TmuxHandoff) -> None:
    """Revalidate dead-pane requests before deleting any terminal or record.

    Pane options only request dismissal. Workload authority comes from the
    controller's verified launch record, never from tmux's mutable options.
    """
    cfg = context.config
    socket = tmux.tmux_socket_path(cfg, context.user)

    def snapshot(session_id: str | None = None):
        return sessions_probe.collect_session_snapshot_for_user(
            cfg,
            context.user,
            cfg.session_prefix,
            socket,
            legacy_prefixes=cfg.legacy_session_prefixes,
            session_id=session_id,
            for_dismissal=True,
        )

    targets = snapshot().sessions
    base = tmux.configured_tmux_base(cfg, context.user, nonint=True)
    timeout = cfg.execution.backend_for_user(context.user).probe_timeout_seconds
    for target in targets:
        if not target.dismissed_panes:
            continue
        if not target.launch_record_verified:
            if not tmux.exact_session_exists(cfg, context.user, target.tmux_session_id):
                continue
            fail("cannot close diagnostics: the launch identity could not be verified")
        teardown = kill.prepare_runtime_teardown(cfg, target)
        for pane in target.dismissed_panes:
            if re.fullmatch(r"%[0-9]+", pane) is None:
                fail("cannot close diagnostics: invalid tmux pane identity")
            if (
                re.fullmatch(r"\$[0-9]+", target.tmux_session_id) is None
                or re.fullmatch(r"[A-Za-z0-9_-]{16,64}", target.launch_nonce) is None
            ):
                fail("cannot close diagnostics: invalid launch identity")
            # The predicate and mutation run on tmux's command queue. A respawned
            # pane or a different session can never be killed by a stale request.
            condition = (
                "#{&&:#{pane_dead},#{&&:#{==:#{@uxon-dismissed},#{pane_pid}},"
                "#{&&:#{==:#{session_id}," + target.tmux_session_id + "},"
                "#{==:#{E:" + launch_records.LAUNCH_NONCE_ENV + "}," + target.launch_nonce + "}}}}"
            )
            result = run_cmd(
                base + ["if-shell", "-F", "-t", pane, condition, f"kill-pane -t {pane}"],
                check=False,
                timeout=timeout,
            )
            if result.returncode != 0:
                # Missing panes are idempotent only after a fresh bounded snapshot.
                remaining = snapshot(target.tmux_session_id).sessions
                if remaining and pane in remaining[0].dismissed_panes:
                    detail = (result.stderr or result.stdout).strip()
                    fail(f"cannot close diagnostics: {detail or 'tmux refused dismissal'}")
        if tmux.exact_session_exists(cfg, context.user, target.tmux_session_id):
            continue  # live siblings and unread diagnostics still own their record
        if not kill.finish_killed_session(cfg, target, teardown, target_user=context.user):
            fail("diagnostic screen closed, but workload or launch-record removal failed")


def run_launch_request(req: LaunchRequest) -> HandoffResult:
    """Wait for the interactive client; do extra work only on explicit dismissal."""
    started = time.monotonic()
    context = req.handoff
    command = req.cmd
    stage = "prepare"
    try:
        with loop_guard.handoff_spawn():
            if req.managed is not None:
                tmux.prepare_managed_launch(req)
            else:
                for pre in req.prelaunch:
                    rc = subprocess.call(list(pre))
                    if rc != 0:
                        return HandoffResult(rc, "prelaunch", time.monotonic() - started)
                command = tmux.prepare_diagnostics_attach(req)
            stage = "cmd"
            rc = subprocess.call(list(command))
    except SystemExit as exc:
        message = getattr(exc, "uxon_msg", "")
        if not message:
            raise
        code = exc.code if isinstance(exc.code, int) else 1
        return HandoffResult(code, stage, time.monotonic() - started, message)
    except (OSError, subprocess.SubprocessError) as exc:
        eprint(f"uxon: {exc}")
        return HandoffResult(1, stage, time.monotonic() - started, str(exc))
    warning = ""
    if rc == DISMISS_EXIT_CODE and context is not None:
        try:
            _dismiss_diagnostics(context)
        except (OSError, subprocess.SubprocessError, SystemExit) as exc:
            warning = getattr(exc, "uxon_msg", str(exc))
        rc = 0
    return HandoffResult(rc, "cmd", time.monotonic() - started, warning)


def launch_in_tmux(
    target_dir: str,
    session: str,
    args: ParsedArgs,
    cfg: Config,
    branch: str | None,
    *,
    resolved_profile: ResolvedLaunchProfile | None = None,
    server_running: bool = False,
) -> int:
    """Build once, then use the same interactive lifecycle as explicit attach."""
    if resolved_profile is None:
        fail("internal: launch profile must be resolved before launch_in_tmux")
    req = tmux._build_tmux_launch_request(
        target_dir,
        session,
        args,
        cfg,
        branch,
        resolved_profile=resolved_profile,
        server_running=server_running,
    )
    if args.dry_run:
        from uxon.infra import audit as _audit

        _audit.audit(
            "session.new",
            profile=resolved_profile.profile.id,
            agent=resolved_profile.agent.id,
            target_user=resolved_profile.launch_user,
            project=target_dir,
            branch=branch or "",
            session=session,
            dry_run=True,
        )
        print(f"launch_user={shlex.quote(resolved_profile.launch_user)}")
        print(f"dir={shlex.quote(target_dir)}")
        print(f"socket={shlex.quote(tmux.tmux_socket_path(cfg, resolved_profile.launch_user))}")
        for pre in req.prelaunch:
            print(f"socket_parent_prepare={shlex.join(pre)}")
        if req.managed is not None:
            print(f"tmux_create={shlex.join(req.managed.create_cmd)}")
        print(f"session={shlex.quote(session)}")
        if branch:
            print(f"branch={shlex.quote(branch)}")
        print(f"exec {shlex.join(req.cmd)}")
        return 0
    result = run_launch_request(req)
    if result.warning:
        eprint(f"uxon: {result.warning}")
        return 1
    return result.rc

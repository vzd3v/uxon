# SPDX-License-Identifier: MIT
"""Interactive tmux handoff shared by the CLI and the TUI's outer runner."""

from __future__ import annotations

import os
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
from uxon.infra import loop_guard, sessions_probe, tmux
from uxon.infra.process import run_cmd
from uxon.infra.tmux_diagnostics import ACKNOWLEDGED_FORMAT, DISMISS_EXIT_CODE

_TRANSPORT_DIAGNOSTIC_BYTES = 512


def _run_ssh_client(command: tuple[str, ...]) -> tuple[int, str]:
    """Tee OpenSSH diagnostics while the remote PTY stays on inherited stdout.

    A forced remote PTY merges workload output into stdout. Only the separate
    local SSH diagnostic stream is retained, with a bounded tail.
    """
    with subprocess.Popen(list(command), stderr=subprocess.PIPE) as client:
        assert client.stderr is not None
        tail = b""
        try:
            while chunk := os.read(client.stderr.fileno(), 4096):
                tail = (tail + chunk)[-_TRANSPORT_DIAGNOSTIC_BYTES:]
                try:
                    remaining = memoryview(chunk)
                    while remaining:
                        remaining = remaining[os.write(2, remaining) :]
                except OSError:
                    # Continue draining if the terminal closed, so the child
                    # cannot block on a full diagnostic pipe during cleanup.
                    pass
            rc = client.wait()
        except BaseException:
            client.kill()
            client.wait()
            raise
    return rc, tail.decode("utf-8", errors="replace").strip()


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
                "#{&&:#{pane_dead},#{&&:"
                + ACKNOWLEDGED_FORMAT
                + ","
                + tmux.session_identity_predicate(target.identity)
                + "}}"
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
        if tmux.exact_identity_exists(cfg, target.identity):
            continue  # live siblings and unread diagnostics still own their record
        cleanup = kill.finish_killed_session(cfg, target, teardown, target_user=context.user)
        if not cleanup:
            fail(f"diagnostic screen closed, but cleanup failed: {cleanup.error}")


def run_launch_request(req: LaunchRequest) -> HandoffResult:
    """Wait for the interactive client; do extra work only on explicit dismissal."""
    started = time.monotonic()
    context = req.handoff
    command = req.cmd
    stage = "prepare"
    metadata = None
    expected = req.identity
    diagnostic = ""

    def finished(rc: int, stage: str, warning: str = "") -> HandoffResult:
        from uxon.domain.launch_request import session_name_from_launch_label
        from uxon.infra import audit as _audit

        result = HandoffResult(
            rc,
            stage,
            time.monotonic() - started,
            warning,
            expected.session_id if expected else "",
            expected.launch_nonce if expected else req.managed.record_nonce if req.managed else "",
        )
        _audit.audit(
            "session.ended",
            outcome="ok" if rc == 0 and not warning else "error",
            session=expected.name if expected else session_name_from_launch_label(req.label),
            session_id=result.session_id,
            launch_nonce=result.launch_nonce,
            target_user=expected.user
            if expected
            else req.managed.launch_user
            if req.managed
            else "",
            execution_backend=expected.execution_backend
            if expected
            else req.managed.execution_backend
            if req.managed
            else "",
            **({"correlation_id": req.correlation_id} if req.correlation_id else {}),
            **({"peer_name": req.peer_name} if req.peer_name else {}),
            rc=rc,
            stage=stage,
            wall_seconds=round(result.wall_seconds, 3),
            **(
                {"error": warning[:512]}
                if warning
                else {"error": f"terminal client exited with status {rc}"}
                if rc
                else {}
            ),
        )
        return result

    try:
        with loop_guard.handoff_spawn():
            if req.managed is not None:
                metadata = tmux.prepare_managed_launch(req)
                assert metadata is not None
                expected = tmux.managed_session_identity(req.managed, metadata)
                command = tmux.prepared_launch_command(req, expected)
            else:
                for pre in req.prelaunch:
                    rc = subprocess.call(list(pre))
                    if rc != 0:
                        return finished(rc, "prelaunch")
                command = tmux.prepare_diagnostics_attach(req)
            stage = "cmd"
            if req.capture_ssh_diagnostics and req.peer_name and command[0] == "ssh":
                rc, diagnostic = _run_ssh_client(command)
            else:
                # Native tmux and custom transports keep all terminal streams.
                rc = subprocess.call(list(command))
    except SystemExit as exc:
        message = getattr(exc, "uxon_msg", "")
        if not message:
            raise
        code = exc.code if isinstance(exc.code, int) else 1
        return finished(code, stage, message)
    except (OSError, subprocess.SubprocessError) as exc:
        eprint(f"uxon: {exc}")
        return finished(1, stage, str(exc))
    warning = diagnostic if rc else ""
    if rc == DISMISS_EXIT_CODE and context is not None:
        try:
            _dismiss_diagnostics(context)
        except (OSError, subprocess.SubprocessError, SystemExit) as exc:
            warning = getattr(exc, "uxon_msg", str(exc))
        rc = 0
    if rc != 0 and expected is not None and context is not None:
        try:
            if not tmux.exact_identity_exists(context.config, expected, require_nonce=False):
                warning = "selected session identity changed or disappeared; attach refused"
        except (OSError, subprocess.SubprocessError, SystemExit) as exc:
            warning = getattr(exc, "uxon_msg", str(exc))
    return finished(rc, "cmd", warning)


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

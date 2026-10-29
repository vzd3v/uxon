# SPDX-License-Identifier: MIT
"""``uxon attach`` use-case: local, cross-user, and remote session attach."""

from __future__ import annotations

import shlex

from uxon.app.session_handoff import run_launch_request
from uxon.domain.args import ParsedArgs
from uxon.domain.config import Config
from uxon.domain.launch_request import LaunchRequest
from uxon.domain.session import SessionInfo
from uxon.errors import eprint, fail
from uxon.infra import sessions_probe, tmux


def _do_attach_remote(args: ParsedArgs, cfg: Config) -> int:
    """Handle ``uxon attach <id> --host <alias> --user <u>``.

    Looks up the configured peer, builds an interactive ssh argv via
    :func:`build_peer_ssh_argv`, and waits for it through the shared handoff. Peer's own
    ``uxon attach --user`` runs the target execution probe, so the
    local side does not need to know the peer's user table.

    The wire command always passes ``--user`` (even when it equals
    the ssh-login-user on the peer): peer is the sole authority on
    'who can attach to what', and we route that decision through
    its own gating. ``--user`` was made required at parse time
    (:func:`_parse_attach_extras`).
    """
    import uuid as _uuid

    from uxon.infra import audit as _audit
    from uxon.infra.remote.collector import DEFAULT_CONNECT_TIMEOUT_SEC
    from uxon.infra.remote.ssh_argv import build_peer_ssh_argv
    from uxon.infra.remote_hosts import find_host

    corr_id = str(_uuid.uuid4())
    peer = find_host(cfg.remote_hosts, args.host or "")
    if peer is None:
        _audit.audit(
            "attach.remote.out.dispatch",
            outcome="not_found",
            peer_name=args.host or "",
            ssh_alias="",
            target_user=args.user,
            target_session=args.target_id,
            dry_run=args.dry_run,
            correlation_id=corr_id,
        )
        names = ", ".join(h.name for h in cfg.remote_hosts) or "(none)"
        fail(f"unknown --host {args.host!r}; configured: {names}")
    assert args.user is not None  # parser-enforced
    from uxon.app.kill import resolve_remote_identity

    expected = args.expected_session or resolve_remote_identity(
        cfg, peer, args.target_id or "", args.user
    )
    expected.validate(require_nonce=False)
    if expected.user != args.user:
        fail("--expect-session user differs from --user", 1)
    # ``target_id`` MUST come first after the verb: peer-side
    # ``parse_subcommand`` reads ``argv[1]`` as the target, with flags
    # tail-parsed afterwards.  Putting flags first makes the peer parse
    # the flag name as the target and reject the rest.
    remote_cmd = (
        f"{shlex.quote(peer.remote_uxon)} attach {shlex.quote(expected.name)} "
        f"--user {shlex.quote(args.user)} "
        f"--expect-session {shlex.quote(expected.to_json())} "
        f"--audit-correlation-id {shlex.quote(corr_id)}"
    )
    ssh_argv = build_peer_ssh_argv(
        command_template=peer.command_template,
        extra_ssh_options=peer.extra_ssh_options,
        ssh_alias=peer.ssh_alias,
        remote_uxon=peer.remote_uxon,
        remote_command=remote_cmd,
        allocate_tty=True,
        connect_timeout=DEFAULT_CONNECT_TIMEOUT_SEC,
        # Interactive attach is a one-shot connection: the multiplex
        # savings (200-500 ms vs 5-20 ms) are negligible against a
        # human-paced session, while sharing the poller's
        # ControlMaster means a wedged master can hang the user's
        # terminal at ``unix_wait_for_peer``. Force a fresh connection.
        ssh_multiplex="off",
        ssh_control_persist_seconds=cfg.ssh_control_persist_seconds,
    )
    _audit.audit(
        "attach.remote.out.dispatch",
        peer_name=peer.name,
        ssh_alias=peer.ssh_alias,
        target_user=args.user,
        target_session=expected.name,
        session_id=expected.session_id,
        launch_nonce=expected.launch_nonce,
        execution_backend=expected.execution_backend,
        dry_run=args.dry_run,
        correlation_id=corr_id,
    )
    if args.dry_run:
        print(shlex.join(ssh_argv))
        return 0
    result = run_launch_request(
        LaunchRequest(
            cmd=tuple(ssh_argv),
            label=f"attach {expected.name}@{peer.name}",
            identity=expected,
            correlation_id=corr_id,
            peer_name=peer.name,
            capture_ssh_diagnostics=not peer.command_template,
        )
    )
    if result.warning:
        fail(result.warning)
    return result.rc


def _resolve_attach_target(
    args: ParsedArgs, sessions: list[SessionInfo], cfg: Config, **kwargs
) -> SessionInfo:
    if args.expected_session is not None:
        from uxon.app.kill import _target_for_identity

        if args.expected_session.user != kwargs["target_user"]:
            fail("--expect-session user differs from --user", 1)
        return _target_for_identity(cfg, args.expected_session, require_nonce=False)
    return sessions_probe._resolve_or_audit_not_found(args.target_id or "", sessions, cfg, **kwargs)


def do_attach(args: ParsedArgs, cfg: Config, launch_user: str) -> int:
    if not args.target_id:
        fail("attach requires an identifier")

    from uxon.infra import audit as _audit

    # Remote dispatch: --host routes to a configured peer over SSH.
    # Per-target execution gating happens on the peer (peer's own
    # 'uxon attach' runs the probe), so the local side does not need
    # to know the peer's user table. Mirrors do_kill --host.
    #
    if args.host is not None:
        return _do_attach_remote(args, cfg)

    # The target host always records the actual local operation. Correlation
    # joins it to an initiating host's remote-dispatch event but is not used to
    # infer transport provenance.
    attach_event = "session.attach.dispatch"

    target_user = args.user or launch_user
    if target_user != launch_user:
        from uxon.infra.sudo_probe import probe_sudo_capability

        caps = probe_sudo_capability(cfg, [target_user])
        if target_user not in caps.reachable_users:
            _audit.audit(
                attach_event,
                outcome="denied",
                session=args.target_id or "",
                target_user=target_user,
                profile="",
                agent="",
                dry_run=args.dry_run,
            )
            eprint(
                f"uxon-error: not-reachable (execution backend cannot reach {target_user}; "
                "check the selected backend helper and target policy)"
            )
            return 1
        sessions = sessions_probe.collect_sessions([target_user], cfg)
        target = _resolve_attach_target(
            args,
            sessions,
            cfg,
            audit_event=attach_event,
            target_user=target_user,
            extra={"profile": "", "agent": ""},
        )
        req = tmux._build_tmux_attach_request(target, cfg, target_user)
        full = list(req.cmd)
        if args.dry_run:
            _audit.audit(
                attach_event,
                session=target.name,
                session_id=target.tmux_session_id,
                launch_nonce=target.launch_nonce,
                execution_backend=target.execution_backend,
                target_user=target_user,
                profile=target.profile,
                agent=target.agent,
                dry_run=True,
            )
            print(f"attach_user={shlex.quote(target_user)}")
            print(f"socket={shlex.quote(tmux.tmux_socket_path(cfg, target_user))}")
            print(f"session={shlex.quote(target.name)}")
            print(f"exec {shlex.join(full)}")
            return 0
        # The shared interactive handoff waits for tmux so an acknowledged
        # error can be removed before returning to the caller.
        _audit.audit(
            attach_event,
            session=target.name,
            session_id=target.tmux_session_id,
            launch_nonce=target.launch_nonce,
            execution_backend=target.execution_backend,
            target_user=target_user,
            profile=target.profile,
            agent=target.agent,
            dry_run=args.dry_run,
        )
        result = run_launch_request(req)
        if result.warning:
            fail(result.warning)
        return result.rc

    # Same-user path.
    sessions = sessions_probe.collect_sessions([launch_user], cfg)
    if not sessions:
        legacy = sessions_probe.collect_sessions_for_user(
            cfg,
            launch_user,
            cfg.session_prefix,
            socket_path=None,
            legacy_prefixes=cfg.legacy_session_prefixes,
        )
        if legacy:
            fail(
                f"no sessions found on dedicated socket {tmux.tmux_socket_path(cfg, launch_user)}, "
                f"but legacy default-socket sessions still exist. Use 'uxon doctor' for details."
            )
    target = _resolve_attach_target(
        args,
        sessions,
        cfg,
        audit_event=attach_event,
        target_user=launch_user,
        extra={"profile": "", "agent": ""},
    )
    # Same-user dispatch audit fires once before the interactive handoff.
    # Emitting from ``do_attach`` (not ``attach_session``) keeps the
    # call exactly once per CLI invocation.
    _audit.audit(
        attach_event,
        session=target.name,
        session_id=target.tmux_session_id,
        launch_nonce=target.launch_nonce,
        execution_backend=target.execution_backend,
        target_user=launch_user,
        profile=target.profile,
        agent=target.agent,
        dry_run=args.dry_run,
    )
    return attach_session(target, cfg, launch_user, args.dry_run)


def _tui_launch_request_cls() -> type:
    """Lazy-load ``LaunchRequest`` from ``uxon.domain.launch_request`` (pure
    data; no textual import). Defers the request import until needed."""
    from uxon.domain.launch_request import LaunchRequest

    return LaunchRequest


def _session_name_from_launch_label(label: str) -> str:
    """Reuse the pure launch-label parser shared with terminal handoff logging."""
    from uxon.domain.launch_request import session_name_from_launch_label

    return session_name_from_launch_label(label)


def attach_session(
    target: SessionInfo, cfg: Config, launch_user: str, dry_run: bool = False
) -> int:
    req = tmux._build_tmux_attach_request(target, cfg, launch_user)
    if dry_run:
        print(f"attach_user={shlex.quote(launch_user)}")
        print(f"socket={shlex.quote(tmux.tmux_socket_path(cfg, launch_user))}")
        print(f"session={shlex.quote(target.name)}")
        print(f"exec {shlex.join(req.cmd)}")
        return 0
    tmux.require_tmux_server(cfg, launch_user, tmux.tmux_socket_path(cfg, launch_user))
    # The child inherits the controlling terminal; the shared handoff runs
    # outside Textual and waits to handle explicit diagnostic dismissal.
    result = run_launch_request(req)
    if result.warning:
        fail(result.warning)
    return result.rc

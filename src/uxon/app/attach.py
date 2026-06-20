# SPDX-License-Identifier: MIT
"""``uxon attach`` use-case: local, cross-user, and remote session attach."""

from __future__ import annotations

import os
import shlex

from uxon.domain.args import ParsedArgs
from uxon.domain.config import Config
from uxon.domain.session import SessionInfo
from uxon.errors import eprint, fail
from uxon.infra import sessions_probe, tmux


def _do_attach_remote(args: ParsedArgs, cfg: Config) -> int:
    """Handle ``uxon attach <id> --host <alias> --user <u>``.

    Looks up the configured peer, builds an interactive ssh argv via
    :func:`build_peer_ssh_argv`, and execvp's it. Peer's own
    ``uxon attach --user`` runs the per-target sudo probe, so the
    local side does not need to know the peer's user table.

    The wire command always passes ``--user`` (even when it equals
    the ssh-login-user on the peer): peer is the sole authority on
    'who can attach to what', and we route that decision through
    its own gating. ``--user`` was made required at parse time
    (:func:`_parse_attach_extras`).
    """
    from uxon.infra.remote.collector import DEFAULT_CONNECT_TIMEOUT_SEC
    from uxon.infra.remote.ssh_argv import build_peer_ssh_argv
    from uxon.infra.remote_hosts import find_host

    peer = find_host(cfg.remote_hosts, args.host or "")
    if peer is None:
        names = ", ".join(h.name for h in cfg.remote_hosts) or "(none)"
        fail(f"unknown --host {args.host!r}; configured: {names}")
    assert args.user is not None  # parser-enforced
    import uuid as _uuid

    from uxon.infra import audit as _audit

    corr_id = str(_uuid.uuid4())
    _audit.set_correlation_id(corr_id)
    # ``target_id`` MUST come first after the verb: peer-side
    # ``parse_subcommand`` reads ``argv[1]`` as the target, with flags
    # tail-parsed afterwards.  Putting flags first makes the peer parse
    # the flag name as the target and reject the rest.
    remote_cmd = (
        f"{shlex.quote(peer.remote_uxon)} attach {shlex.quote(args.target_id or '')} "
        f"--user {shlex.quote(args.user)} "
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
    # Lane B — interactive terminal handoff: ``execvp`` replaces this image
    # with the ssh client, which keeps the controlling terminal. Bypasses
    # ``Popen``/the loop guard by construction.
    # Audit must fire *before* ``os.execvp`` (Bug 7) — once the process
    # image is replaced the cached socket is gone.  ``audit()`` is a
    # non-blocking ``socket.send``, so the kernel buffers the datagram
    # and the data is handed off before we exec.
    _audit.audit(
        "attach.remote.out",
        peer_name=peer.name,
        ssh_alias=peer.ssh_alias,
        target_user=args.user,
        target_session=args.target_id,
        correlation_id=corr_id,
    )
    if args.dry_run:
        print(shlex.join(ssh_argv))
        return 0
    try:
        os.execvp(ssh_argv[0], ssh_argv)
    except Exception as exc:
        _audit.audit(
            "attach.remote.out",
            outcome="error",
            peer_name=peer.name,
            ssh_alias=peer.ssh_alias,
            target_user=args.user,
            target_session=args.target_id,
            correlation_id=corr_id,
            error=str(exc)[:256],
        )
        raise
    return 0  # unreachable


def do_attach(args: ParsedArgs, cfg: Config, launch_user: str) -> int:
    if not args.target_id:
        fail("attach requires an identifier")

    from uxon.infra import audit as _audit

    # Remote dispatch: --host routes to a configured peer over SSH.
    # Per-target sudo gating happens on the peer (peer's own
    # 'uxon attach' runs the probe), so the local side does not need
    # to know the peer's user table. Mirrors do_kill --host.
    #
    # Checked *before* the SSH_CONNECTION peer-inbound branch: a
    # caller invoking ``ssh peer1 "uxon attach --host peer2 …"`` is the
    # caller-side leg dispatching onward, not a peer-inbound terminus,
    # and must not emit ``attach.remote.in``.
    if args.host is not None:
        return _do_attach_remote(args, cfg)

    # Bug 6 — peer-inbound branch.  When invoked over SSH the only
    # signal that this is the peer side of an ``attach.remote.out`` is
    # ``SSH_CONNECTION`` in the env (sudo strips it on the next leg, so
    # we have to capture it before the sudo execvp below).  Spec line
    # 299: ``attach.remote.in`` *replaces* ``session.attach`` on the
    # peer side — both names describe the same physical event from
    # caller-vs-peer POV.
    #
    # The spec also requires (line 207-209) that state-changing events
    # emit on **both** the success and failure paths.  We honour that
    # for the peer side too: instead of a single ``outcome=ok`` emit at
    # the top, every ``session.attach`` emission point below switches
    # event name (``attach.remote.in``) and identifier-field name
    # (``target_session`` instead of ``session``) when ``peer_inbound``.
    # An auditor querying ``EVENT=attach.remote.in OUTCOME=denied``
    # then actually finds the failure.
    peer_inbound = bool(os.environ.get("SSH_CONNECTION"))
    _attach_event: str = "attach.remote.in" if peer_inbound else "session.attach"
    _session_field: str = "target_session" if peer_inbound else "session"

    target_user = args.user or launch_user
    if target_user != launch_user:
        from uxon.infra.sudo_probe import probe_sudo_capability

        caps = probe_sudo_capability([target_user])
        if target_user not in caps.reachable_users:
            _audit.audit(
                _attach_event,
                outcome="denied",
                **{_session_field: args.target_id or ""},
                target_user=target_user,
            )
            eprint(
                f"uxon-error: not-reachable (cannot sudo -niu {target_user}; "
                "check /etc/sudoers.d for a NOPASSWD rule for this target)"
            )
            return 1
        sessions = sessions_probe.collect_sessions([target_user], cfg)
        target = sessions_probe._resolve_or_audit_not_found(
            args.target_id,
            sessions,
            cfg,
            audit_event=_attach_event,
            target_user=target_user,
            session_field=_session_field,
        )
        base = tmux.configured_tmux_base(cfg, target_user) + ["attach-session", "-t", target.name]
        full = ["sudo", "-niu", target_user, "--", *base]
        if args.dry_run:
            print(f"attach_user={shlex.quote(target_user)}")
            print(f"socket={shlex.quote(tmux.tmux_socket_path(cfg, target_user))}")
            print(f"session={shlex.quote(target.name)}")
            print(f"exec {shlex.join(full)}")
            return 0
        # Lane B — interactive terminal handoff: ``execvp`` replaces this
        # image with ``tmux attach``, which keeps the controlling terminal.
        # Bypasses ``Popen``/the loop guard by construction.
        # Audit before ``os.execvp`` (Bug 7) — once the image is
        # replaced our cached socket is gone.
        _audit.audit(
            _attach_event,
            **{_session_field: target.name},
            target_user=target_user,
        )
        try:
            os.execvp(full[0], full)
        except Exception as exc:
            _audit.audit(
                _attach_event,
                outcome="error",
                **{_session_field: target.name},
                target_user=target_user,
                error=str(exc)[:256],
            )
            raise
        return 0

    # Same-user path.
    sessions = sessions_probe.collect_sessions([launch_user], cfg)
    if not sessions:
        legacy = sessions_probe.collect_sessions_for_user(
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
    target = sessions_probe._resolve_or_audit_not_found(
        args.target_id,
        sessions,
        cfg,
        audit_event=_attach_event,
        target_user=launch_user,
        session_field=_session_field,
    )
    # Same-user audit fires once before ``attach_session``'s execvp.
    # Emitting from ``do_attach`` (not ``attach_session``) keeps the
    # call exactly once per CLI invocation.
    _audit.audit(
        _attach_event,
        **{_session_field: target.name},
        target_user=launch_user,
    )
    try:
        return attach_session(target, cfg, launch_user, args.dry_run)
    except Exception as exc:
        _audit.audit(
            _attach_event,
            outcome="error",
            **{_session_field: target.name},
            target_user=launch_user,
            error=str(exc)[:256],
        )
        raise


def _tui_launch_request_cls() -> type:
    """Lazy-load ``LaunchRequest`` from ``uxon.domain.launch_request`` (pure
    data; no textual import). Kept as a function so the module-top import
    surface of cli.py stays small."""
    from uxon.domain.launch_request import LaunchRequest

    return LaunchRequest


def _session_name_from_launch_label(label: str) -> str:
    """Thin wrapper so cli.py call sites keep their local symbol.

    Helper lives next to LaunchRequest (``uxon.domain.launch_request``) so
    the TUI run-loop can reuse it for ``session.ended`` without a circular
    dep on cli.py.
    """
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
    # Lane B — interactive terminal handoff. ``execvp`` replaces this
    # process image with ``tmux attach``; the TUI is gone and the child
    # inherits the real controlling terminal (an interactive attach needs
    # one). This bypasses ``subprocess``/``Popen`` entirely, so it is
    # outside the loop guard and the no-raw-spawn test by construction.
    os.execvp(req.cmd[0], list(req.cmd))
    return 0

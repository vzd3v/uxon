# SPDX-License-Identifier: MIT
"""``uxon kill`` / ``kill-all`` use-cases: local, cross-user, and remote kill."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

import uxon.app.listing as listing_app
from uxon.domain.args import ParsedArgs
from uxon.domain.config import Config
from uxon.domain.session import SessionInfo
from uxon.errors import eprint, fail
from uxon.infra import identity, process, sessions_probe, tmux
from uxon.infra.run import run_query


@dataclass(frozen=True)
class RuntimeTeardown:
    """A prepared runtime session stop (the kill-path mirror of launch).

    Captured while the session is still alive (its env holds the resolved
    runtime resource and launch-time identity markers). ``stop_cmd`` is the
    rendered ``stop_command`` argv; ``resource`` is operator-chosen;
    ``launch_epoch`` is the resource start epoch
    stashed at launch (``UXON_RUNTIME_EPOCH``, empty when ``identity_command`` is
    unset). The PID-recycle guard (AC-P3.5) compares ``launch_epoch`` against
    the live epoch in :func:`run_runtime_teardown` — re-resolved there, after
    ``kill-session``, so the comparison sits as close to the kill as possible.
    """

    stop_cmd: list[str]
    runtime: str
    resource: str
    runtime_id: str
    runtime_fingerprint: str
    launch_epoch: str = ""


def _print_killed(name: str, cfg: Config) -> None:
    """Print the kill-success line.

    Runtime-specific cleanup is handled by ``stop_command`` before this line.
    """
    print(f"killed: {name}")


def _teardown_skip(session_name: str, reason: str) -> None:
    eprint(f"uxon: runtime session stop for {session_name} skipped: {reason}")


def prepare_runtime_teardown(cfg: Config, target: SessionInfo) -> RuntimeTeardown | None:
    """Prepare the workload session stop, or return ``None``.

    When a runtime ``stop_command`` is set, launch records the workload agent
    PID and stashes the resolved resource on the session environment. This reads it and
    renders the operator's stop_command against it and the per-session
    pidfile — targeting exactly this session's process (the resource may be
    shared across arbitrarily many sessions, so a per-session PID is the only
    correct target).

    **PID-recycle guard (AC-P3.5):** this captures the launch-time start-epoch
    (``UXON_RUNTIME_EPOCH``, set only when ``identity_command`` is configured) off
    the still-live session env onto the returned teardown. The actual
    restarted-since-launch comparison happens in :func:`run_runtime_teardown`,
    which re-resolves the *live* epoch immediately before the kill (after
    ``kill-session``) — keeping the check as close to the kill as possible
    rather than sampling it here, a whole ``kill-session`` round-trip earlier.

    MUST run while the session is still alive (the name + markers live in its
    env). The caller runs the result via :func:`run_runtime_teardown` AFTER
    ``kill-session`` — killing the agent closes its pane, so reaping first
    would race tmux's own session teardown and leave ``kill-session`` with no
    server to talk to. Kill the session first, then reap the orphan the
    severed exec leaves behind.

    Self-contained and non-raising: any failure (no stop_command, name
    unreadable/invalid, bad template) degrades to None, and the kill proceeds
    regardless (orphaning is no worse than the pre-teardown behaviour).
    """
    if not target.runtime_resource and not target.runtime_marker:
        return None
    if not target.launch_record_verified:
        _teardown_skip(target.name, "no verified launch record")
        return None
    if not target.runtime:
        _teardown_skip(target.name, "missing workload runtime")
        return None
    profile = cfg.runtimes.get(target.runtime)
    if profile is None:
        _teardown_skip(target.name, f"workload runtime {target.runtime!r} is unavailable")
        return None
    if profile.fingerprint != target.runtime_fingerprint:
        _teardown_skip(target.name, "workload runtime changed since launch")
        return None
    if not profile.stop_command:
        return None
    if not target.runtime_id or not target.runtime_epoch:
        _teardown_skip(target.name, "missing runtime resource identity")
        return None
    try:
        from uxon.domain.runtime import (
            is_valid_runtime_resource,
            render_profile_template,
            runtime_pidfile,
        )

        name = target.runtime_resource
        if not name or not is_valid_runtime_resource(name):
            _teardown_skip(target.name, "missing runtime resource")
            return None
        pidfile = runtime_pidfile(target.name)
        stop_cmd = render_profile_template(
            profile.stop_command,
            profile=profile,
            what="stop_command",
            resource=name,
            runtime_dir="/",
            user=target.launch_user or target.user,
            launch_profile=target.profile,
            agent=target.agent,
            project_slug="",
            pidfile=pidfile,
        )
        return RuntimeTeardown(
            stop_cmd=stop_cmd,
            runtime=target.runtime,
            resource=name,
            runtime_id=target.runtime_id,
            runtime_fingerprint=target.runtime_fingerprint,
            launch_epoch=target.runtime_epoch,
        )
    except SystemExit as exc:  # template render failure must never abort the kill
        msg = getattr(exc, "uxon_msg", exc)
        _teardown_skip(target.name, str(msg))
        return None
    except Exception as exc:  # noqa: BLE001 — teardown must never block kill-session
        _teardown_skip(target.name, str(exc))
        return None


def run_runtime_teardown(
    cfg: Config, teardown: RuntimeTeardown, target_user: str, session_name: str
) -> None:
    """Run a prepared teardown (best-effort) — after ``kill-session``.

    The single shared teardown-call site: every kill path (CLI self/cross-user,
    kill-all, and the three TUI bridge paths) routes through here so the
    ``runtime.session_stop`` audit event (AC-P3.2) is emitted uniformly with
    ``name``, ``session``, and ``outcome`` (``ok`` / ``error`` / ``stale``).
    Carries zero secrets; the runtime resource is operator-chosen.

    **PID-recycle guard (AC-P3.5):** when the launch stashed a start-epoch, the
    live epoch is re-resolved *here* — immediately before the kill, after
    ``kill-session`` — and a confirmed mismatch means the resource restarted
    since launch (the recorded runtime PID now names an unrelated
    process). The stop command is then **not** run and the event records
    ``outcome=stale``. The guard fires only on a *confirmed* restart: when the
    epoch cannot be re-resolved (no ``identity_command``, resource gone, probe
    fails) the kill proceeds. A residual window remains between this re-resolve
    and the kill itself — fully closing it would need an epoch-conditional
    ``stop_command`` (an operator-template concern), but re-resolving here
    rather than at capture time shrinks it to a single resolve→kill round-trip.
    Otherwise reaps the workload agent the severed ``exec`` orphaned. Never
    raises; a failure is surfaced as a note and the kill is already done.
    """
    from uxon.infra import audit as _audit
    from uxon.infra import runtime as runtime_infra

    profile = cfg.runtimes.get(teardown.runtime)
    if profile is None:
        _teardown_skip(session_name, f"workload runtime {teardown.runtime!r} is unavailable")
        _audit.audit(
            "runtime.session_stop",
            outcome="missing_profile",
            runtime=teardown.runtime,
            runtime_resource=teardown.resource,
            action="skip",
            session=session_name,
            target_user=target_user,
        )
        return
    if profile.fingerprint != teardown.runtime_fingerprint:
        _teardown_skip(session_name, "workload runtime changed since launch")
        _audit.audit(
            "runtime.session_stop",
            outcome="fingerprint_mismatch",
            runtime=teardown.runtime,
            runtime_resource=teardown.resource,
            action="skip",
            session=session_name,
            target_user=target_user,
        )
        return
    live = runtime_infra.current_runtime_identity_for_profile(
        cfg, profile, teardown.resource, target_user
    )
    if live is None:
        _teardown_skip(session_name, "live runtime resource identity could not be resolved")
        _audit.audit(
            "runtime.session_stop",
            outcome="identity_unresolved",
            runtime=teardown.runtime,
            runtime_resource=teardown.resource,
            action="skip",
            session=session_name,
            target_user=target_user,
        )
        return
    if live.id != teardown.runtime_id or live.epoch != teardown.launch_epoch:
        _teardown_skip(
            session_name,
            "the runtime resource identity changed since launch",
        )
        _audit.audit(
            "runtime.session_stop",
            outcome="stale",
            runtime=teardown.runtime,
            runtime_resource=teardown.resource,
            action="skip",
            session=session_name,
            target_user=target_user,
        )
        return

    ok, detail = runtime_infra.run_teardown(
        cfg, teardown.stop_cmd, target_user, profile.stop_timeout_seconds
    )
    if not ok:
        eprint(f"uxon: runtime session stop for {session_name} did not complete: {detail}")
    _audit.audit(
        "runtime.session_stop",
        outcome="ok" if ok else "error",
        runtime=teardown.runtime,
        runtime_resource=teardown.resource,
        action="stop",
        session=session_name,
        target_user=target_user,
        **({"error": detail[:256]} if not ok and detail else {}),
    )


def run_kill_session(
    full: list[str],
    *,
    audit_event: str,
    session: str,
    target_user: str,
    profile: str,
    agent: str,
    force: bool,
    dry_run: bool,
) -> None:
    """Run a ``kill-session`` command; on a non-zero exit emit the failure audit
    (with the command's real rc) before the failure propagates.

    The single shared kill-spawn site (CLI self / cross-user / kill-all and the
    TUI bridge). ``run_cmd(check=True)`` cannot be used here: its failure path
    is ``fail() -> SystemExit``, never ``CalledProcessError``, so an
    ``except subprocess.CalledProcessError`` around it is dead and the error
    audit is skipped. We run with ``check=False`` and translate explicitly so
    the ``<event> outcome=error`` record fires before ``fail()``.
    """
    from uxon.infra import audit as _audit

    cp = process.run_cmd(full, check=False)
    if cp.returncode != 0:
        _audit.audit(
            audit_event,
            outcome="error",
            session=session,
            target_user=target_user,
            profile=profile,
            agent=agent,
            force=force,
            dry_run=dry_run,
            rc=cp.returncode,
        )
        stderr = (cp.stderr or cp.stdout or "").strip()
        fail(stderr or f"kill-session failed (rc={cp.returncode})", 1)


def _confirm_kill_or_fail(prompt: str, args: ParsedArgs) -> None:
    """Common confirmation gate for cross-user / cross-host kills.

    ``--json`` is non-interactive — refuse unless ``--force`` or
    ``--dry-run`` was passed (mirrors the ``kill-all`` precedent).
    On a TTY without ``--force``, prompt for the literal phrase
    ``kill``. Non-TTY without ``--force`` fails fast with a hint.
    """
    if args.force or args.dry_run:
        return
    if args.json_output:
        fail("kill --json requires --force or --dry-run")
    if not identity.is_interactive_tty():
        fail(
            "kill is destructive; rerun with --force, or omit --user/--host for the local self path"
        )
    response = input(f"{prompt} Type 'kill' to confirm: ")
    if response.strip() != "kill":
        fail("cancelled", 130)


def _do_kill_remote(args: ParsedArgs, cfg: Config) -> int:
    """Handle ``uxon kill <id> --host <alias>`` (optionally with ``--user``).

    Looks up the configured peer, optionally confirms with the user
    locally, then dispatches the kill to the peer over SSH. The
    peer's own ``uxon kill`` does the per-target sudo gating, so
    the local side does not need to know the peer's user table —
    this matches the design constraint that bulk destructive ops
    stay local while per-session kill may cross hosts.

    Confirmation shape mirrors :func:`do_kill` for the local case:
    ``--json`` requires ``--force`` or ``--dry-run``; an interactive
    TTY without ``--force`` prompts for the literal phrase ``kill``.

    On the wire we always pass ``--force`` to the peer — local
    confirmation is a UI gesture, not a wire concern; the peer
    must not re-prompt.
    """
    from uxon.infra.remote.collector import (
        DEFAULT_CONNECT_TIMEOUT_SEC,
        DEFAULT_TOTAL_TIMEOUT_SEC,
    )
    from uxon.infra.remote.master_recovery import recover_wedged_master
    from uxon.infra.remote.ssh_argv import build_peer_ssh_argv
    from uxon.infra.remote_hosts import find_host

    if not cfg.remote_hosts:
        fail("no [[remote_hosts]] configured; --host requires at least one peer")
    target_host = find_host(cfg.remote_hosts, args.host or "")
    if target_host is None:
        names = ", ".join(h.name for h in cfg.remote_hosts) or "<none>"
        fail(f"unknown --host {args.host!r}; configured: {names}")

    target_user_part = f" (user={args.user})" if args.user else ""
    prompt = f"Kill {args.target_id}@{target_host.name}{target_user_part}?"
    _confirm_kill_or_fail(prompt, args)

    # ``target_id`` MUST come first after the verb: peer-side
    # ``parse_subcommand`` reads ``argv[1]`` as the target, flags are
    # tail-parsed afterwards.  Mirrors ``_do_attach_remote`` ordering.
    remote_cmd_parts = [
        shlex.quote(target_host.remote_uxon),
        "kill",
        shlex.quote(str(args.target_id)),
        "--force",
    ]
    if args.user:
        remote_cmd_parts.extend(["--user", shlex.quote(args.user)])
    if args.json_output:
        remote_cmd_parts.append("--json")
    # Correlation-id append must precede the join.  ``_do_kill_remote``
    # uses ``run_query`` (not ``os.execvp``), so there is no Bug 7
    # process-replacement concern here — the audit emit is correct
    # anywhere before the run.
    import uuid as _uuid

    from uxon.infra import audit as _audit

    corr_id = str(_uuid.uuid4())
    _audit.set_correlation_id(corr_id)
    remote_cmd_parts.extend(["--audit-correlation-id", shlex.quote(corr_id)])
    remote_cmd = " ".join(remote_cmd_parts)
    _audit.audit(
        "kill.remote.out",
        peer_name=target_host.name,
        ssh_alias=target_host.ssh_alias,
        target_user=args.user,
        target_session=args.target_id,
        force=args.force,
        dry_run=args.dry_run,
        correlation_id=corr_id,
    )
    ssh_argv = build_peer_ssh_argv(
        command_template=target_host.command_template,
        extra_ssh_options=target_host.extra_ssh_options,
        ssh_alias=target_host.ssh_alias,
        remote_uxon=target_host.remote_uxon,
        remote_command=remote_cmd,
        allocate_tty=False,
        connect_timeout=DEFAULT_CONNECT_TIMEOUT_SEC,
        ssh_multiplex=cfg.ssh_multiplex,
        ssh_control_persist_seconds=cfg.ssh_control_persist_seconds,
    )

    if args.dry_run:
        if args.json_output:
            listing_app._emit_json_with_host(
                "kill",
                {
                    "target": args.target_id,
                    "target_user": args.user,
                    "action": "would-kill",
                    "dry_run": True,
                    "ssh_argv": ssh_argv,
                },
                host=target_host.name,
            )
        else:
            print(f"dry-run: {shlex.join(ssh_argv)}")
        return 0

    def _emit_kill_remote_error(error: str, rc: int) -> None:
        _audit.audit(
            "kill.remote.out",
            outcome="error",
            peer_name=target_host.name,
            ssh_alias=target_host.ssh_alias,
            target_user=args.user,
            target_session=args.target_id,
            force=args.force,
            dry_run=args.dry_run,
            correlation_id=corr_id,
            rc=rc,
            error=error[:256],
        )

    try:
        cp = run_query(
            ssh_argv,
            timeout=DEFAULT_TOTAL_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        # Same wedge-recovery as the polling path — without it a CLI
        # ``uxon kill --host`` invoked when no TUI is running has no
        # other consumer to drive recovery, and every retry will hang
        # identically until the master is killed by hand. See
        # ``fetch_remote_snapshot._run_one`` for the rationale.
        if cfg.ssh_multiplex != "off":
            recover_wedged_master(target_host)
        _emit_kill_remote_error("ssh timeout", 124)
        eprint(f"uxon: --host {target_host.name}: ssh timeout after {DEFAULT_TOTAL_TIMEOUT_SEC}s")
        return 1
    except FileNotFoundError:
        _emit_kill_remote_error("ssh binary missing", 127)
        eprint("uxon: ssh not installed on local host")
        return 1

    if cp.stdout:
        sys.stdout.write(cp.stdout)
    if cp.stderr:
        sys.stderr.write(cp.stderr)
    if cp.returncode != 0:
        _emit_kill_remote_error("non-zero ssh rc", cp.returncode)
        return 1
    return 0


def do_kill(args: ParsedArgs, cfg: Config, launch_user: str) -> int:
    if not args.target_id:
        fail("kill requires an identifier")

    from uxon.infra import audit as _audit

    # Remote dispatch: --host routes to a configured peer over SSH.
    # Per-target sudo gating happens on the peer (its own ``uxon kill``
    # runs the probe), so the local side does not need to know the
    # peer's user table. Bulk kill stays strictly local.
    #
    # Checked *before* the SSH_CONNECTION peer-inbound branch: a chained
    # ``ssh peer1 "uxon kill --host peer2 …"`` invocation is the
    # caller-side dispatch leg, not a peer-inbound terminus.
    if args.host is not None:
        return _do_kill_remote(args, cfg)

    # Bug 6 — peer-inbound branch.  Same shape as ``do_attach`` above.
    # ``correlation_id`` is auto-injected by ``audit()`` from module
    # state (the parser layer set it via ``set_correlation_id`` after
    # popping ``--audit-correlation-id`` from argv).  Spec line 302:
    # ``kill.remote.in`` *replaces* ``session.kill`` for the peer-side
    # branch.  Spec line 207-209: state-changing events emit on **both**
    # success and failure paths; we honour that on the peer side too by
    # switching the event name at every emit point (rather than the old
    # single ``outcome=ok`` emit at the top, which lost the failure
    # signal for sudo-denied / not-found / process.run_cmd-error paths).  Per
    # spec line 225, ``kill.remote.in`` shares the ``session`` key with
    # ``session.kill`` — only the event name differs, no field rename.
    peer_inbound = bool(os.environ.get("SSH_CONNECTION"))
    _kill_event: str = "kill.remote.in" if peer_inbound else "session.kill"

    # Local cross-user kill: --user X where X != launch_user requires
    # per-target NOPASSWD. Probe once for the single target (the same
    # probe machinery the TUI uses on startup, but a single-target
    # subset). Matches the TUI's per-target sudo gating.
    target_user = args.user or launch_user
    if target_user != launch_user:
        from uxon.infra.sudo_probe import probe_sudo_capability

        caps = probe_sudo_capability(cfg, [target_user])
        reachable = target_user in caps.reachable_users
        if not reachable:
            _audit.audit(
                _kill_event,
                outcome="denied",
                session=args.target_id or "",
                target_user=target_user,
                profile="",
                agent="",
                force=args.force,
                dry_run=args.dry_run,
            )
            # Stable error tag — mirrors the ``all-users-disabled``
            # precedent. Callers (and the SSH peer-aggregator) parse
            # this exact substring. Surface the verdict on dry-run too:
            # without sudo we cannot resolve the session name, so the
            # honest answer is "this would fail" rather than a faked
            # would-kill envelope.
            eprint(
                f"uxon-error: not-reachable (cannot sudo -niu {target_user}; "
                "check /etc/sudoers.d for a NOPASSWD rule for this target)"
            )
            return 1

        prompt = f"Kill {args.target_id} (user={target_user})?"
        _confirm_kill_or_fail(prompt, args)

        sessions = sessions_probe.collect_sessions([target_user], cfg)
        target = sessions_probe._resolve_or_audit_not_found(
            args.target_id,
            sessions,
            cfg,
            audit_event=_kill_event,
            target_user=target_user,
            extra={"force": args.force, "dry_run": args.dry_run, "profile": "", "agent": ""},
        )
        # Non-interactive sudo: there's no TTY in the kill path even
        # for the CLI; if NOPASSWD is missing we want a fast failure
        # rather than a blocked password prompt.
        full = tmux.configured_tmux_base(cfg, target_user, nonint=True) + [
            "kill-session",
            "-t",
            target.name,
        ]
        if args.dry_run:
            _audit.audit(
                _kill_event,
                session=target.name,
                target_user=target_user,
                profile=target.profile,
                agent=target.agent,
                force=args.force,
                dry_run=True,
            )
            if args.json_output:
                listing_app._emit_json(
                    "kill",
                    {
                        "target": target.name,
                        "user": launch_user,
                        "target_user": target_user,
                        "reachable": reachable,
                        "socket": tmux.tmux_socket_path(cfg, target_user),
                        "action": "would-kill",
                        "dry_run": True,
                    },
                )
            else:
                print(f"dry-run: {shlex.join(full)}")
            return 0
        # Prepare teardown from the verified launch record before tmux removes
        # the session; run it after kill-session severs the container exec.
        teardown = prepare_runtime_teardown(cfg, target)
        run_kill_session(
            full,
            audit_event=_kill_event,
            session=target.name,
            target_user=target_user,
            profile=target.profile,
            agent=target.agent,
            force=args.force,
            dry_run=args.dry_run,
        )
        if teardown:
            run_runtime_teardown(cfg, teardown, target_user, target.name)
        _audit.audit(
            _kill_event,
            session=target.name,
            target_user=target_user,
            profile=target.profile,
            agent=target.agent,
            force=args.force,
            dry_run=args.dry_run,
        )
        if args.json_output:
            listing_app._emit_json(
                "kill",
                {
                    "target": target.name,
                    "user": launch_user,
                    "target_user": target_user,
                    "reachable": True,
                    "socket": tmux.tmux_socket_path(cfg, target_user),
                    "action": "killed",
                    "dry_run": False,
                },
            )
        else:
            _print_killed(target.name, cfg)
        return 0

    # Self-only path: unchanged from the pre-3.4.0 behaviour.
    sessions = sessions_probe.collect_sessions([launch_user], cfg)
    target = sessions_probe._resolve_or_audit_not_found(
        args.target_id,
        sessions,
        cfg,
        audit_event=_kill_event,
        target_user=launch_user,
        extra={"force": args.force, "dry_run": args.dry_run, "profile": "", "agent": ""},
    )
    full = tmux.configured_tmux_base(cfg, launch_user) + ["kill-session", "-t", target.name]
    if args.dry_run:
        _audit.audit(
            _kill_event,
            session=target.name,
            target_user=launch_user,
            profile=target.profile,
            agent=target.agent,
            force=args.force,
            dry_run=True,
        )
        if args.json_output:
            listing_app._emit_json(
                "kill",
                {
                    "target": target.name,
                    "user": launch_user,
                    "socket": tmux.tmux_socket_path(cfg, launch_user),
                    "action": "would-kill",
                    "dry_run": True,
                },
            )
        else:
            print(f"dry-run: {shlex.join(full)}")
        return 0
    # Prepare teardown from the verified launch record before tmux removes the
    # session; run it after kill-session severs the container exec.
    teardown = prepare_runtime_teardown(cfg, target)
    run_kill_session(
        full,
        audit_event=_kill_event,
        session=target.name,
        target_user=launch_user,
        profile=target.profile,
        agent=target.agent,
        force=args.force,
        dry_run=args.dry_run,
    )
    if teardown:
        run_runtime_teardown(cfg, teardown, launch_user, target.name)
    _audit.audit(
        _kill_event,
        session=target.name,
        target_user=launch_user,
        profile=target.profile,
        agent=target.agent,
        force=args.force,
        dry_run=args.dry_run,
    )
    if args.json_output:
        listing_app._emit_json(
            "kill",
            {
                "target": target.name,
                "user": launch_user,
                "socket": tmux.tmux_socket_path(cfg, launch_user),
                "action": "killed",
                "dry_run": False,
            },
        )
    else:
        _print_killed(target.name, cfg)
    return 0


def do_kill_all(args: ParsedArgs, cfg: Config, launch_user: str) -> int:
    sessions = sessions_probe.collect_sessions([launch_user], cfg)
    if not sessions:
        from uxon.infra import audit as _audit

        _audit.audit(
            "session.kill_all",
            target_users=[launch_user],
            killed_count=0,
            dry_run=args.dry_run,
        )
        if args.json_output:
            listing_app._emit_json(
                "kill-all",
                {
                    "user": launch_user,
                    "socket": tmux.tmux_socket_path(cfg, launch_user),
                    "dry_run": args.dry_run,
                    "sessions": [],
                },
            )
        else:
            print(f"uxon: no {cfg.session_prefix}* sessions for {launch_user}")
        return 0
    if not args.dry_run and not args.force:
        if args.json_output:
            # --json is a non-interactive surface; we never prompt with
            # JSON enabled. Force the caller to be explicit.
            fail("kill-all --json requires --force or --dry-run")
        if not identity.is_interactive_tty():
            fail(
                "kill-all is destructive; rerun with --force, or use 'uxon list' / 'uxon doctor' first"
            )
        names = ", ".join(s.name for s in sessions)
        response = input(
            f"Kill all {len(sessions)} session(s) on {tmux.tmux_socket_path(cfg, launch_user)}: {names}\nType 'kill-all' to confirm: "
        )
        if response.strip() != "kill-all":
            fail("cancelled", 130)
    results: list[dict[str, Any]] = []
    for s in sessions:
        full = tmux.configured_tmux_base(cfg, launch_user) + ["kill-session", "-t", s.name]
        if args.dry_run:
            if not args.json_output:
                print(f"dry-run: {shlex.join(full)}")
            results.append({"name": s.name, "action": "would-kill"})
            continue
        # Capture teardown before the kill, reap the orphan after it.
        teardown = prepare_runtime_teardown(cfg, s)
        cp = process.run_cmd(full, check=False)
        ok = cp.returncode == 0
        if ok and teardown:
            run_runtime_teardown(cfg, teardown, launch_user, s.name)
        if not args.json_output:
            print(f"killed: {s.name}" if ok else f"failed: {s.name}")
        results.append({"name": s.name, "action": "killed" if ok else "failed"})
    if args.json_output:
        listing_app._emit_json(
            "kill-all",
            {
                "user": launch_user,
                "socket": tmux.tmux_socket_path(cfg, launch_user),
                "dry_run": args.dry_run,
                "sessions": results,
            },
        )
    from uxon.infra import audit as _audit

    killed = sum(1 for r in results if r["action"] == "killed")
    attempted = sum(1 for r in results if r["action"] in ("killed", "failed"))
    _audit.audit(
        "session.kill_all",
        outcome="ok" if killed == attempted else "error",
        target_users=[launch_user],
        killed_count=killed,
        dry_run=args.dry_run,
    )
    return 0

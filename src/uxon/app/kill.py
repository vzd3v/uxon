# SPDX-License-Identifier: MIT
"""``uxon kill`` / ``kill-all`` use-cases: local, cross-user, and remote kill."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from typing import Any

import uxon.app.listing as listing_app
from uxon.domain.args import ParsedArgs
from uxon.domain.config import Config
from uxon.errors import eprint, fail
from uxon.infra import identity, process, sessions_probe, tmux


def _print_killed(name: str, cfg: Config) -> None:
    """Print the kill-success line plus the container caveat when enabled.

    Centralizes the ``[container].enabled`` reminder (Security MEDIUM-2) so a
    killed tmux session whose agent ran in a long-lived container doesn't read
    as fully contained. The caveat carries zero internals (``<runtime>`` is a
    config value).
    """
    from uxon.domain.container import kill_caveat

    print(f"killed: {name}")
    caveat = kill_caveat(cfg.container)
    if caveat is not None:
        print(f"note: {caveat}")


def prepare_container_teardown(
    cfg: Config, target_user: str, session_name: str
) -> list[str] | None:
    """Render the in-container stop command for ``session_name``, or None.

    The mirror of the launch wrap: when ``[container].stop_template`` is set,
    the launch recorded the agent's in-container PID and stashed the resolved
    container name on the session *environment*. This reads that name back and
    renders the operator's stop_template against it and the per-session
    pidfile — targeting exactly this session's process (the container is
    shared across arbitrarily many sessions, so a per-session PID is the only
    correct target).

    MUST run while the session is still alive (the name lives in its env). The
    caller runs the returned argv via :func:`run_container_teardown` AFTER
    ``kill-session`` — killing the agent closes its pane, so reaping first
    would race tmux's own session teardown and leave ``kill-session`` with no
    server to talk to. Kill the session first, then reap the orphan the
    severed exec leaves behind.

    Self-contained and non-raising: any failure (no stop_template, name
    unreadable/invalid, bad template) degrades to None, and the kill proceeds
    regardless (orphaning is no worse than the pre-teardown behaviour).
    """
    c = cfg.container
    if not c.enabled or not c.stop_template:
        return None
    try:
        from uxon.domain.container import (
            CONTAINER_NAME_ENV,
            container_pidfile,
            is_valid_container_name,
            render_stop_template,
        )

        name = tmux.read_session_env(cfg, target_user, session_name, CONTAINER_NAME_ENV)
        if not name or not is_valid_container_name(name):
            # Session predates teardown, ran without a container, or the env
            # var is malformed — nothing safe to target.
            return None
        pidfile = container_pidfile(session_name)
        return render_stop_template(c.stop_template, name=name, pidfile=pidfile)
    except SystemExit as exc:  # render_stop_template fail() — never abort the kill
        msg = getattr(exc, "uxon_msg", exc)
        eprint(f"uxon: container teardown for {session_name} skipped: {msg}")
        return None
    except Exception as exc:  # noqa: BLE001 — teardown must never block kill-session
        eprint(f"uxon: container teardown for {session_name} skipped: {exc}")
        return None


def run_container_teardown(stop_cmd: list[str], target_user: str, session_name: str) -> None:
    """Run a prepared stop command (best-effort) — after ``kill-session``.

    Reaps the in-container agent the severed ``exec`` orphaned. Never raises;
    a failure is surfaced as a note and the kill is already done.
    """
    from uxon.infra import container as container_infra

    ok, detail = container_infra.run_teardown(stop_cmd, target_user)
    if not ok:
        eprint(f"uxon: container teardown for {session_name} did not complete: {detail}")


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
    # uses ``subprocess.run`` (not ``os.execvp``), so there is no Bug 7
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
        cp = subprocess.run(
            ssh_argv,
            capture_output=True,
            text=True,
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

        caps = probe_sudo_capability([target_user])
        reachable = target_user in caps.reachable_users
        if not reachable:
            _audit.audit(
                _kill_event,
                outcome="denied",
                session=args.target_id or "",
                target_user=target_user,
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
            extra={"force": args.force, "dry_run": args.dry_run},
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
        # Capture the teardown command while the session is still alive (its
        # env holds the container name); run it AFTER kill-session reaps the
        # orphaned in-container agent. Best-effort, never blocks the kill.
        stop_cmd = prepare_container_teardown(cfg, target_user, target.name)
        try:
            process.run_cmd(full, check=True)
        except subprocess.CalledProcessError as exc:
            _audit.audit(
                _kill_event,
                outcome="error",
                session=target.name,
                target_user=target_user,
                force=args.force,
                dry_run=args.dry_run,
                rc=exc.returncode,
            )
            raise
        if stop_cmd:
            run_container_teardown(stop_cmd, target_user, target.name)
        _audit.audit(
            _kill_event,
            session=target.name,
            target_user=target_user,
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
        extra={"force": args.force, "dry_run": args.dry_run},
    )
    full = tmux.configured_tmux_base(cfg, launch_user) + ["kill-session", "-t", target.name]
    if args.dry_run:
        _audit.audit(
            _kill_event,
            session=target.name,
            target_user=launch_user,
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
    # Capture teardown before the kill (session env holds the name); reap the
    # orphaned in-container agent after kill-session. Best-effort.
    stop_cmd = prepare_container_teardown(cfg, launch_user, target.name)
    try:
        process.run_cmd(full, check=True)
    except subprocess.CalledProcessError as exc:
        _audit.audit(
            _kill_event,
            outcome="error",
            session=target.name,
            target_user=launch_user,
            force=args.force,
            dry_run=args.dry_run,
            rc=exc.returncode,
        )
        raise
    if stop_cmd:
        run_container_teardown(stop_cmd, launch_user, target.name)
    _audit.audit(
        _kill_event,
        session=target.name,
        target_user=launch_user,
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
        stop_cmd = prepare_container_teardown(cfg, launch_user, s.name)
        cp = process.run_cmd(full, check=False)
        ok = cp.returncode == 0
        if ok and stop_cmd:
            run_container_teardown(stop_cmd, launch_user, s.name)
        if not args.json_output:
            print(f"killed: {s.name}" if ok else f"failed: {s.name}")
        results.append({"name": s.name, "action": "killed" if ok else "failed"})
    if not args.json_output and not args.dry_run:
        # Container caveat (Security MEDIUM-2): killing the tmux sessions does
        # not guarantee the in-container agents died. Emit once for the bulk
        # operation — this is exactly where an operator believes a fleet of
        # yolo agents is dead.
        from uxon.domain.container import kill_caveat

        caveat = kill_caveat(cfg.container)
        if caveat is not None and any(r["action"] == "killed" for r in results):
            print(f"note: {caveat}")
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

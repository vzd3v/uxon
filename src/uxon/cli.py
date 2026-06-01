# SPDX-License-Identifier: MIT
"""uxon: readable wrapper for terminal AI coding agent sessions."""

from __future__ import annotations

import os
import sys

import uxon.app.attach as attach_app
import uxon.app.doctor as doctor_app
import uxon.app.kill as kill_app
import uxon.app.listing as listing_app
import uxon.app.new as new_app
import uxon.app.run as run_app
from uxon.domain.args import SUBCOMMANDS, USAGE, ParsedArgs
from uxon.domain.authz import canonical
from uxon.domain.config import (
    Config,
)
from uxon.domain.constants import VALID_AGENT_IDS
from uxon.errors import eprint, fail
from uxon.infra import (
    config_loader,
    identity,
    sessions_probe,
    version_probe,
)

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None

# The TUI context-builder lives in ``uxon.tui.bridge`` and is imported
# lazily inside ``do_interactive`` so module-load of ``uxon.cli`` never
# pulls ``uxon.tui`` / Textual (~90 ms saved on ``uxon version`` /
# ``uxon list``; latency invariant #7).


def parse_list_args(argv: list[str]) -> ParsedArgs:
    from uxon.infra.audit import extract_correlation_id, set_correlation_id

    corr_id, argv = extract_correlation_id(argv)
    if corr_id:
        set_correlation_id(corr_id)
    all_users = False
    json_out = False
    all_hosts = False
    host: str | None = None
    i = 0
    extras: list[str] = []
    while i < len(argv):
        token = argv[i]
        if token == "--all-users":
            all_users = True
        elif token == "--json":
            json_out = True
        elif token == "--all-hosts":
            all_hosts = True
        elif token == "--host":
            i += 1
            if i >= len(argv):
                fail("--host requires a host name")
            host = argv[i]
        else:
            extras.append(token)
        i += 1
    if extras:
        fail(f"unknown args for list: {' '.join(extras)}")
    if host is not None and all_hosts:
        fail("--host and --all-hosts are mutually exclusive")
    return ParsedArgs(
        action="list",
        all_users=all_users,
        json_output=json_out,
        host=host,
        all_hosts=all_hosts,
        audit_correlation_id=corr_id,
    )


def parse_run_like(argv: list[str], action: str, target_id: str | None = None) -> ParsedArgs:
    parsed = ParsedArgs(action=action, target_id=target_id)
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in ("-w", "--worktree"):
            if parsed.git_remote:
                fail("cannot combine -w with --git-remote")
            i += 1
            if i >= len(argv):
                fail(f"{token} requires a branch value")
            parsed.worktree_branch = argv[i]
        elif token == "--dry-run":
            parsed.dry_run = True
        elif token == "--attach-existing":
            if action != "new":
                fail(f"{token} is only supported with 'new' / '-n'")
            if parsed.repeat_mode == "new":
                fail("cannot combine --attach-existing with --new-session")
            parsed.repeat_mode = "attach"
        elif token == "--new-session":
            if action != "new":
                fail(f"{token} is only supported with 'new' / '-n'")
            if parsed.repeat_mode == "attach":
                fail("cannot combine --new-session with --attach-existing")
            parsed.repeat_mode = "new"
        elif token in ("--dsp", "--dangerously-skip-permissions", "--dap", "-dap", "-dsp"):
            # --dsp is the canonical short form; --dap, -dap, -dsp are legacy synonyms
            if parsed.permission_mode == "auto":
                fail("--dsp and --auto are mutually exclusive")
            parsed.permission_mode = "yolo"
        elif token == "--auto":
            if parsed.permission_mode == "yolo":
                fail("--dsp and --auto are mutually exclusive")
            parsed.permission_mode = "auto"
        elif token == "--agent":
            i += 1
            if i >= len(argv):
                fail("--agent requires an id (claude|codex|cursor)")
            value = argv[i]
            if value not in VALID_AGENT_IDS:
                fail(f"--agent must be one of {VALID_AGENT_IDS}, got {value!r}")
            parsed.agent = value
        elif token == "--git-remote":
            if action != "new":
                fail(f"{token} is only supported with 'new' / '-n'")
            if parsed.no_git:
                fail("cannot combine --git-remote with --no-git")
            if parsed.worktree_branch:
                fail("cannot combine --git-remote with -w")
            i += 1
            if i >= len(argv):
                fail(f"{token} requires a profile name (or 'default')")
            parsed.git_remote = argv[i]
        elif token == "--no-git":
            if action != "new":
                fail(f"{token} is only supported with 'new' / '-n'")
            if parsed.git_remote:
                fail("cannot combine --no-git with --git-remote")
            parsed.no_git = True
        elif token == "--git-visibility":
            if action != "new":
                fail(f"{token} is only supported with 'new' / '-n'")
            i += 1
            if i >= len(argv):
                fail(f"{token} requires 'private' or 'public'")
            value = argv[i]
            if value not in ("private", "public"):
                fail(f"{token} must be 'private' or 'public', got {value!r}")
            parsed.git_visibility = value
        else:
            parsed.agent_args.append(token)
        i += 1
    return parsed


def _parse_kill_extras(rest: list[str], target_id: str) -> ParsedArgs:
    """Parse the arg tail of ``uxon kill <id> [...]``.

    Shared between the subcommand form and the ``-k`` / ``--kill`` short
    form so both surfaces accept exactly the same flag set.

    Recognised flags:
        --dry-run        : print the would-be argv (or SSH command),
                           do not execute.
        --force          : skip the interactive confirmation prompt.
        --json           : emit a wire-schema envelope on stdout.
        --user <name>    : kill a session belonging to a different
                           launch user (per-target NOPASSWD required).
        --host <alias>   : route the kill to a configured remote peer
                           over SSH.

    Unknown flags fail loudly. Returns a fully populated
    :class:`ParsedArgs` with ``action="kill"``.
    """
    from uxon.infra.audit import extract_correlation_id, set_correlation_id

    corr_id, rest = extract_correlation_id(rest)
    if corr_id:
        set_correlation_id(corr_id)
    dry = False
    force = False
    json_out = False
    user: str | None = None
    host: str | None = None
    extras: list[str] = []
    i = 0
    while i < len(rest):
        token = rest[i]
        if token == "--dry-run":
            dry = True
        elif token == "--force":
            force = True
        elif token == "--json":
            json_out = True
        elif token == "--user":
            i += 1
            if i >= len(rest):
                fail("--user requires a name")
            user = rest[i]
        elif token == "--host":
            i += 1
            if i >= len(rest):
                fail("--host requires a host name")
            host = rest[i]
        else:
            extras.append(token)
        i += 1
    if extras:
        fail(f"unknown args for kill: {' '.join(extras)}")
    return ParsedArgs(
        action="kill",
        target_id=target_id,
        dry_run=dry,
        force=force,
        json_output=json_out,
        user=user,
        host=host,
        audit_correlation_id=corr_id,
    )


def _parse_attach_extras(rest: list[str], target_id: str) -> ParsedArgs:
    """Parse the arg tail of ``uxon attach <id> [...]``.

    Symmetric to :func:`_parse_kill_extras` but without
    ``--all-users``, ``--json``, ``--force``. ``--host`` requires
    ``--user`` — implicit peer-login-user defaults invite
    "where did this attach actually go?" surprises.
    """
    from uxon.infra.audit import extract_correlation_id, set_correlation_id

    corr_id, rest = extract_correlation_id(rest)
    if corr_id:
        set_correlation_id(corr_id)
    dry = False
    user: str | None = None
    host: str | None = None
    extras: list[str] = []
    i = 0
    while i < len(rest):
        token = rest[i]
        if token == "--dry-run":
            dry = True
        elif token == "--user":
            i += 1
            if i >= len(rest):
                fail("--user requires a name")
            user = rest[i]
        elif token == "--host":
            i += 1
            if i >= len(rest):
                fail("--host requires a host name")
            host = rest[i]
        else:
            extras.append(token)
        i += 1
    if extras:
        fail(f"unknown args for attach: {' '.join(extras)}")
    if host is not None and user is None:
        fail(
            "attach --host requires --user (peer owns authorisation; "
            "pass the target user explicitly)"
        )
    return ParsedArgs(
        action="attach",
        target_id=target_id,
        dry_run=dry,
        user=user,
        host=host,
        audit_correlation_id=corr_id,
    )


def parse_subcommand(argv: list[str]) -> ParsedArgs:
    cmd = argv[0]
    if cmd == "version":
        json_out = "--json" in argv[1:]
        extras = [a for a in argv[1:] if a != "--json"]
        if extras:
            fail(f"unknown args for version: {' '.join(extras)}")
        return ParsedArgs(action="version", json_output=json_out)
    if cmd == "doctor":
        json_out = "--json" in argv[1:]
        # Stage 10c — opt-in ``--remote`` flag walks back the
        # AGENTS.md "doctor doesn't probe remote_hosts" rule under
        # explicit operator gesture. See ``do_doctor`` for the
        # rationale + the AGENTS.md addendum.
        all_remote = "--remote" in argv[1:]
        extras = [a for a in argv[1:] if a not in {"--json", "--remote"}]
        if extras:
            fail(f"unknown args for doctor: {' '.join(extras)}")
        # Reuse ``all_hosts`` as the bool carrier — adding a separate
        # field for one flag isn't worth widening ``ParsedArgs``.
        return ParsedArgs(action="doctor", json_output=json_out, all_hosts=all_remote)
    if cmd == "run":
        return parse_run_like(argv[1:], "run")
    if cmd == "list":
        return parse_list_args(argv[1:])
    if cmd == "kill-all":
        dry = "--dry-run" in argv[1:]
        force = "--force" in argv[1:]
        json_out = "--json" in argv[1:]
        extras = [a for a in argv[1:] if a not in {"--dry-run", "--force", "--json"}]
        if extras:
            fail(f"unknown args for kill-all: {' '.join(extras)}")
        return ParsedArgs(action="kill-all", dry_run=dry, force=force, json_output=json_out)
    if cmd in ("attach", "kill"):
        if len(argv) < 2:
            fail(f"{cmd} requires an identifier")
        target = argv[1]
        if cmd == "kill":
            return _parse_kill_extras(argv[2:], target)
        return _parse_attach_extras(argv[2:], target)
    if cmd == "new":
        if len(argv) < 2:
            fail("new requires a name")
        name = argv[1]
        return parse_run_like(argv[2:], "new", target_id=name)
    fail(f"unknown subcommand: {cmd}")
    raise AssertionError("unreachable")


def parse_args(argv: list[str]) -> ParsedArgs:
    if not argv:
        if identity.is_interactive_tty():
            return ParsedArgs(action="interactive")
        print(USAGE)
        raise SystemExit(0)
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        raise SystemExit(0)
    if argv[0] in ("-V", "--version"):
        json_out = "--json" in argv[1:]
        extras = [a for a in argv[1:] if a != "--json"]
        if extras:
            fail(f"unknown args for version: {' '.join(extras)}")
        return ParsedArgs(action="version", json_output=json_out)
    if argv[0] in ("-l", "--list"):
        return parse_list_args(argv[1:])
    if argv[0] in ("-a", "--attach"):
        if len(argv) < 2:
            fail("attach requires an identifier")
        return _parse_attach_extras(argv[2:], argv[1])
    if argv[0] in ("-k", "--kill"):
        if len(argv) < 2:
            fail("kill requires an identifier")
        return _parse_kill_extras(argv[2:], argv[1])
    if argv[0] in ("--killall",):
        dry = "--dry-run" in argv[1:]
        force = "--force" in argv[1:]
        json_out = "--json" in argv[1:]
        extras = [a for a in argv[1:] if a not in {"--dry-run", "--force", "--json"}]
        if extras:
            fail(f"unknown args for kill-all: {' '.join(extras)}")
        return ParsedArgs(action="kill-all", dry_run=dry, force=force, json_output=json_out)
    if argv[0] in ("-n", "--new"):
        if len(argv) < 2:
            fail("new requires a name")
        return parse_run_like(argv[2:], "new", target_id=argv[1])
    if argv[0] in SUBCOMMANDS:
        return parse_subcommand(argv)
    if not argv[0].startswith("-"):
        fail(f"unknown command: {argv[0]}\n{USAGE}")
    # Convenience: support `uxon --model sonnet` as run passthrough.
    return parse_run_like(argv, "run")


def format_version() -> str:
    """Render the ``uxon version`` display string.

    Delegates to :func:`uxon.infra.version_probe.format_version`, which
    composes the impure git/FS readers with the pure
    :func:`uxon.domain.version.format_version` builder.
    """
    return version_probe.format_version()


def do_interactive(cfg: Config, launch_user: str) -> int:
    try:
        from uxon import tui as uxon_tui
    except ImportError:
        try:
            from uxon.tui.hints import TEXTUAL_MISSING_HINT

            eprint(TEXTUAL_MISSING_HINT)
        except ImportError:
            eprint(
                "uxon: interactive mode requires the 'textual' package "
                "(pip install --user textual)."
            )
        return 1
    # Lazy import: the bridge pulls in Textual-adjacent tui modules, which
    # must stay out of ``import uxon.cli`` (latency invariant #7).
    from uxon.tui.bridge import build_tui_context

    cwd = canonical(os.getcwd())
    # Hand the TUI a skeleton ctx so the first frame paints immediately;
    # the real ctx is loaded by a worker once the app is mounted.
    ctx = build_tui_context(cfg, launch_user, cwd, skeleton=True)
    return uxon_tui.run(ctx)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    try:
        args = parse_args(argv)
    except SystemExit as ex:
        # argparse always raises SystemExit with an int (0 for --help,
        # 2 for parse errors); guard the typed-as-``str | int | None`` shape.
        return int(ex.code) if isinstance(ex.code, int) else (0 if ex.code is None else 2)
    from uxon.infra import audit as _audit

    try:
        cfg = config_loader.load_config(os.getcwd())
    except SystemExit as ex:
        # Bug 5 part 2 — convert config-load failure into an audit event.
        # The audit module's compile-time defaults (``enabled=True``,
        # ``syslog_facility="user"``) are what fires here; ``configure()``
        # has not run yet because ``config_loader.load_config`` is what feeds it.
        # Spec says ``error`` carries the first 256 chars of the error
        # text; ``fail()`` stashes the human-readable message on
        # ``ex.uxon_msg`` so we don't end up logging just the int exit
        # code (``str(SystemExit(1)) == "1"``).
        err_msg = getattr(ex, "uxon_msg", None) or str(ex.code)
        _audit.audit(
            "config.error",
            outcome="error",
            path=str(config_loader.repo_config_path()),
            error=err_msg[:256],
        )
        raise
    _audit.configure(
        enabled=cfg.audit_enabled,
        syslog_facility=cfg.audit_syslog_facility,
        subcmd=args.action,
    )
    caller_user = identity.resolve_caller_user()
    launch_user = identity.resolve_launch_user(cfg, caller_user)

    # CLI preflight: probe for tmux and required agents on actions that
    # actually shell out to tmux. ``interactive`` is excluded so the TUI
    # mount stays fast — the TUI runs its own async probe in the
    # background and surfaces the same hints in line.
    if args.action in {"run", "new", "attach", "list", "kill", "kill-all"}:
        from uxon.infra import probes as uxon_probes

        report = uxon_probes.probe_host(launch_user)
        if report.tmux.path is None:
            fail(f"tmux is not installed.\n{report.tmux.install_hint}", 1)
        # Stash the report on ``args`` so downstream ``resolve_agent_id``
        # reuses it instead of paying a second sudo round-trip. Agent
        # install-gating is owned by ``resolve_agent_id`` — it now
        # validates the picked candidate (including ``--agent`` and
        # ``agents.default``) against this same report.
        args.host_report = report

    if args.action == "interactive":
        return do_interactive(cfg, launch_user)
    if args.action == "version":
        if args.json_output:
            listing_app._emit_json("version", version_probe._version_data())
            return 0
        print(format_version())
        return 0
    # Emit ``cli.start`` *after* the ``version`` early-return (the version
    # subcommand is a no-op probe; we don't litter the audit trail with it)
    # but *before* the ``doctor`` early-return — ``uxon doctor`` is a
    # substantive operator gesture that belongs in the audit history.
    _audit.audit(
        "cli.start",
        flags=_audit._sanitize_flags(list(argv or [])),
        agents_enabled=list(cfg.enabled_agents),
        enable_all_users_list=cfg.enable_all_users_list,
        audit_enabled=True,
        allowed_roots_count=len(cfg.allowed_roots),
        remote_hosts_count=len(cfg.remote_hosts),
    )
    if args.action == "doctor":
        return doctor_app.do_doctor(
            cfg,
            caller_user,
            launch_user,
            canonical(os.getcwd()),
            json_output=args.json_output,
            probe_remote=args.all_hosts,
        )
    if args.action == "run":
        return run_app.do_run(args, cfg, launch_user)
    if args.action == "list":
        if args.host is not None:
            return listing_app._do_list_host(args, cfg)
        if args.all_hosts:
            return listing_app._do_list_all_hosts(args, cfg, launch_user)
        # Peer-inbound branch: a peer-collector invocation arrives with
        # ``SSH_CONNECTION`` set and neither ``--host`` nor ``--all-hosts``.
        # Fires *after* those early-returns so a caller-side
        # ``uxon list --host`` does not double-emit on its own host.
        # Spec line 306: when peer-inbound, ``list.remote.in`` replaces
        # ``list.peek`` ("instead of"), so we suppress the latter on
        # this code path.
        #
        # Spec line 207-209: state-changing events emit on **both**
        # success and failure paths.  ``list.remote.in`` is no
        # exception: the previous shape (single ``outcome=ok`` emit at
        # the top, before the all-users-disabled gate) lost the denied
        # outcome — a peer that refused ``--all-users`` recorded a
        # stale ``ok``.  Emit at outcome boundaries instead: once on
        # the all-users-disabled denial, once on success after the
        # gate passes (or for the own-only branch).
        peer_inbound = bool(os.environ.get("SSH_CONNECTION"))
        # ``correlation_id`` for ``list.remote.in`` is auto-injected by
        # ``audit()`` from module state when the parser popped
        # ``--audit-correlation-id`` off argv.  See spec §"Correlation
        # across hosts" ("omitted rather than synthesized").
        list_scope = "all-users" if args.all_users else "own"
        if args.all_users:
            if not cfg.enable_all_users_list:
                # Stable error tag. The remote-host aggregator's
                # fallback detector greps for this exact substring to
                # decide whether to retry with the legacy ``list
                # --json`` (own-only) command.
                if peer_inbound:
                    _audit.audit(
                        "list.remote.in",
                        outcome="denied",
                        scope=list_scope,
                    )
                fail("uxon-error: all-users-disabled (enable_all_users_list = false in config)")
            scope_users, scope_skipped = listing_app._resolve_all_users_scope(cfg, launch_user)
            # ``list.peek`` / ``list.remote.in`` fires only after the
            # gate passes — placement ensures we never log a successful
            # peek for a denied invocation.
            if peer_inbound:
                _audit.audit(
                    "list.remote.in",
                    scope=list_scope,
                )
            else:
                _audit.audit(
                    "list.peek",
                    scope_users=scope_users,
                    scope_skipped=list(scope_skipped),
                )
            sessions = sessions_probe.collect_sessions(scope_users, cfg)
            if args.json_output:
                listing_app._emit_json(
                    "list",
                    listing_app._list_data(
                        cfg,
                        sessions,
                        scope_users,
                        all_users=True,
                        scope_skipped=scope_skipped,
                    ),
                )
                return 0
            rc = listing_app.print_list(cfg, sessions, scope_users, show_user=True)
            listing_app._emit_scope_skipped_hint(scope_skipped)
            return rc
        # Own-only branch: no gate, single success emit on the peer
        # side (the local-side ``list`` does not produce a ``list.peek``
        # for its own user — that's by spec, only ``--all-users``
        # local enumeration triggers ``list.peek``).
        if peer_inbound:
            _audit.audit(
                "list.remote.in",
                scope=list_scope,
            )
        scope_users = [launch_user]
        sessions = sessions_probe.collect_sessions(scope_users, cfg)
        if args.json_output:
            listing_app._emit_json(
                "list", listing_app._list_data(cfg, sessions, scope_users, all_users=False)
            )
            return 0
        return listing_app.print_list(cfg, sessions, scope_users, show_user=False)
    if args.action == "attach":
        return attach_app.do_attach(args, cfg, launch_user)
    if args.action == "kill":
        return kill_app.do_kill(args, cfg, launch_user)
    if args.action == "kill-all":
        return kill_app.do_kill_all(args, cfg, launch_user)
    if args.action == "new":
        return new_app.do_new(args, cfg, launch_user)
    fail(f"unsupported action: {args.action}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

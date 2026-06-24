# SPDX-License-Identifier: MIT
"""Impure argv parsing layer for the ``uxon`` CLI.

Sits above the pure :mod:`uxon.domain.args` data model: these
functions read TTY state and pop the audit correlation-id out of argv
(side-effecting), then build a :class:`~uxon.domain.args.ParsedArgs`.
Kept free of ``uxon.tui`` / ``textual`` imports so module-load of
``uxon.cli`` stays fast (latency invariant #7).
"""

from __future__ import annotations

from uxon.domain.args import SUBCOMMANDS, USAGE, ParsedArgs
from uxon.errors import fail
from uxon.infra import identity


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
        elif token == "--mode":
            i += 1
            if i >= len(argv):
                fail("--mode requires an id")
            # No catalog in scope at parse time — the id is validated against
            # the chosen agent's modes in ``_build_tmux_launch_request`` (which
            # has ``cfg``/``spec``), where an unknown id fails listing the
            # agent's valid modes.
            parsed.permission_mode = argv[i]
        elif token == "--agent":
            i += 1
            if i >= len(argv):
                fail("--agent requires an id")
            fail("--agent is no longer a uxon selector; use --profile <id> instead")
        elif token == "--profile":
            i += 1
            if i >= len(argv):
                fail("--profile requires an id")
            parsed.profile = argv[i]
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
    form so both accept the same flags: ``--dry-run``, ``--force``,
    ``--json``, ``--user <name>`` (per-target NOPASSWD required),
    ``--host <alias>`` (route over SSH). Unknown flags fail loudly.
    Returns a populated :class:`ParsedArgs` with ``action="kill"``.
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

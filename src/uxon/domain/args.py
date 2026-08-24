# SPDX-License-Identifier: MIT
"""Parsed-args record + usage/subcommand vocabulary.

``ParsedArgs`` is the pure, typed result of CLI parsing; ``USAGE`` /
``SUBCOMMANDS`` are the static parsing vocabulary. The parser *functions*
that produce a :class:`ParsedArgs` are impure (audit-correlation side
effects, TTY probing) and live in the composition root.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from uxon.domain.host_report import HostReport

USAGE = """Usage:
  uxon                              (interactive session picker if TTY, else this help)
  uxon -h | --help
  uxon -V | --version
  uxon [run] [-w <branch>] [--dry-run] [--profile <id>] [--mode <id>] [agent-flags...]
  uxon new <name> [-w <branch>] [--attach-existing|--new-session] [--dry-run] [--profile <id>] [--mode <id>]
                 [--git-remote <profile>|default | --no-git] [--git-visibility private|public]
                 [agent-flags...]
  uxon doctor [--remote] [--json]
  uxon list [--all-users] [--host <name>|--all-hosts] [--json]
  uxon version [--json]
  uxon attach <id> [--user <name>] [--host <alias>] [--dry-run]
  uxon kill <id> [--user <name>] [--host <alias>] [--force] [--dry-run] [--json]
  uxon kill-all [--force] [--dry-run] [--json]
  uxon --killall [--force] [--dry-run] [--json]
  uxon -l [--all-users] [--host <name>|--all-hosts] [--json]
  uxon -a <id> [--user <name>] [--host <alias>] [--dry-run]
  uxon -k <id> [--user <name>] [--host <alias>] [--force] [--dry-run] [--json]
  uxon -n <name> [-w <branch>] [--attach-existing|--new-session] [--dry-run] [--profile <id>] [--mode <id>]
                [--git-remote <profile>|default | --no-git] [--git-visibility private|public]
                [agent-flags...]

Notes:
  - Without '-w', 'new' creates <new_project_root>/<name> (default ~/projects) and runs there.
  - With '-w <branch>', 'new' uses repo inside <new_project_root>/<name> (no cwd fallback).
  - With '-w <branch>' on 'run', uses the git repo at cwd.
  - Repeating 'new' for the same plain project or worktree asks whether to attach or start a new parallel session.
  - Use '--attach-existing' or '--new-session' to bypass that prompt explicitly.
  - Non-interactive repeat handling can be pinned via UXON_REPEAT_NONINTERACTIVE_POLICY or config.
  - Unknown flags in run/new are passed to the selected terminal agent.
  - --profile <id> selects a launch profile.
  - --mode <id> selects a permission mode from the chosen agent's catalog
    (unset picks the agent's first mode); an unknown id fails listing valid modes.
  - ID accepts: session name (with/without configured session_prefix), unique prefix, or active pane PID.
  - 'list' shows sessions for the current effective launch user; '--all-users' shows configured session_users.
  - Session IDs are human-readable: <prefix><stem>@<profile>, <prefix><stem>@<profile>-2 (default prefix is 'uxon-').
  - uxon uses a dedicated tmux socket per launch user by default.
  - '--git-remote <profile>' creates a remote repo before launch,
    using the named profile from config.toml. 'default' picks
    the selected launch profile's default git-remote profile. Without the flag, no git is touched.
"""


SUBCOMMANDS = {"run", "list", "attach", "kill", "kill-all", "new", "version", "doctor"}


@dataclass
class ParsedArgs:
    action: str
    target_id: str | None = None
    worktree_branch: str | None = None
    repeat_mode: str | None = None
    dry_run: bool = False
    force: bool = False
    all_users: bool = False
    profile: str | None = None  # None = resolve from path/global/auto launch policy
    permission_mode: str | None = None  # None = use the agent's first (default) mode
    agent_args: list[str] = field(default_factory=list)
    git_remote: str | None = None  # profile name, or "default", or None
    no_git: bool = False  # explicit "do not touch git" (redundant if --git-remote absent)
    git_visibility: str | None = None  # "private" | "public" | None (use profile default)
    json_output: bool = False  # --json: emit machine-readable wire-schema envelope on stdout
    host: str | None = None  # --host <name>: route 'list' / 'kill' to one configured remote peer
    all_hosts: bool = False  # --all-hosts: aggregate local + every configured remote peer
    user: str | None = None  # --user <name>: target a different launch user (kill)
    # Internal peer-protocol flag — propagated by callers to peers so a
    # cross-host operation appears in both audit trails with the same UUID.
    # Stripped from argv by :func:`uxon.infra.audit.extract_correlation_id` before
    # the per-parser walk sees it; never surfaces in ``--help``.
    audit_correlation_id: str | None = None
    # Populated by ``main()``'s non-launch preflight and by selected TUI
    # paths. Launch profile resolution reuses it only when it was probed for
    # the same effective launch user.
    host_report: HostReport | None = None


def owned_option_flags(args: ParsedArgs) -> list[str]:
    """Return only parsed Uxon option names for the ``cli.start`` audit event.

    Values and ``agent_args`` are deliberately excluded: either may contain an
    agent prompt or credential.  The event records which Uxon controls were
    requested, not a reconstruction of the command line.
    """
    fields = (
        (args.worktree_branch is not None, "--worktree"),
        (args.repeat_mode == "attach", "--attach-existing"),
        (args.repeat_mode == "new", "--new-session"),
        (args.dry_run, "--dry-run"),
        (args.force, "--force"),
        (args.all_users, "--all-users"),
        (args.profile is not None, "--profile"),
        (args.permission_mode is not None, "--mode"),
        (args.git_remote is not None, "--git-remote"),
        (args.no_git, "--no-git"),
        (args.git_visibility is not None, "--git-visibility"),
        (args.json_output, "--json"),
        (args.host is not None, "--host"),
        (args.all_hosts, "--all-hosts"),
        (args.user is not None, "--user"),
    )
    return [name for enabled, name in fields if enabled]

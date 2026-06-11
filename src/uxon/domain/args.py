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
  uxon [run] [-w <branch>] [--dry-run] [--dsp] [claude-flags...]
  uxon new <name> [-w <branch>] [--attach-existing|--new-session] [--dry-run] [--dsp]
                 [--git-remote <profile>|default | --no-git] [--git-visibility private|public]
                 [claude-flags...]
  uxon doctor
  uxon list [--all-users]
  uxon version
  uxon attach <id>
  uxon kill <id> [--user <name>] [--host <alias>] [--force] [--dry-run] [--json]
  uxon kill-all [--force] [--dry-run]
  uxon --killall [--force] [--dry-run]
  uxon -l [--all-users]
  uxon -a <id>
  uxon -k <id> [--user <name>] [--host <alias>] [--force] [--dry-run] [--json]
  uxon -n <name> [-w <branch>] [--attach-existing|--new-session] [--dry-run] [--dsp]
                [--git-remote <profile>|default | --no-git] [--git-visibility private|public]
                [claude-flags...]

Notes:
  - Without '-w', 'new' creates <new_project_root>/<name> (default ~/projects) and runs there.
  - With '-w <branch>', 'new' uses repo inside <new_project_root>/<name> (no cwd fallback).
  - With '-w <branch>' on 'run', uses the git repo at cwd.
  - Repeating 'new' for the same plain project or worktree asks whether to attach or start a new parallel session.
  - Use '--attach-existing' or '--new-session' to bypass that prompt explicitly.
  - Non-interactive repeat handling can be pinned via UXON_REPEAT_NONINTERACTIVE_POLICY or config.
  - Unknown flags in run/new are passed to 'claude'.
  - --dsp is short for --dangerously-skip-permissions (legacy synonyms: --dap, -dap, -dsp).
  - ID accepts: session name (with/without configured session_prefix), unique prefix, or active pane PID.
  - 'list' shows sessions for the current effective launch user; '--all-users' shows configured session_users.
  - Session IDs are human-readable: <prefix><stem>@<agent>, <prefix><stem>@<agent>-2 (default prefix is 'uxon-').
  - uxon uses a dedicated tmux socket per launch user by default.
  - '--git-remote <profile>' creates a remote repo before launching claude,
    using the named profile from config.toml. 'default' picks
    default_git_remote_profile. Without the flag, no git is touched.
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
    agent: str | None = None  # None = use cfg.default_agent
    permission_mode: str = "normal"  # "normal" | "auto" | "yolo"
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
    # Populated by ``main()``'s preflight when it probes the host for
    # tmux. Downstream ``resolve_agent_id`` reuses it to install-gate
    # the picked agent without a second probe. ``None`` everywhere
    # else (interactive/version paths, TUI-side ParsedArgs
    # construction in ``_plan_tui_*_agent``).
    host_report: HostReport | None = None

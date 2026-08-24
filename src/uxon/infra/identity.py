# SPDX-License-Identifier: MIT
"""Caller / launch-user identity + sudo command prefixes.

Resolves who the process is running as, who sessions should launch as,
and builds the ``sudo -iu`` / ``sudo -niu`` prefixes the launch and
listing paths wrap their commands with. Impure: reads ``pwd``, ``os``
env/uid, and probes the filesystem.
"""

from __future__ import annotations

import os
import pwd
import subprocess
import sys

from uxon.domain.config import Config
from uxon.infra.config_loader import normalize_user_list
from uxon.infra.run import run_query


def process_user() -> str:
    return pwd.getpwuid(os.getuid()).pw_name


def resolve_caller_user() -> str:
    current_user = process_user()
    if current_user != "root":
        return current_user
    sudo_user = os.environ.get("SUDO_USER", "").strip()
    if sudo_user and sudo_user != "root":
        return sudo_user
    return current_user


def resolve_launch_user(cfg: Config, caller_user: str) -> str:
    mapped = cfg.launch_user_by_caller.get(caller_user, "").strip()
    if mapped:
        return mapped
    if cfg.default_launch_mode == "caller":
        return caller_user
    return cfg.default_launch_user


def resolve_all_session_users(cfg: Config, current_launch_user: str) -> list[str]:
    users = normalize_user_list(cfg.session_users + [current_launch_user])
    if not users:
        return [current_launch_user]
    return users


def command_prefix_for_user(cfg: Config, target_user: str) -> list[str]:
    """Interactive sudo prefix used by the launch path.

    Used by ``run`` / ``new`` / ``attach`` and the launch-time
    helpers that run while a TTY is available — sudo's ``-i`` runs the
    target's login shell so PATH / HOME / nvm / direnv set up the same
    way they would for a real interactive login. Without ``-n``, an
    unreachable target prompts for a password (or fails with a clear
    "a password is required" message), which is the correct UX at
    launch time.

    For background work where no TTY exists — listing, probing, the
    TUI's session-collection passes — use
    :func:`nonint_command_prefix_for_user` instead so a missing
    NOPASSWD grant fails fast rather than blocking on a prompt.
    """
    from uxon.infra.execution import command_prefix

    return command_prefix(cfg, target_user, interactive=True)


def nonint_command_prefix_for_user(cfg: Config, target_user: str) -> list[str]:
    """Non-interactive sudo prefix for listing / probing / TUI polling.

    Same as :func:`command_prefix_for_user` but adds ``-n`` so sudo
    refuses to prompt. Used wherever the caller does not have a TTY
    available — listing other users' sessions, the TUI background
    refresh, capability probes — so a missing NOPASSWD grant returns
    a non-zero exit immediately rather than blocking on a hidden
    password prompt.
    """
    from uxon.infra.execution import command_prefix

    return command_prefix(cfg, target_user, interactive=False)


def probe_cwd_writable(cfg: Config, target_user: str, target_dir: str) -> bool:
    """Return True if ``target_user`` has write access to ``target_dir``.

    Same-user fast path uses ``os.access`` so the TUI common case
    (no sudo, uxon running as the launch user) is instant. Cross-user
    case shells out via :func:`command_prefix_for_user` so the same
    ``sudo -iu`` mechanism that actually launches the agent is what
    gates the row — if sudo isn't available the probe correctly
    returns False, matching the launch behaviour. Treated as a yes/no:
    any subprocess error is "no".
    """
    if not os.path.isdir(target_dir):
        return False
    from uxon.infra.execution import resolve_target

    if process_user() == target_user and resolve_target(cfg, target_user).backend.kind == "local":
        return os.access(target_dir, os.W_OK | os.X_OK)
    cmd = nonint_command_prefix_for_user(cfg, target_user) + ["test", "-w", target_dir]
    try:
        cp = run_query(
            cmd,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return cp.returncode == 0


def is_interactive_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()

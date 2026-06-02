# SPDX-License-Identifier: MIT
"""TUI launch/new planning use-cases.

These compose ``app.launch`` / ``app.new`` / ``app.agent_select`` and the
``infra.*`` adapters into LaunchRequests for the TUI's launch flows. They
import **zero** ``uxon.tui`` symbols — they were previously mis-placed in
``tui/bridge.py`` and belong in the use-case (``app/``) layer. The TUI
bridge methods (``on_launch_*``) delegate here.
"""

from __future__ import annotations

import os

import uxon.app.agent_select as agent_select
import uxon.app.launch as launch_app
import uxon.app.new as new_app
from uxon.domain.args import ParsedArgs
from uxon.domain.authz import canonical
from uxon.domain.config import Config
from uxon.domain.session import (
    allocate_session_name,
    compatible_indexed_sessions,
    session_stem_for_path,
    session_stem_for_worktree,
)
from uxon.errors import fail
from uxon.infra import identity, process, sessions_probe, tmux


def _plan_tui_run_agent(
    cfg: Config,
    launch_user: str,
    cwd: str,
    agent_id: str,
    mode_id: str,
    worktree: tuple[str, str] | None = None,
):
    """Build a LaunchRequest for the TUI "New session in current folder" action.

    Mirrors :func:`do_run` minus the terminal handoff: gates via
    :func:`ensure_launch_target_allowed` (writable + ``allowed_roots``
    whitelist when configured), allocates a session name, returns a
    LaunchRequest. The agent and permission mode are picked by the TUI
    callback before this is called — no probe needed here.

    When ``worktree`` is ``(repo_root, branch)`` the session stem is the
    repo-qualified :func:`session_stem_for_worktree` (§2.5) — identical to
    the stem the worktree-aware probe derives — instead of the
    basename-only :func:`session_stem_for_path`. ``cwd`` is the worktree
    path in that case; for a plain (primary / non-git) target ``worktree``
    is ``None`` and the basename stem is used unchanged.
    """
    launch_app.ensure_launch_target_allowed(cfg, launch_user, cwd)
    target_dir = cwd
    if worktree is not None:
        repo_root, branch = worktree
        session_stem = session_stem_for_worktree(repo_root, branch)
    else:
        session_stem = session_stem_for_path(target_dir)
    sessions = sessions_probe.collect_sessions([launch_user], cfg)
    session = allocate_session_name(
        session_stem, agent_id, target_dir, sessions, prefix=cfg.session_prefix
    )
    args = ParsedArgs(action="run", agent=agent_id, permission_mode=mode_id)
    return tmux._build_tmux_launch_request(
        target_dir, session, args, cfg, None, launch_user, server_running=bool(sessions)
    )


def _resolve_tui_project_dir(cfg: Config, launch_user: str, name: str) -> str:
    """Shared validation + directory creation for both TUI project flows.

    Returns the canonical absolute path; raises via ``fail()`` if ``name``
    is malformed, the parent is not writable, or the path violates a
    non-empty ``allowed_roots`` whitelist.
    """
    if "/" in name or name in (".", ".."):
        fail(f"invalid name: {name}")
    project_dir = canonical(os.path.join(cfg.new_project_root, name))
    new_app.ensure_new_project_target_allowed(cfg, launch_user, project_dir)
    process.run_cmd(identity.command_prefix_for_user(launch_user) + ["mkdir", "-p", project_dir])
    return project_dir


def _plan_tui_existing_session_or_launch(
    cfg: Config,
    launch_user: str,
    project_dir: str,
    name: str,
    args: ParsedArgs,
):
    """Allocate + launch a fresh session under ``project_dir``.

    Shared tail of both TUI project flows. The TUI is the sole owner of
    the attach-vs-launch decision now: it probes via
    :func:`sessions_probe.probe_tui_compatible_sessions` after the operator picks
    agent+mode, surfaces the choice in a modal, and routes "attach" to
    :func:`on_attach` directly. By the time this planner runs we already
    know the operator wants a new (parallel) session — the only thing
    left to do is allocate the next available index and emit the launch
    request. :func:`compatible_indexed_sessions` is still called for its
    path-safety side effect (it ``fail()``-s if a same-named session
    points outside ``project_dir``).
    """
    session_stem = session_stem_for_path(project_dir)
    compatibility_root = project_dir
    _agent = agent_select.resolve_agent_id(
        cfg, launch_user, args.agent or None, report=args.host_report
    )
    args.agent = _agent
    sessions = sessions_probe.collect_sessions([launch_user], cfg)
    # Path-safety side effect — raises via fail() on a path mismatch.
    compatible_indexed_sessions(
        session_stem,
        _agent,
        compatibility_root,
        sessions,
        prefix=cfg.session_prefix,
        legacy_prefixes=cfg.legacy_session_prefixes,
    )
    sessions_probe.repeat_guardrail_for_legacy_socket(
        cfg, launch_user, session_stem, compatibility_root
    )
    session = allocate_session_name(
        session_stem, _agent, compatibility_root, sessions, prefix=cfg.session_prefix
    )
    return tmux._build_tmux_launch_request(
        project_dir, session, args, cfg, None, launch_user, server_running=bool(sessions)
    )


def _plan_tui_create_new_agent(
    cfg: Config,
    launch_user: str,
    name: str,
    agent_id: str,
    mode_id: str,
    git_profile: str,
):
    """Build a LaunchRequest for the TUI "Create new project" flow.

    Creates the project directory (if missing), optionally creates the
    git remote, and — when a compatible session already exists — forces
    ``attach`` semantics (the TUI cannot safely prompt via stdin inside
    a blessed context). ``git_profile`` is the (possibly empty) name of
    a ``[[git_remote_profiles]]`` entry; when set this calls
    :func:`_do_create_git_remote`. The "Open existing project" flow must
    never call this — see :func:`_plan_tui_open_existing_agent`.
    """
    project_dir = _resolve_tui_project_dir(cfg, launch_user, name)
    args = ParsedArgs(
        action="new",
        target_id=name,
        agent=agent_id,
        permission_mode=mode_id,
        git_remote=git_profile or None,
        repeat_mode="attach",
    )
    if args.git_remote:
        new_app._do_create_git_remote(args, cfg, launch_user, project_dir, name, None)
    return _plan_tui_existing_session_or_launch(cfg, launch_user, project_dir, name, args)


def _plan_tui_open_existing_agent(
    cfg: Config,
    launch_user: str,
    name: str,
    agent_id: str,
    mode_id: str,
):
    """Build a LaunchRequest for the TUI "Open existing project" flow.

    By construction this function has **no** ``git_profile`` parameter
    and never calls :func:`_do_create_git_remote`: opening an existing
    project must not have any git side effect, regardless of
    ``git_create_enabled`` or profile configuration.
    """
    project_dir = _resolve_tui_project_dir(cfg, launch_user, name)
    args = ParsedArgs(
        action="new",
        target_id=name,
        agent=agent_id,
        permission_mode=mode_id,
        git_remote=None,
        repeat_mode="attach",
    )
    return _plan_tui_existing_session_or_launch(cfg, launch_user, project_dir, name, args)

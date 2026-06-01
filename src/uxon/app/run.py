# SPDX-License-Identifier: MIT
"""``uxon run`` use-case: launch an agent in the current directory or worktree.

Impure: gates write access, shells out to git/tmux, emits audit events.
"""

from __future__ import annotations

import os
import shlex

import uxon.app.agent_select as agent_select
import uxon.app.launch as launch_app
from uxon.domain.args import ParsedArgs
from uxon.domain.authz import canonical
from uxon.domain.config import Config
from uxon.domain.session import allocate_session_name, session_stem_for_path
from uxon.errors import fail
from uxon.infra import git, process, sessions_probe, tmux


def do_run(args: ParsedArgs, cfg: Config, launch_user: str) -> int:
    cwd = canonical(os.getcwd())
    launch_app.ensure_launch_target_allowed(cfg, launch_user, cwd)
    branch = args.worktree_branch
    if branch:
        repo_root = git.git_repo_root_nonint_as_user(cwd, launch_user)
        if not repo_root:
            fail(f"run -w must be run inside a git repository readable by {launch_user}")
        # Normalise to the PRIMARY working tree so a worktree-from-worktree
        # anchors to the main repo, not a nested one (§8).
        primary = git.git_common_dir_root_as_user(cwd, launch_user)
        if primary:
            repo_root = primary
        launch_app.ensure_launch_target_allowed(cfg, launch_user, repo_root)
        _agent = agent_select.resolve_agent_id(
            cfg, launch_user, args.agent, report=args.host_report
        )
        args.agent = _agent
        # plan_worktree_launch gates the worktree path, runs git worktree
        # add, copies includes, emits worktree.create + session.new, and
        # returns the launch request. In dry-run it prints the git plan and
        # does no side effects (Task 11).
        req = launch_app.plan_worktree_launch(
            cfg,
            launch_user,
            repo_root,
            branch,
            _agent,
            args.permission_mode,
            agent_args=args.agent_args,
            dry_run=args.dry_run,
        )
        if args.dry_run:
            print(f"launch_user={shlex.quote(launch_user)}")
            print(f"exec {shlex.join(req.cmd)}")
            return 0
        for pre in req.prelaunch:
            process.run_cmd(list(pre))
        os.execvp(req.cmd[0], list(req.cmd))
        return 0
    target_dir = cwd
    session_stem = session_stem_for_path(target_dir)
    compatibility_root = target_dir
    _agent = agent_select.resolve_agent_id(cfg, launch_user, args.agent, report=args.host_report)
    # Pin the resolved id back to ``args.agent`` so the downstream
    # ``tmux._build_tmux_launch_request`` does not re-derive it from
    # ``cfg.default_agent`` (which can disagree with auto-mode pick).
    args.agent = _agent
    sessions = sessions_probe.collect_sessions([launch_user], cfg)
    session = allocate_session_name(
        session_stem, _agent, compatibility_root, sessions, prefix=cfg.session_prefix
    )
    from uxon.infra import audit as _audit

    _audit.audit(
        "session.new",
        agent=_agent,
        project=target_dir,
        branch=branch or "",
        session=session,
        dry_run=args.dry_run,
    )
    try:
        return tmux.launch_in_tmux(
            target_dir, session, args, cfg, branch, launch_user, server_running=bool(sessions)
        )
    except Exception as exc:
        _audit.audit(
            "session.new",
            outcome="error",
            agent=_agent,
            project=target_dir,
            branch=branch or "",
            session=session,
            dry_run=args.dry_run,
            error=str(exc)[:256],
        )
        raise

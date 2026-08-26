# SPDX-License-Identifier: MIT
"""``uxon run`` use-case: launch an agent in the current directory or worktree.

Impure: gates write access, shells out to git/tmux, emits audit events.
"""

from __future__ import annotations

import os
import shlex

import uxon.app.launch as launch_app
import uxon.app.launch_profile as launch_profile_app
from uxon.domain.args import ParsedArgs
from uxon.domain.config import Config
from uxon.domain.session import allocate_session_name, session_stem_for_path
from uxon.errors import fail
from uxon.infra import git, process, sessions_probe, tmux
from uxon.infra.worktrees import compute_worktree_path


def do_run(args: ParsedArgs, cfg: Config, caller_user: str) -> int:
    # Keep the caller path lexical until the launch user is known. Git and
    # launch-profile resolution canonicalize it inside that user's execution
    # backend; resolving it on the controller can cross the wrong mount view.
    cwd = os.path.normpath(os.path.abspath(os.getcwd()))
    branch = args.worktree_branch
    if branch:
        seed_user = launch_profile_app.preflight_launch_user(cfg, caller_user, args.profile)
        seed_repo_root = git.git_repo_root_nonint_as_user(cfg, cwd, seed_user)
        if not seed_repo_root:
            fail(f"run -w must be run inside a git repository readable by {seed_user}")
        seed_primary = git.git_common_dir_root_as_user(cfg, cwd, seed_user)
        if seed_primary:
            seed_repo_root = seed_primary
        worktree_path = compute_worktree_path(
            repo_root=seed_repo_root, branch=branch, worktree_root=cfg.worktree_root
        )
        resolved = launch_profile_app.resolve_launch_profile(
            cfg,
            caller_user,
            args.profile,
            worktree_path,
            args.permission_mode,
            target_may_not_exist=True,
            report=args.host_report,
        )
        launch_user = resolved.launch_user
        repo_root = git.git_repo_root_nonint_as_user(cfg, cwd, launch_user)
        if not repo_root:
            fail(f"run -w must be run inside a git repository readable by {launch_user}")
        primary = git.git_common_dir_root_as_user(cfg, cwd, launch_user)
        if primary:
            repo_root = primary
        launch_app.ensure_launch_target_allowed(cfg, launch_user, repo_root)
        final_worktree_path = compute_worktree_path(
            repo_root=repo_root, branch=branch, worktree_root=cfg.worktree_root
        )
        if final_worktree_path != worktree_path:
            resolved = launch_profile_app.resolve_launch_profile(
                cfg,
                caller_user,
                args.profile,
                final_worktree_path,
                args.permission_mode,
                target_may_not_exist=True,
                report=args.host_report,
            )
            next_launch_user = resolved.launch_user
            if next_launch_user != launch_user:
                launch_user = next_launch_user
                repo_root = git.git_repo_root_nonint_as_user(cfg, cwd, launch_user)
                if not repo_root:
                    fail(f"run -w must be run inside a git repository readable by {launch_user}")
                primary = git.git_common_dir_root_as_user(cfg, cwd, launch_user)
                if primary:
                    repo_root = primary
                launch_app.ensure_launch_target_allowed(cfg, launch_user, repo_root)
                if (
                    compute_worktree_path(
                        repo_root=repo_root,
                        branch=branch,
                        worktree_root=cfg.worktree_root,
                    )
                    != final_worktree_path
                ):
                    fail("worktree target changed after resolving launch user")
            else:
                launch_user = next_launch_user
        # plan_worktree_launch gates the worktree path, runs git worktree
        # add, copies includes, emits worktree.create + session.new, and
        # returns the launch request. In dry-run it prints the git plan and
        # does no side effects.
        req = launch_app.plan_worktree_launch(
            cfg,
            caller_user,
            resolved,
            repo_root,
            branch,
            requested_profile=args.profile,
            agent_args=args.agent_args,
            dry_run=args.dry_run,
        )
        if args.dry_run:
            print(f"launch_user={shlex.quote(launch_user)}")
            print(f"exec {shlex.join(req.cmd)}")
            return 0
        if req.managed is not None:
            tmux.prepare_managed_launch(req)
        else:
            for pre in req.prelaunch:
                process.run_cmd(list(pre))
        # Lane B — interactive terminal handoff: ``execvp`` replaces this
        # image with the agent/tmux client, which keeps the controlling
        # terminal. Bypasses ``Popen``/the loop guard by construction.
        os.execvp(req.cmd[0], list(req.cmd))
        return 0

    resolved = launch_profile_app.resolve_launch_profile(
        cfg,
        caller_user,
        args.profile,
        cwd,
        args.permission_mode,
        report=args.host_report,
    )
    launch_user = resolved.launch_user
    target_dir = resolved.canonical_target
    launch_app.ensure_launch_target_allowed(cfg, launch_user, target_dir)
    session_stem = session_stem_for_path(target_dir)
    compatibility_root = target_dir
    snapshot = sessions_probe.collect_current_session_snapshot(cfg, launch_user)
    sessions = list(snapshot.sessions)
    session = allocate_session_name(
        session_stem,
        resolved.profile.id,
        compatibility_root,
        sessions,
        prefix=cfg.session_prefix,
    )
    if not args.dry_run:
        # Probe and, when allowed, prepare the workload runtime before exec.
        launch_app.ensure_runtime_ready(cfg, target_dir, resolved)
    return tmux.launch_in_tmux(
        target_dir,
        session,
        args,
        cfg,
        branch,
        resolved_profile=resolved,
        server_running=snapshot.server_state == "running",
        active_sessions=sessions,
    )

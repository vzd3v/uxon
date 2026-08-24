# SPDX-License-Identifier: MIT
"""``uxon new`` use-case: create a project directory (or worktree) and launch.

Impure: probes write access, shells out to git/tmux, emits audit events.
Also home to the new-project authorization gate
(:func:`is_new_project_target_allowed` / :func:`ensure_new_project_target_allowed`),
a variant of the launch gate in :mod:`uxon.app.launch` for the create-new
flow where the target does not exist yet.
"""

from __future__ import annotations

import os
import shlex

import uxon.app.attach as attach_app
import uxon.app.launch as launch_app
import uxon.app.launch_profile as launch_profile_app
import uxon.app.repeat as repeat_app
from uxon.domain.args import ParsedArgs
from uxon.domain.authz import is_under_allowed_roots
from uxon.domain.config import Config
from uxon.domain.launch_profiles import GitRemotePolicy
from uxon.domain.session import (
    allocate_session_name,
    choose_attach_session,
    compatible_indexed_sessions,
    session_stem_for_path,
    session_stem_for_worktree,
)
from uxon.errors import eprint, fail
from uxon.infra import git, identity, process, sessions_probe, tmux
from uxon.infra.worktrees import compute_worktree_path


def is_new_project_target_allowed(cfg: Config, launch_user: str, project_dir: str) -> bool:
    """Return True if ``project_dir`` may be created by ``uxon new``.

    Variant of :func:`uxon.app.launch.is_launch_target_allowed` for the
    create-new flow: the target itself does not exist yet, so we check the
    parent's write access (typically ``cfg.new_project_root``) plus
    the same whitelist policy. With empty ``cfg.allowed_roots`` the
    whitelist is bypassed and a writable parent suffices.
    """
    parent = os.path.dirname(project_dir) or "/"
    if not identity.probe_cwd_writable(cfg, launch_user, parent):
        return False
    return is_under_allowed_roots(cfg, project_dir)


def ensure_new_project_target_allowed(cfg: Config, launch_user: str, project_dir: str) -> None:
    """Raise variant of :func:`is_new_project_target_allowed`.

    Splits the failure reasons so the user sees whether the parent is
    unwritable or whether the path is outside ``allowed_roots``.
    """
    parent = os.path.dirname(project_dir) or "/"
    if not identity.probe_cwd_writable(cfg, launch_user, parent):
        fail(f"no write access to {parent} for {launch_user}")
    if not is_under_allowed_roots(cfg, project_dir):
        eprint("uxon: new project directory must be under one of:")
        for base in cfg.allowed_roots:
            eprint(f"uxon:   - {base}")
        fail(f"got: {project_dir}")


def do_new(args: ParsedArgs, cfg: Config, caller_user: str) -> int:
    name = args.target_id
    if not name:
        fail("new requires a name")
    if "/" in name or name in (".", ".."):
        fail(f"invalid name: {name}")
    project_dir = launch_profile_app.canonical_intended_target(
        os.path.join(cfg.new_project_root, name)
    )
    branch = args.worktree_branch
    if branch:
        if args.git_remote:
            fail("new -w does not support --git-remote")
        if not os.path.isdir(project_dir):
            fail(
                "new -w requires an existing project directory: "
                f"{project_dir} (create it first with 'uxon -n {name}')"
            )
        seed_user = launch_profile_app.preflight_launch_user(cfg, caller_user, args.profile)
        seed_repo_root = git.git_repo_root_as_user(cfg, project_dir, seed_user)
        if not seed_repo_root:
            fail(
                "new -w requires a git repository (checked as launch user "
                f"{seed_user}) in {project_dir}"
            )
        seed_primary = git.git_common_dir_root_as_user(cfg, project_dir, seed_user)
        if seed_primary:
            seed_repo_root = seed_primary
        compatibility_root = compute_worktree_path(
            repo_root=seed_repo_root, branch=branch, worktree_root=cfg.worktree_root
        )
        resolved = launch_profile_app.resolve_launch_profile(
            cfg,
            caller_user,
            args.profile,
            compatibility_root,
            args.permission_mode,
            target_may_not_exist=True,
            report=args.host_report,
        )
        launch_user = resolved.launch_user
        ensure_new_project_target_allowed(cfg, launch_user, project_dir)
        repo_root = git.git_repo_root_as_user(cfg, project_dir, launch_user)
        if not repo_root:
            fail(
                "new -w requires a git repository (checked as launch user "
                f"{launch_user}) in {project_dir}"
            )
        # Normalise worktree-from-worktree to the primary repo (§8).
        primary = git.git_common_dir_root_as_user(cfg, project_dir, launch_user)
        if primary:
            repo_root = primary
        launch_app.ensure_launch_target_allowed(cfg, launch_user, repo_root)
        # uxon-managed worktree sessions live AT the worktree path (§2.5),
        # so both the stem and the compatibility root are derived from the
        # worktree, not the repo root.
        session_stem = session_stem_for_worktree(repo_root, branch)
        final_compatibility_root = compute_worktree_path(
            repo_root=repo_root, branch=branch, worktree_root=cfg.worktree_root
        )
        if final_compatibility_root != compatibility_root:
            resolved = launch_profile_app.resolve_launch_profile(
                cfg,
                caller_user,
                args.profile,
                final_compatibility_root,
                args.permission_mode,
                target_may_not_exist=True,
                report=args.host_report,
            )
            next_launch_user = resolved.launch_user
            if next_launch_user != launch_user:
                launch_user = next_launch_user
                ensure_new_project_target_allowed(cfg, launch_user, project_dir)
                repo_root = git.git_repo_root_as_user(cfg, project_dir, launch_user)
                if not repo_root:
                    fail(
                        "new -w requires a git repository (checked as launch user "
                        f"{launch_user}) in {project_dir}"
                    )
                primary = git.git_common_dir_root_as_user(cfg, project_dir, launch_user)
                if primary:
                    repo_root = primary
                launch_app.ensure_launch_target_allowed(cfg, launch_user, repo_root)
                session_stem = session_stem_for_worktree(repo_root, branch)
                if (
                    compute_worktree_path(
                        repo_root=repo_root,
                        branch=branch,
                        worktree_root=cfg.worktree_root,
                    )
                    != final_compatibility_root
                ):
                    fail("worktree target changed after resolving launch user")
            else:
                launch_user = next_launch_user
            compatibility_root = final_compatibility_root
        target_desc = f"{repo_root} (worktree {branch})"
        sessions = sessions_probe.collect_sessions([launch_user], cfg)
        existing = compatible_indexed_sessions(
            session_stem,
            resolved.profile.id,
            compatibility_root,
            sessions,
            prefix=cfg.session_prefix,
            legacy_prefixes=cfg.legacy_session_prefixes,
        )
        if existing:
            attach_target = choose_attach_session(
                existing,
                session_stem,
                resolved.profile.id,
                prefix=cfg.session_prefix,
                legacy_prefixes=cfg.legacy_session_prefixes,
            )
            decision = repeat_app.resolve_repeat_decision(
                args.repeat_mode, cfg, target_desc, attach_target, existing
            )
            if decision == "attach":
                from uxon.infra import audit as _audit

                _audit.audit(
                    "session.attach",
                    session=attach_target.name,
                    target_user=launch_user,
                    profile=attach_target.profile,
                    agent=attach_target.agent,
                )
                return attach_app.attach_session(attach_target, cfg, launch_user, args.dry_run)
        # No existing session, or decision == "new": create + launch via the
        # single worktree planner (gates the path, runs git worktree add,
        # copies includes, emits worktree.create + session.new).
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
        for pre in req.prelaunch:
            process.run_cmd(list(pre))
        if req.managed is not None:
            tmux.prepare_managed_launch(req)
        # Lane B — interactive terminal handoff: ``execvp`` replaces this
        # image with the agent/tmux client, which keeps the controlling
        # terminal. Bypasses ``Popen``/the loop guard by construction.
        os.execvp(req.cmd[0], list(req.cmd))
        return 0

    resolved = launch_profile_app.resolve_launch_profile(
        cfg,
        caller_user,
        args.profile,
        project_dir,
        args.permission_mode,
        git_remote_selector=args.git_remote,
        target_may_not_exist=True,
        report=args.host_report,
    )
    launch_user = resolved.launch_user
    project_dir = resolved.canonical_target
    if args.git_remote and not cfg.git_create_enabled:
        fail(
            "git_create_enabled=false in config; either flip it on in "
            "config/config.toml or drop --git-remote"
        )
    ensure_new_project_target_allowed(cfg, launch_user, project_dir)
    target_dir = project_dir
    if args.dry_run:
        mkdir_cmd = identity.command_prefix_for_user(cfg, launch_user) + ["mkdir", "-p", target_dir]
        print(f"mkdir= {shlex.join(mkdir_cmd)}")
    else:
        process.run_cmd(
            identity.command_prefix_for_user(cfg, launch_user) + ["mkdir", "-p", target_dir]
        )
        resolved = launch_profile_app.revalidate_launch_profile(
            cfg,
            caller_user,
            resolved,
            target_dir,
            requested_profile=args.profile,
            mode_id=args.permission_mode,
            git_remote_selector=args.git_remote,
        )
        launch_user = resolved.launch_user
        target_dir = resolved.canonical_target
    session_stem = session_stem_for_path(target_dir)
    compatibility_root = target_dir
    target_desc = target_dir
    if args.git_remote:
        _do_create_git_remote(
            args,
            cfg,
            launch_user,
            project_dir,
            name,
            branch,
            resolved.profile.id,
            resolved.git_remote,
        )

    sessions = sessions_probe.collect_sessions([launch_user], cfg)
    existing = compatible_indexed_sessions(
        session_stem,
        resolved.profile.id,
        compatibility_root,
        sessions,
        prefix=cfg.session_prefix,
        legacy_prefixes=cfg.legacy_session_prefixes,
    )
    if existing:
        attach_target = choose_attach_session(
            existing,
            session_stem,
            resolved.profile.id,
            prefix=cfg.session_prefix,
            legacy_prefixes=cfg.legacy_session_prefixes,
        )
        decision = repeat_app.resolve_repeat_decision(
            args.repeat_mode, cfg, target_desc, attach_target, existing
        )
        if decision == "attach":
            # Same physical operation as ``do_attach`` for an existing
            # session — emit the same event before ``attach_session``'s
            # execvp (Bug 7 — audit fires before the image is replaced).
            from uxon.infra import audit as _audit

            _audit.audit(
                "session.attach",
                session=attach_target.name,
                target_user=launch_user,
                profile=attach_target.profile,
                agent=attach_target.agent,
            )
            try:
                return attach_app.attach_session(attach_target, cfg, launch_user, args.dry_run)
            except Exception as exc:
                _audit.audit(
                    "session.attach",
                    outcome="error",
                    session=attach_target.name,
                    target_user=launch_user,
                    profile=attach_target.profile,
                    agent=attach_target.agent,
                    error=str(exc)[:256],
                )
                raise
    else:
        sessions_probe.repeat_guardrail_for_legacy_socket(
            cfg, launch_user, session_stem, compatibility_root
        )
    session = allocate_session_name(
        session_stem,
        resolved.profile.id,
        compatibility_root,
        sessions,
        prefix=cfg.session_prefix,
    )
    from uxon.infra import audit as _audit

    _audit.audit(
        "session.new",
        profile=resolved.profile.id,
        agent=resolved.agent.id,
        target_user=launch_user,
        project=target_dir,
        branch=branch or "",
        session=session,
        dry_run=args.dry_run,
    )
    if not args.dry_run:
        # Probe + (auto) start/create the container before exec when enabled.
        launch_app.ensure_runtime_ready(cfg, target_dir, resolved)
    try:
        return tmux.launch_in_tmux(
            target_dir,
            session,
            args,
            cfg,
            branch,
            resolved_profile=resolved,
            server_running=bool(sessions),
            active_sessions=sessions,
        )
    except Exception as exc:
        _audit.audit(
            "session.new",
            outcome="error",
            profile=resolved.profile.id,
            agent=resolved.agent.id,
            target_user=launch_user,
            project=target_dir,
            branch=branch or "",
            session=session,
            dry_run=args.dry_run,
            error=str(exc)[:256],
        )
        raise


def _do_create_git_remote(
    args: ParsedArgs,
    cfg: Config,
    launch_user: str,
    project_dir: str,
    repo_name: str,
    branch: str | None,
    launch_profile_id: str,
    git_remote_policy: GitRemotePolicy,
) -> None:
    """Resolve the selected profile and drive the creation orchestrator.

    Fails (via :func:`fail`) on invalid combinations — the CLI is
    strictly non-interactive, so mismatches are surfaced as errors
    rather than prompts.
    """
    # Callers gate on ``if args.git_remote:`` before dispatching here.
    assert args.git_remote is not None, "_do_create_git_remote called without --git-remote"
    git_remote_selector = args.git_remote
    if branch:
        fail("--git-remote is not supported together with -w <branch>")
    if not cfg.git_create_enabled:
        fail(
            "git_create_enabled=false in config; either flip it on in "
            "config/config.toml or drop --git-remote"
        )
    if not cfg.git_remote_profiles:
        fail(
            "no git_remote_profiles configured; add at least one "
            "[[git_remote_profiles]] entry to config/config.toml"
        )

    from uxon.domain import git_profiles as uxon_git_profiles
    from uxon.gitremote import create as uxon_git_create

    try:
        profile = uxon_git_profiles.resolve_profile_selector(
            cfg.git_remote_profiles,
            git_remote_selector,
            git_remote_policy.default_profile,
        )
    except uxon_git_profiles.ProfileError as exc:
        fail(str(exc))
    if profile.name not in git_remote_policy.allowed_profiles:
        valid = ", ".join(git_remote_policy.allowed_profiles) or "(none)"
        fail(
            f"git remote profile {profile.name!r} is not allowed for this launch profile; "
            f"valid profiles: {valid}"
        )

    if args.git_visibility:
        profile = uxon_git_profiles.GitRemoteProfile(
            name=profile.name,
            host=profile.host,
            owner=profile.owner,
            auth=profile.auth,
            creds_user=profile.creds_user,
            token_file=profile.token_file,
            visibility=args.git_visibility,
        )

    current_user = identity.process_user()
    from uxon.infra import audit as _audit

    _git_ok = False
    try:
        result = uxon_git_create.create_project_remote(
            cfg,
            profile,
            repo_name,
            project_dir,
            launch_user=launch_user,
            current_user=current_user,
            dry_run=args.dry_run,
        )
        _git_ok = True
    except uxon_git_create.CreationError as exc:
        # Audit before ``fail()`` re-raises ``SystemExit`` — the operator
        # cares more about the failure than the success.
        _audit.audit(
            "git.remote.create",
            outcome="error",
            profile=launch_profile_id,
            git_remote_profile=profile.name,
            repo=repo_name,
            creds_user=profile.creds_user or launch_user,
            launch_user=launch_user,
            rc=1,
        )
        fail(f"git remote creation failed at stage {exc.stage!r}: {exc}")
    if _git_ok:
        _audit.audit(
            "git.remote.create",
            outcome="ok",
            profile=launch_profile_id,
            git_remote_profile=profile.name,
            repo=repo_name,
            creds_user=profile.creds_user or launch_user,
            launch_user=launch_user,
            rc=0,
        )

    if args.dry_run:
        for cmd in result.commands:
            print(f"git-remote dry-run: {cmd}")
        print(f"git-remote ssh_url={result.ssh_url}")
    else:
        print(f"git remote created: {result.ssh_url}")

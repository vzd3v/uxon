# SPDX-License-Identifier: MIT
"""Launch use-cases: target authorization gate + worktree launch planner.

Impure: every function here either probes write access via
:mod:`uxon.infra.identity` or shells out to git/tmux. The pure whitelist
predicate (:func:`is_under_allowed_roots`) lives in :mod:`uxon.domain.authz`;
this module composes it with the filesystem/subprocess gates.

:func:`plan_worktree_launch` is the **single** ``git worktree add`` site
for both the CLI ``-w`` flow and the TUI new-worktree path — worktree
creation is owned here and must not be duplicated elsewhere.
"""

from __future__ import annotations

import os
import shlex

from uxon.domain.args import ParsedArgs
from uxon.domain.authz import is_under_allowed_roots
from uxon.domain.config import Config
from uxon.domain.launch_request import LaunchRequest
from uxon.domain.session import allocate_session_name, session_stem_for_worktree
from uxon.errors import eprint, fail
from uxon.infra import git, identity, process, sessions_probe, tmux
from uxon.infra.worktrees import compute_worktree_path


def is_launch_target_allowed(cfg: Config, launch_user: str, target_dir: str) -> bool:
    """Return True if ``target_dir`` is a valid place to launch an agent.

    The launch user must be able to write to it. When
    ``cfg.allowed_roots`` is non-empty, the directory must additionally
    sit under one of the listed roots — strict whitelist with no
    implicit allowance for anywhere else (``$HOME`` included). When
    ``cfg.allowed_roots`` is empty, write access is enough.

    Used by both the CLI (gating ``uxon run`` / ``uxon new -w``) and
    the TUI (deciding whether the "new session in current folder" row
    is enabled). :func:`ensure_launch_target_allowed` is the raise-on-
    failure variant with user-facing error messages.
    """
    if not os.path.isdir(target_dir):
        return False
    if not identity.probe_cwd_writable(launch_user, target_dir):
        return False
    return is_under_allowed_roots(cfg, target_dir)


def ensure_launch_target_allowed(cfg: Config, launch_user: str, target_dir: str) -> None:
    """Raise (via :func:`fail`) if ``target_dir`` isn't a valid launch
    directory under ``cfg``'s policy.

    Same predicate as :func:`is_launch_target_allowed`; this variant
    emits a specific user-facing error describing exactly what failed
    (not a directory / not writable / outside ``allowed_roots``).
    """
    if not os.path.isdir(target_dir):
        fail(f"not a directory: {target_dir}")
    if not identity.probe_cwd_writable(launch_user, target_dir):
        fail(f"no write access to {target_dir} for {launch_user}")
    if not is_under_allowed_roots(cfg, target_dir):
        eprint("uxon: directory must be under one of:")
        for base in cfg.allowed_roots:
            eprint(f"uxon:   - {base}")
        fail(f"got: {target_dir}")


def is_worktree_target_allowed(cfg: Config, launch_user: str, worktree_path: str) -> bool:
    """Return True if ``worktree_path`` may be created by uxon.

    Not-yet-exists predicate (the worktree dir does not exist yet — this
    is why ``ensure_launch_target_allowed``/``is_launch_target_allowed``,
    which hard-fail on a missing dir, cannot be used here). The *parent*
    must be writable by ``launch_user`` and the path must satisfy the
    ``allowed_roots`` whitelist when non-empty (§2.3). The parent is
    created later by the caller; here we only check policy.
    """
    parent = os.path.dirname(worktree_path) or "/"
    # The immediate parent may not exist yet (e.g. ``.uxon/worktrees`` on
    # first use); walk up to the nearest existing ancestor for the
    # write-access probe, which is what mkdir -p will actually need.
    probe_dir = parent
    while probe_dir and probe_dir != "/" and not os.path.isdir(probe_dir):
        probe_dir = os.path.dirname(probe_dir)
    if not identity.probe_cwd_writable(launch_user, probe_dir):
        return False
    return is_under_allowed_roots(cfg, worktree_path)


def plan_worktree_launch(
    cfg: Config,
    launch_user: str,
    repo_root: str,
    branch_name: str,
    agent_id: str,
    mode_id: str | None,
    *,
    agent_args: list[str] | None = None,
    dry_run: bool = False,
) -> LaunchRequest:
    """Create a uxon-managed worktree and return a launch request for it.

    Single create-and-launch planner for both the CLI ``-w`` flag (on a
    "new" decision — the CLI keeps its own attach-vs-new guard, Task 12)
    and the TUI new-worktree path (§4.1). Gates the computed path via the
    not-yet-exists predicate (§2.3); when ``worktree_base == "remote"``
    fetches origin first, else stays local and network-free (§4.5). Adds
    the worktree (``-b`` for a new branch, plain checkout for an existing
    one), copies ``.worktreeinclude`` (§2.4), writes the
    ``.git/info/exclude`` entry unless ``worktree_root`` moves the tree out
    of the repo (§2.3), then launches with the worktree-aware stem (§2.5).
    Emits **both** ``worktree.create`` and ``session.new`` for the launched
    session (§4.6, B3).

    ``dry_run=True`` (CLI ``-w --dry-run``) still gates the path and
    resolves the base ref / branch existence, but prints the git commands
    instead of running ``git worktree add`` / copy / exclude, and emits no
    audit events — no side effects. The returned LaunchRequest is built
    against the computed (not-yet-created) worktree path so the caller can
    print the exec line.
    """
    from uxon.infra import audit as _audit

    worktree_path = compute_worktree_path(
        repo_root=repo_root, branch=branch_name, worktree_root=cfg.worktree_root
    )
    # Gate the computed path BEFORE any git work or mkdir (§2.3, B1). An
    # out-of-roots worktree_root is the common failure — name the override
    # key in the error so the operator knows how to fix it. Runs in dry-run
    # too, so a misconfigured worktree_root is caught without side effects.
    if not is_worktree_target_allowed(cfg, launch_user, worktree_path):
        eprint("uxon: worktree directory must be under one of allowed_roots:")
        for base_root in cfg.allowed_roots:
            eprint(f"uxon:   - {base_root}")
        fail(
            f"got: {worktree_path} — set worktree_root to a path inside allowed_roots "
            "(and writable by the launch user) to relocate worktrees"
        )

    prefix = identity.command_prefix_for_user(launch_user)
    base = cfg.worktree_base
    branch_exists = git._branch_exists_as_user(repo_root, branch_name, launch_user)
    if branch_exists:
        add_cmd = prefix + [
            "git",
            "-C",
            repo_root,
            "worktree",
            "add",
            worktree_path,
            branch_name,
        ]
    else:
        # For remote base the real ref is resolved AFTER the fetch (below);
        # use a provisional "origin/HEAD" here so the dry-run print and the
        # request shape are correct. No fetch / set-head side effect runs in
        # this pre-guard block (those are post-dry-run-guard, non-dry-run).
        if base == "remote":
            base_ref = "origin/HEAD"
        else:
            base_ref = git._local_base_ref_as_user(repo_root, launch_user)
        add_cmd = prefix + [
            "git",
            "-C",
            repo_root,
            "worktree",
            "add",
            worktree_path,
            "-b",
            branch_name,
            base_ref,
        ]

    parent = os.path.dirname(worktree_path)
    session_stem = session_stem_for_worktree(repo_root, branch_name)
    sessions = sessions_probe.collect_sessions([launch_user], cfg)
    session = allocate_session_name(
        session_stem, agent_id, worktree_path, sessions, prefix=cfg.session_prefix
    )
    run_args = ParsedArgs(
        action="run",
        agent=agent_id,
        permission_mode=mode_id,
        agent_args=list(agent_args or []),
    )
    req = tmux._build_tmux_launch_request(
        worktree_path, session, run_args, cfg, None, launch_user, server_running=bool(sessions)
    )

    if dry_run:
        # No side effects: print the git plan, skip add/copy/exclude/audit.
        print(f"worktree_path={shlex.quote(worktree_path)}")
        if base == "remote":
            print(f"fetch={shlex.join(prefix + ['git', '-C', repo_root, 'fetch', 'origin'])}")
        print(f"worktree_add={shlex.join(add_cmd)}")
        return req

    process.run_cmd(prefix + ["mkdir", "-p", parent], check=True)
    # ``.uxon/`` exclusion must precede the first add so the in-tree
    # worktree never shows as untracked (§2.3); skipped for out-of-repo.
    if not cfg.worktree_root:
        git.write_uxon_exclude_entry(repo_root, launch_user)
    if base == "remote":
        process.run_cmd(prefix + ["git", "-C", repo_root, "fetch", "origin"], check=True)
        if not branch_exists:
            # Re-resolve the base ref post-fetch (set-head needs the fetch).
            add_cmd[-1] = git._remote_base_ref_as_user(repo_root, launch_user)
    # Run with check=False and inspect the result ourselves: process.run_cmd's own
    # failure path would surface the raw ``fatal:`` git stderr; we want a
    # friendlier, actionable message for the §8 edges.
    cp = process.run_cmd(add_cmd, check=False)
    if cp.returncode != 0:
        stderr = (cp.stderr or cp.stdout or "").strip()
        if "already checked out" in stderr:
            fail(
                f"branch {branch_name!r} is already checked out in another worktree — "
                "use that workspace row instead of creating a new one"
            )
        fail(
            f"worktree path already exists or git refused the add: {worktree_path} "
            f"(pick another branch name). git said: {stderr or 'no detail'}"
        )

    git.copy_worktreeinclude_matches(repo_root, worktree_path, launch_user)

    _audit.audit(
        "worktree.create",
        agent=agent_id,
        project=repo_root,
        branch=branch_name,
        path=worktree_path,
        base=base,
        session=session,
    )
    # §4.6 / B3: the launched session still emits its own session.new —
    # worktree.create is the ADDITIONAL lifecycle event, not a replacement.
    _audit.audit(
        "session.new",
        agent=agent_id,
        project=worktree_path,
        branch=branch_name,
        session=session,
        dry_run=False,
    )
    return req

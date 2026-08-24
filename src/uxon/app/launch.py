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
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import uxon.app.launch_profile as launch_profile_app
from uxon.domain.args import ParsedArgs
from uxon.domain.authz import is_under_allowed_roots
from uxon.domain.config import Config
from uxon.domain.launch_profiles import ResolvedLaunchProfile
from uxon.domain.launch_request import LaunchRequest
from uxon.domain.session import allocate_session_name, session_stem_for_worktree
from uxon.errors import eprint, fail
from uxon.infra import execution, git, identity, process, sessions_probe, tmux
from uxon.infra.worktrees import compute_worktree_path

if TYPE_CHECKING:
    from uxon.infra.runtime import RuntimePlan


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
    if not identity.probe_cwd_writable(cfg, launch_user, target_dir):
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
    if not identity.probe_cwd_writable(cfg, launch_user, target_dir):
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
    if not identity.probe_cwd_writable(cfg, launch_user, probe_dir):
        return False
    return is_under_allowed_roots(cfg, worktree_path)


def plan_runtime(
    cfg: Config,
    target_dir: str,
    resolved_or_launch_user: ResolvedLaunchProfile | str,
):
    """Probe the workload runtime and return the not-ready plan.

    Returns ``None`` for the built-in direct runtime. Otherwise returns a
    ``RuntimePlan`` whose ``action`` is the capability-gated verdict
    (``exec`` / ``start`` / ``create`` / ``fail``). The probe shells out as
    the launch user under a bounded timeout, so the caller MUST run this off
    the event loop in the TUI. The TUI inspects the plan to decide whether to
    prompt before running the prepare; the CLI runs it auto-if-permitted.
    """
    from uxon.infra import runtime as runtime_infra

    if not isinstance(resolved_or_launch_user, ResolvedLaunchProfile):
        # Legacy string input carries no resolved workload runtime.
        return None
    if resolved_or_launch_user.runtime_context is None:
        return None
    return runtime_infra.plan_runtime_launch_for_profile(cfg, target_dir, resolved_or_launch_user)


def _run_prepare_audited(cfg: Config, plan: RuntimePlan, target_dir: str, launch_user: str) -> None:
    """Run a workload ``RuntimePlan`` prepare, emitting ``runtime.prepare``.

    The single shared call site for both the headless (CLI) and TUI prepare,
    so every runtime start/create is audited uniformly. Emits only
    when ``run_prepare`` **acts** — i.e. ``action`` is ``start`` or ``create``.
    ``exec`` is a no-op (a ready resource) and ``fail`` is an out-of-policy
    verdict where uxon never touches the runtime — neither is a state change,
    so neither emits. For a ``start``/``create`` ``run_prepare`` raises
    ``fail()`` (SystemExit) on a runtime failure; this wraps-and-reraises with
    ``outcome=error`` first, mirroring ``session.new`` in ``app/run.py``.

    The event carries the operator-chosen runtime resource and action only.
    """
    from uxon.infra import audit as _audit
    from uxon.infra import runtime as runtime_infra

    if plan.action not in ("start", "create"):
        # ``exec`` is a no-op; ``fail`` raises the policy message without ever
        # touching the runtime — neither is an audited state change.
        runtime_infra.run_prepare(cfg, plan, target_dir, launch_user)
        return
    try:
        runtime_infra.run_prepare(cfg, plan, target_dir, launch_user)
    except BaseException as exc:
        _audit.audit(
            "runtime.prepare",
            outcome="error",
            action=plan.action,
            runtime_resource=plan.resource,
            error=str(getattr(exc, "uxon_msg", exc))[:256],
        )
        raise
    _audit.audit("runtime.prepare", action=plan.action, runtime_resource=plan.resource)


def ensure_runtime_ready(
    cfg: Config,
    target_dir: str,
    resolved_or_launch_user: ResolvedLaunchProfile | str,
) -> None:
    """Probe and, when permitted, prepare the workload runtime before launch.

    No-op for the built-in direct runtime. Headless CLI policy: a missing
    capability (``on_missing`` doesn't permit the needed start/create) errors
    with an actionable message; within capability, the start/create runs
    automatically — there is no interactive affordance off the TUI, so
    ``prompt`` degrades to auto-if-permitted (B.3). The probe + start/create
    shell out as the launch user under their own bounded timeout (separate
    from the sudo detector). uxon never removes an operator-owned resource.

    The TUI does NOT call this — it splits :func:`plan_runtime` (off-loop
    probe) from the prepare so it can show a confirm affordance when
    ``approval == "prompt"`` before any side effect.
    """
    plan = plan_runtime(cfg, target_dir, resolved_or_launch_user)
    if plan is None:
        return
    launch_user = (
        resolved_or_launch_user.launch_user
        if isinstance(resolved_or_launch_user, ResolvedLaunchProfile)
        else resolved_or_launch_user
    )
    # ``run_prepare`` is a no-op for ``exec``, ``fail``s for an out-of-policy
    # state, and runs the start/create template otherwise. The audited wrapper
    # emits ``runtime.prepare`` (start/create + outcome).
    _run_prepare_audited(cfg, plan, target_dir, launch_user)
    if isinstance(resolved_or_launch_user, ResolvedLaunchProfile):
        from uxon.infra import runtime as runtime_infra

        runtime_infra.probe_agent_in_runtime(cfg, target_dir, resolved_or_launch_user)


@dataclass(frozen=True)
class RuntimeGate:
    """TUI-facing workload-runtime decision.

    Keeps the infra ``RuntimePlan`` opaque to the TUI screen: it reads only
    these three booleans + ``message``. ``needs_prepare`` is true when a
    start/create is required before launch; ``needs_prompt`` is true only when
    that prepare must be confirmed (``approval == "prompt"``);
    ``fail_message`` is non-empty when the state is out of policy (the launch
    must abort with that message). ``prepare`` runs the side effect.
    """

    needs_prepare: bool
    needs_prompt: bool
    message: str
    fail_message: str
    prepare: Callable[[], None]


def decide_runtime_gate(
    cfg: Config,
    target_dir: str,
    resolved_or_launch_user: ResolvedLaunchProfile | str,
) -> RuntimeGate | None:
    """Probe the workload runtime and return the TUI gate without side effects.

    ``None`` for the direct runtime or when the resource is ready (``exec``).
    Otherwise the
    caller inspects the flags: ``fail_message`` → abort; else if
    ``needs_prepare`` run ``prepare`` (gated by ``needs_prompt``) before the
    launch. The capability gate is already applied inside ``plan_runtime``
    (``decide_runtime_action`` never exceeds ``on_missing``), so ``prepare``
    can never start/create beyond policy. The probe runs off the event loop in
    the caller's worker.
    """
    plan = plan_runtime(cfg, target_dir, resolved_or_launch_user)
    if plan is None:
        return None
    if plan.action == "exec":
        if isinstance(resolved_or_launch_user, ResolvedLaunchProfile):
            from uxon.infra import runtime as runtime_infra

            runtime_infra.probe_agent_in_runtime(cfg, target_dir, resolved_or_launch_user)
        return None
    if plan.action == "fail":
        return RuntimeGate(
            needs_prepare=False,
            needs_prompt=False,
            message=plan.message,
            fail_message=plan.message,
            prepare=lambda: None,
        )

    def _prepare() -> None:
        # Audited prepare — emits ``runtime.prepare`` (start/create + outcome)
        # at the single shared call site, identical to the headless path.
        launch_user = (
            resolved_or_launch_user.launch_user
            if isinstance(resolved_or_launch_user, ResolvedLaunchProfile)
            else resolved_or_launch_user
        )
        _run_prepare_audited(cfg, plan, target_dir, launch_user)
        if isinstance(resolved_or_launch_user, ResolvedLaunchProfile):
            from uxon.infra import runtime as runtime_infra

            runtime_infra.probe_agent_in_runtime(cfg, target_dir, resolved_or_launch_user)

    return RuntimeGate(
        needs_prepare=True,
        needs_prompt=(
            cfg.runtimes[resolved_or_launch_user.runtime_context.runtime_id].approval == "prompt"
            if isinstance(resolved_or_launch_user, ResolvedLaunchProfile)
            and resolved_or_launch_user.runtime_context is not None
            else False
        ),
        message=plan.message,
        fail_message="",
        prepare=_prepare,
    )


def plan_worktree_launch(
    cfg: Config,
    caller_user: str,
    resolved_profile: ResolvedLaunchProfile,
    repo_root: str,
    branch_name: str,
    *,
    requested_profile: str | None = None,
    git_remote_selector: str | None = None,
    agent_args: list[str] | None = None,
    dry_run: bool = False,
) -> LaunchRequest:
    """Create a uxon-managed worktree and return a launch request for it.

    Single create-and-launch planner for both the CLI ``-w`` flag (on a
    "new" decision — the CLI keeps its own attach-vs-new guard)
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
    launch_user = resolved_profile.launch_user
    worktree_path = execution.canonicalize_path(cfg, launch_user, worktree_path, intended=True)
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

    # Runtime path-map coverage gate: when a mapped runtime is selected
    # with a non-empty path_map but the computed worktree path falls under no
    # host prefix, the workload has no mount backing it. Fail fast naming the path
    # (and the fix) BEFORE any git work. Empty path_map = host-path-verbatim,
    # the legitimate bind-at-same-path case — never fails. Disabled → skipped.
    from uxon.domain.runtime import path_map_under_prefix

    runtime_context = resolved_profile.runtime_context
    runtime = cfg.runtimes[runtime_context.runtime_id] if runtime_context is not None else None
    if (
        runtime is not None
        and runtime.path_map
        and not path_map_under_prefix(worktree_path, runtime.path_map)
    ):
        fail(
            f"worktree path {worktree_path} is not under any runtime path_map "
            "host prefix, so the workload has no mapped path. Add a path_map "
            "entry covering it, or set worktree_root to a path that is already mapped."
        )

    prefix = identity.command_prefix_for_user(cfg, launch_user)
    base = cfg.worktree_base
    branch_exists = git._branch_exists_as_user(cfg, repo_root, branch_name, launch_user)
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
            base_ref = git._local_base_ref_as_user(cfg, repo_root, launch_user)
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
    run_args = ParsedArgs(
        action="run",
        profile=resolved_profile.profile.id,
        permission_mode=resolved_profile.mode_id,
        agent_args=list(agent_args or []),
    )

    if dry_run:
        sessions = sessions_probe.collect_sessions([launch_user], cfg)
        session = allocate_session_name(
            session_stem,
            resolved_profile.profile.id,
            worktree_path,
            sessions,
            prefix=cfg.session_prefix,
        )
        req = tmux._build_tmux_launch_request(
            worktree_path,
            session,
            run_args,
            cfg,
            None,
            resolved_profile=resolved_profile,
            server_running=bool(sessions),
            include_runtime_identity=False,
            active_sessions=sessions,
        )
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
        git.write_uxon_exclude_entry(cfg, repo_root, launch_user)
    if base == "remote":
        process.run_cmd(prefix + ["git", "-C", repo_root, "fetch", "origin"], check=True)
        if not branch_exists:
            # Re-resolve the base ref post-fetch (set-head needs the fetch).
            add_cmd[-1] = git._remote_base_ref_as_user(cfg, repo_root, launch_user)
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

    resolved_profile = launch_profile_app.revalidate_launch_profile(
        cfg,
        caller_user,
        resolved_profile,
        worktree_path,
        requested_profile=requested_profile,
        mode_id=resolved_profile.mode_id,
        git_remote_selector=git_remote_selector,
    )
    launch_user = resolved_profile.launch_user
    worktree_path = resolved_profile.canonical_target

    git.copy_worktreeinclude_matches(cfg, repo_root, worktree_path, launch_user)

    # Container readiness for the worktree tree. The worktree dir only exists
    # after the add above, so its name/{dir} can't be resolved earlier — the
    # TUI's pre-launch prompt affordance (which needs the path up front) does
    # not cover this path, so it uses the headless auto-if-permitted policy
    # documented for non-interactive worktree creation.
    ensure_runtime_ready(cfg, worktree_path, resolved_profile)

    sessions = sessions_probe.collect_sessions([launch_user], cfg)
    session = allocate_session_name(
        session_stem,
        resolved_profile.profile.id,
        worktree_path,
        sessions,
        prefix=cfg.session_prefix,
    )
    req = tmux._build_tmux_launch_request(
        worktree_path,
        session,
        run_args,
        cfg,
        None,
        resolved_profile=resolved_profile,
        server_running=bool(sessions),
        active_sessions=sessions,
    )

    _audit.audit(
        "worktree.create",
        profile=resolved_profile.profile.id,
        agent=resolved_profile.agent.id,
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
        profile=resolved_profile.profile.id,
        agent=resolved_profile.agent.id,
        target_user=launch_user,
        project=worktree_path,
        branch=branch_name,
        session=session,
        dry_run=False,
    )
    return req

"""LaunchFlow — the launch-chain controller for :class:`MainScreen`.

Holds the heavy bodies of the launch flow (session-choice probe, the
worktree-aware folder launch, the three entry points, and the settings
opener) so the Screen's ``action_*`` handlers stay thin delegators
(AGENTS.md: every ``action_*``/``on_*`` is a method *on the Screen*, but
its body may live here).

The controller is a mechanical lift of the former ``MainScreen``
methods: every reference to ``self.cfg``/``self.app``/``self.state`` etc.
is re-routed through ``self.host`` (the owning Screen). No behavior
changes — the closures, the ``app.probe_workspaces_then`` off-loop
handoff, and the ``request_launch`` ordering are preserved exactly.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from .confirm import ConfirmYesNo
from .git_profile import GitProfileScreen
from .launch_options import LaunchOptionsScreen
from .new_project import NewProjectScreen
from .session_choice import SessionChoiceScreen
from .worktree_branch import WorktreeBranchScreen

if TYPE_CHECKING:
    from ..state import MainIntent
    from .existing import ExistingProjectScreen as _ExistingProjectScreen  # noqa: F401
    from .main import MainScreen


class LaunchFlow:
    """Launch-chain controller bound to one :class:`MainScreen`.

    The Screen owns the live state (``cfg``/``state``/``_workspace_repo_root``)
    and the Textual primitives (``app.push_screen``/``request_launch``);
    this controller reaches into ``host`` for them so there is a single
    source of truth — the controller carries no copy of per-build state.
    """

    def __init__(self, host: MainScreen) -> None:
        self.host = host

    def commit_with_runtime_gate(
        self, target_dir: str, profile_id: str, mode_id: str, do_commit
    ) -> None:
        """Probe the workload runtime for ``target_dir``, then run ``do_commit``.

        The single TUI seam for workload-runtime readiness. ``do_commit`` is the
        zero-arg closure that actually launches (each entry point's existing
        ``run_off_loop(on_launch_*)`` block). Order, all off the event loop
        (§ blocking invariant):

        1. ``on_runtime_gate`` resolves the selected profile, probes its
           launch user's workload runtime, and returns a ``RuntimeGate`` (or
           ``None`` → launch straight through: disabled, or already running).
        2. ``fail_message`` (state outside ``on_missing`` policy) → notify and
           abort — uxon never exceeds the capability gate.
        3. A needed start/create: when ``needs_prompt`` (``approval ==
           "prompt"``) push a ``ConfirmYesNo`` with the pre-built message and
           run the prepare only on confirm; otherwise (``auto``) run it
           straight. Then ``do_commit``.

        The capability decision already happened inside the gate
        (``decide_runtime_action`` never exceeds ``on_missing``), so the
        prepare here can only do what policy permits — the prompt is consent,
        not authorization.
        """
        host = self.host
        gate_fn = host.cfg.on_runtime_gate

        def run_prepare_then_commit(gate) -> None:
            # The prepare shells out (start/create) — strictly off the loop.
            host.app.run_off_loop(  # type: ignore[attr-defined]
                gate.prepare,
                on_success=lambda _: do_commit(),
                on_error=lambda exc: host.app.notify(str(exc), severity="error", timeout=6),
                label="runtime_prepare",
            )

        def on_gate(gate) -> None:
            if gate is None:
                do_commit()
                return
            if gate.fail_message:
                host.app.notify(gate.fail_message, severity="error", timeout=8)
                return
            if not gate.needs_prepare:
                do_commit()
                return
            if not gate.needs_prompt:
                run_prepare_then_commit(gate)
                return

            def after_confirm(ok: bool | None) -> None:
                if ok:
                    run_prepare_then_commit(gate)

            host.app.push_screen(ConfirmYesNo(gate.message), after_confirm)

        host.app.run_off_loop(  # type: ignore[attr-defined]
            lambda: gate_fn(target_dir, profile_id, mode_id),
            on_success=on_gate,
            on_error=lambda exc: host.app.notify(
                f"Container probe failed: {exc}", severity="error", timeout=6
            ),
            label="runtime_gate",
        )

    def run_intent(self, intent: MainIntent | None) -> None:
        """Route a resolved :class:`MainIntent` to the right entry point.

        Focus moves (``intent.index``) stay on the Screen — the Textual
        ``focus``/``move_cursor`` half. Kill-all-global routes through the
        sibling :class:`KillFlow` controller.
        """
        host = self.host
        if intent is None:
            return
        if intent.index is not None:
            host._focus_index(intent.index)
        # Dispatch through the Screen's thin delegators (not this
        # controller's own methods) so a test that overrides e.g.
        # ``MainScreen._launch_cwd`` on a live screen still intercepts.
        if intent.kind == "launch-cwd":
            host._launch_cwd()
        elif intent.kind == "launch-new":
            host._launch_new()
        elif intent.kind == "launch-existing":
            host._launch_existing()
        elif intent.kind == "open-settings":
            host._open_settings()
        elif intent.kind == "kill-all-global":
            host._kill_all_global()
        elif intent.kind == "attach":
            host._attach_session(intent.user, intent.session_name)

    def attach_session(self, user: str, session_name: str) -> None:
        host = self.host
        # ``on_attach`` shells out to tmux — run it off the event loop so
        # the keystroke pump never starves (§ blocking-call invariant).
        # Snapshot the bound callback on the loop: a background refresh may
        # swap ``host.cfg`` while the worker runs.
        fn = host.cfg.on_attach
        host.app.run_off_loop(  # type: ignore[attr-defined]
            lambda: fn(user, session_name),
            on_success=lambda req: host.app.request_launch(req),  # type: ignore[attr-defined]
            on_error=lambda exc: host.app.notify(
                f"Attach failed: {exc}", severity="error", timeout=6
            ),
            label="attach",
        )

    def attach_row(self, row) -> None:
        """Attach to the session under a dashboard row (Enter / click).

        Local rows (``row.host is None``) go through ``ctx.on_attach``;
        remote rows go through ``ctx.on_remote_attach`` (SSH). The cursor
        bounds check + row lookup stay on the Screen (Textual half).
        """
        host = self.host
        if row.host is not None:
            # Remote: dispatch via ctx.on_remote_attach over SSH — a network
            # round-trip, so it MUST run off the event loop (§ invariant).
            user = row.user or host.cfg.current_user
            fn = host.cfg.on_remote_attach
            host.app.run_off_loop(  # type: ignore[attr-defined]
                lambda: fn(row.host, user, row.name),
                on_success=lambda req: host.app.request_launch(req),  # type: ignore[attr-defined]
                on_error=lambda exc: host.app.notify(
                    f"Remote attach failed: {exc}", severity="error", timeout=6
                ),
                label="remote_attach",
            )
            return
        session_user = row.user or host.cfg.current_user
        host._attach_session(session_user, row.name)

    def maybe_show_session_choice(
        self,
        *,
        target_dir: str,
        target_label: str,
        agent_id: str,
        mode_id: str,
        on_new,
        probe=None,
    ) -> None:
        """Probe for compatible existing sessions; if any, prompt the operator.

        ``target_dir`` is the absolute target path (cwd or
        ``<new_project_root>/<name>``); the CLI side canonicalises it
        before lookup. ``on_new`` is invoked when the operator chooses
        "new alongside" (or when the probe returns no matches) — it's
        the closure that actually commits the launch by calling the
        corresponding ``on_launch_*`` callback. The attach branch routes
        through the existing ``_attach_session`` so audit + LaunchRequest
        construction stay in one place.

        ``probe`` defaults to the plain path-based
        ``on_probe_existing_sessions`` (primary / non-git target). A
        worktree target passes a zero-arg closure over the worktree-aware
        probe (``on_probe_existing_worktree_sessions``) so the guard uses
        the repo-qualified stem (§2.5) — "same tmux cwd" alone would not
        match because the name-stem differs.
        """
        host = self.host
        # The probe shells out to tmux (``tmux list-sessions``) — the call
        # that used to freeze "new project" on the event loop. Run it in a
        # worker; the on-loop continuation prompts or commits (§ invariant).
        _probe = host.cfg.on_probe_existing_sessions
        probe_fn = probe if probe is not None else (lambda: _probe(target_dir, agent_id, mode_id))

        def on_probed(existing) -> None:
            if not existing:
                on_new()
                return

            launch_user = host.cfg.launch_user or host.cfg.current_user

            def after_choice(result):
                if result is None:
                    return
                action, name, user = result
                if action == "attach" and name:
                    # Route through the Screen's thin delegator so a test that
                    # overrides ``_attach_session`` on a stub host still sees
                    # the call (and audit/LaunchRequest construction stays in
                    # one place).
                    host._attach_session(user or launch_user, name)
                elif action == "new":
                    on_new()

            host.app.push_screen(
                SessionChoiceScreen(target_label=target_label, existing=existing),
                after_choice,
            )

        host.app.run_off_loop(  # type: ignore[attr-defined]
            probe_fn,
            on_success=on_probed,
            # A probe failure shouldn't silently swallow the launch — surface
            # it and abort; the operator can retry.
            on_error=lambda exc: host.app.notify(
                f"Session probe failed: {exc}", severity="error", timeout=6
            ),
            label="probe_sessions",
        )

    def begin_launch_in_folder(
        self,
        *,
        target_dir: str,
        target_label: str,
        commit_primary,
        launchable: bool | None = None,
        on_probed=None,
    ) -> None:
        """Worktree-aware launch into an existing folder (cwd or named project).

        Shared by ``_launch_cwd`` and ``_launch_existing``. Profile-specific
        launchability and workspace discovery happen after the operator picks a
        profile, inside the commit planner. Before that choice this screen uses
        the plain profile/mode picker so pinned launch-user profiles cannot be
        denied by probes run under the startup user.

        ``commit_primary(agent_id, mode_id, target_dir=None)`` is the
        folder-specific launch (``on_launch_cwd`` vs ``on_launch_existing``)
        used for the primary tree and the non-git case. ``target_dir`` is the
        resolved primary ``repo_root`` the WORKSPACE primary row carries — the
        cwd flow anchors its launch there (so the primary row lands in the
        primary tree even when the TUI was started in a linked worktree or a
        subdirectory of the repo); the non-git / 2-tuple path passes ``None``
        to launch the folder as-is.
        Worktree create / attach is available only when profile-aware workspace
        choices are supplied to the launch-options screen.
        """
        host = self.host

        def commit_existing_worktree(
            agent_id: str, mode_id: str, repo_root: str, path: str, branch: str
        ) -> None:
            # Launches into an existing worktree — the planner shells out to
            # tmux (`collect_sessions`), so it runs off the loop (§ invariant).
            fn = host.cfg.on_launch_existing_worktree

            def do_commit() -> None:
                host.app.run_off_loop(  # type: ignore[attr-defined]
                    lambda: fn(repo_root, branch, path, agent_id, mode_id),
                    on_success=lambda req: host.app.request_launch(req),  # type: ignore[attr-defined]
                    on_error=lambda exc: host.app.notify(str(exc), severity="error", timeout=6),
                    label="launch_existing_worktree",
                )

            # The worktree dir already exists, so its workload resolves up
            # front — prompt affordance applies (unlike new-worktree create).
            self.commit_with_runtime_gate(path, agent_id, mode_id, do_commit)

        def commit_new_worktree(agent_id: str, mode_id: str, repo_root: str, branch: str) -> None:
            # Creates a git worktree (`git worktree add`, possibly `git fetch`)
            # then launches — heavy subprocess work, strictly off the loop.
            fn = host.cfg.on_create_worktree
            host.app.run_off_loop(  # type: ignore[attr-defined]
                lambda: fn(repo_root, branch, agent_id, mode_id),
                on_success=lambda req: host.app.request_launch(req),  # type: ignore[attr-defined]
                on_error=lambda exc: host.app.notify(str(exc), severity="error", timeout=6),
                label="create_worktree",
            )

        def dispatch_workspace(agent_id: str, mode_id: str, choice) -> None:
            kind = choice[0]
            if kind == "primary":
                # Primary tree keeps the plain path-based planner + probe
                # (§3), but anchored on the resolved primary ``repo_root`` the
                # choice carries rather than on ``target_dir``. They coincide
                # when the TUI was started at the primary repo root, and differ
                # when it was started anywhere else (a linked worktree or a
                # subdirectory): the primary row then launches into — and
                # probes for existing sessions in — the primary tree, matching
                # the row's ``(primary)`` label, not wherever the TUI was
                # opened.
                _, primary_root = choice
                self.maybe_show_session_choice(
                    target_dir=primary_root,
                    target_label=target_label,
                    agent_id=agent_id,
                    mode_id=mode_id,
                    on_new=lambda: commit_primary(agent_id, mode_id, primary_root),
                )
                return
            if kind == "worktree":
                _, path, branch = choice
                repo_root = host._workspace_repo_root or target_dir
                # Worktree target: the attach guard uses the worktree-aware
                # probe (repo-qualified stem, §2.5), not the path-based one.
                self.maybe_show_session_choice(
                    target_dir=path,
                    target_label=branch,
                    agent_id=agent_id,
                    mode_id=mode_id,
                    on_new=lambda: commit_existing_worktree(
                        agent_id, mode_id, repo_root, path, branch
                    ),
                    probe=lambda: host.cfg.on_probe_existing_worktree_sessions(
                        path, repo_root, branch, agent_id, mode_id
                    ),
                )
                return
            # ("new", None) → prompt for a branch name, then create + launch.
            repo_root = host._workspace_repo_root or target_dir

            def after_branch(branch: str | None) -> None:
                if not branch:
                    return
                commit_new_worktree(agent_id, mode_id, repo_root, branch)

            host.app.push_screen(WorktreeBranchScreen(), after_branch)

        def after_opts(result) -> None:
            if result is None:
                return
            # B2: a 3-tuple only arrives when the WORKSPACE column was
            # shown (git target); a 2-tuple is the non-git path.
            if len(result) == 3:
                agent_id, mode_id, choice = result
                dispatch_workspace(agent_id, mode_id, choice)
                return
            agent_id, mode_id = result
            self.maybe_show_session_choice(
                target_dir=target_dir,
                target_label=target_label,
                agent_id=agent_id,
                mode_id=mode_id,
                on_new=lambda: commit_primary(agent_id, mode_id),
            )

        def push_with_workspaces(workspaces, error) -> None:
            # The primary working tree carries its own path == repo_root;
            # thread it into the screen + the dispatch closures so neither
            # has to re-resolve the repo root on the event loop (§4.2).
            host._workspace_repo_root = next(
                (w.path for w in workspaces if getattr(w, "is_primary", False)),
                target_dir,
            )
            host.app.push_screen(  # type: ignore[attr-defined]
                LaunchOptionsScreen(
                    host.cfg,
                    host.state,
                    workspaces=workspaces,
                    repo_root=host._workspace_repo_root,
                    probe_error=error,
                ),
                after_opts,
            )

        def deny() -> None:
            user = host.cfg.launch_user or host.cfg.current_user or "launch user"
            host.app.notify(
                f"Cannot launch in {target_label} as {user} "
                "(no write access, or outside allowed_roots)",
                severity="warning",
                timeout=6,
            )

        def on_probed_workspaces(resolved: bool | None, workspaces, error=None) -> None:
            # On-loop callback (off the worker thread). ``resolved is None``
            # ⟺ the caller pre-resolved launchability and no worker probe
            # ran, so ``on_probed`` (cwd slot persist) fires only on a fresh
            # probe — matching the original never-loaded-only refresh.
            if resolved is not None and on_probed is not None:
                on_probed(bool(resolved))
            if resolved is False:
                deny()
                return
            push_with_workspaces(workspaces, error)

        # Profile-specific launchability and worktree availability cannot be
        # probed until the operator picks a profile. The commit planner performs
        # the authoritative gate after that choice; until the profile-aware TUI
        # path is available, show the plain profile/mode picker without a
        # pre-selection filesystem probe.
        if on_probed is not None and launchable is not None:
            on_probed(bool(launchable))
        push_with_workspaces([], None)

    def launch_cwd(self) -> None:
        host = self.host

        def commit_primary(agent_id: str, mode_id: str, target_dir: str | None = None) -> None:
            # The planner probes tmux (`collect_sessions`) before building the
            # request — off the loop so the launch never freezes the UI.
            fn = host.cfg.on_launch_cwd

            def do_commit() -> None:
                host.app.run_off_loop(  # type: ignore[attr-defined]
                    lambda: fn(agent_id, mode_id, target_dir),
                    on_success=lambda req: host.app.request_launch(req),  # type: ignore[attr-defined]
                    on_error=lambda exc: host.app.notify(str(exc), severity="error", timeout=6),
                    label="launch_cwd",
                )

            # Container readiness (probe → prompt/auto start/create) precedes
            # the launch; ``None`` target resolves to the started-in folder.
            self.commit_with_runtime_gate(target_dir or host.cfg.cwd, agent_id, mode_id, do_commit)

        def on_probed(value: bool) -> None:
            # The cross-user / sudo probe may not have landed yet; when the
            # gate resolves it synchronously, persist into the reactive slot
            # and re-render the cwd row so its enabled state stops lying. The
            # slot getter prefers a later background-worker write, so this
            # never masks a fresher value (a legitimate ``value=None`` does
            # not retrigger — the slot is concrete after any probe attempt).
            # Persist onto the static seed; ``_cwd_writable_now`` reads it
            # until a worker probe lands on ``state.cwd_writable``.
            host.cfg.cwd_writable = value
            host._refresh_cwd_row()

        self.begin_launch_in_folder(
            target_dir=host.cfg.cwd,
            target_label=host.cfg.cwd_short or host.cfg.cwd,
            commit_primary=commit_primary,
            launchable=host._cwd_writable_now(),
            on_probed=on_probed,
        )

    def launch_new(self) -> None:
        host = self.host

        def after_opts(name: str):
            # Built WITHOUT ``workspaces`` → only ever a 2-tuple at runtime
            # (B2). The annotation covers the screen's widened result type
            # so pyright accepts the callback; ``result[:2]`` is robust if a
            # 3-tuple ever reaches here.
            def _on_opts(result: tuple[str, str] | tuple[str, str, object] | None) -> None:
                if result is None:
                    return
                agent_id, mode_id = result[0], result[1]

                def commit_new(git_profile: str) -> None:
                    # Creates the project dir (`mkdir -p`), optionally a git
                    # remote, and probes tmux — all subprocess, off the loop.
                    fn = host.cfg.on_launch_new
                    host.app.run_off_loop(  # type: ignore[attr-defined]
                        lambda: fn(name, agent_id, mode_id, git_profile),
                        on_success=lambda req: host.app.request_launch(req),  # type: ignore[attr-defined]
                        on_error=lambda exc: host.app.notify(str(exc), severity="error", timeout=6),
                        label="launch_new",
                    )

                def choose_session(git_profile: str) -> None:
                    self.maybe_show_session_choice(
                        target_dir=os.path.join(host.cfg.new_project_root, name),
                        target_label=name,
                        agent_id=agent_id,
                        mode_id=mode_id,
                        on_new=lambda: commit_new(git_profile),
                    )

                def on_git_choice(git_profile: str | None) -> None:
                    if git_profile is None:
                        return
                    choose_session(git_profile)

                def on_options(payload: tuple[list[tuple[str, str]], str]) -> None:
                    options, default_profile = payload
                    if not options:
                        choose_session("")
                        return
                    host.app.push_screen(
                        GitProfileScreen(options, default_profile=default_profile),
                        on_git_choice,
                    )

                if host.cfg.git_create_enabled:
                    host.app.run_off_loop(  # type: ignore[attr-defined]
                        lambda: host.cfg.on_git_remote_options(name, agent_id, mode_id),
                        on_success=on_options,
                        on_error=lambda exc: host.app.notify(str(exc), severity="error", timeout=6),
                        label="git_remote_options",
                    )
                else:
                    choose_session("")

            return _on_opts

        def after_name(name: str | None) -> None:
            if not name:
                return
            host.app.push_screen(LaunchOptionsScreen(host.cfg, host.state), after_opts(name))

        host.app.push_screen(NewProjectScreen(host.cfg.new_project_root), after_name)

    def launch_existing(self) -> None:
        host = self.host
        if not host.cfg.existing_projects:
            host.app.notify(
                f"No projects in {host.cfg.new_project_root}",
                severity="warning",
                timeout=4,
            )
            return

        from .existing import ExistingProjectScreen

        def after_name(name: str | None) -> None:
            if not name:
                return

            project_dir = os.path.join(host.cfg.new_project_root, name)

            def commit_primary(agent_id: str, mode_id: str, target_dir: str | None = None) -> None:
                # ``target_dir`` (the primary ``repo_root`` from the WORKSPACE
                # choice) is accepted for a uniform ``commit_primary`` signature
                # but intentionally unused here: a named project launches by
                # name, not path, so the primary tree is already its
                # ``new_project_root/<name>`` root (``project_dir``). The
                # named-project-is-a-linked-worktree case is out of scope (the
                # backlog item is cwd-only). The planner probes tmux before
                # building the request — off the loop (§ invariant).
                fn = host.cfg.on_launch_existing

                def do_commit() -> None:
                    host.app.run_off_loop(  # type: ignore[attr-defined]
                        lambda: fn(name, agent_id, mode_id),
                        on_success=lambda req: host.app.request_launch(req),  # type: ignore[attr-defined]
                        on_error=lambda exc: host.app.notify(str(exc), severity="error", timeout=6),
                        label="launch_existing",
                    )

                # The named project already exists on disk → runtime resolves
                # up front → prompt affordance applies.
                self.commit_with_runtime_gate(project_dir, agent_id, mode_id, do_commit)

            # Same worktree-aware flow as launch-cwd: a git project shows the
            # WORKSPACE column, a non-git one degrades to agent/mode only (§3).
            self.begin_launch_in_folder(
                target_dir=project_dir,
                target_label=name,
                commit_primary=commit_primary,
            )

        host.app.push_screen(
            ExistingProjectScreen(host.cfg.existing_projects, host.cfg.new_project_root),
            after_name,
        )

    def open_settings(self) -> None:
        """Push SettingsScreen with the context's callback bundle."""
        host = self.host
        from .settings import SettingsCallbacks, SettingsScreen

        cbs = SettingsCallbacks(
            get_entries=host.cfg.get_settings_entries,
            save_setting=host.cfg.on_setting_save,
            remove_setting=host.cfg.on_setting_remove,
            save_mapping=host.cfg.on_setting_save_mapping,
            get_git_remote_profile_rows=host.cfg.get_git_remote_profile_rows,
        )
        host.app.push_screen(SettingsScreen(cbs))

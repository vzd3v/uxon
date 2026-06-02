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

from ..context import CallbackError
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
        try:
            req = host.cfg.on_attach(user, session_name)
        except CallbackError as exc:
            host.app.notify(f"Attach failed: {exc}", severity="error", timeout=6)
            return
        host.app.request_launch(req)  # type: ignore[attr-defined]

    def attach_row(self, row) -> None:
        """Attach to the session under a dashboard row (Enter / click).

        Local rows (``row.host is None``) go through ``ctx.on_attach``;
        remote rows go through ``ctx.on_remote_attach`` (SSH). The cursor
        bounds check + row lookup stay on the Screen (Textual half).
        """
        host = self.host
        if row.host is not None:
            # Remote: dispatch via ctx.on_remote_attach over SSH.
            user = row.user or host.cfg.current_user
            try:
                req = host.cfg.on_remote_attach(row.host, user, row.name)
            except CallbackError as exc:
                host.app.notify(f"Remote attach failed: {exc}", severity="error", timeout=6)
                return
            host.app.request_launch(req)  # type: ignore[attr-defined]
            return
        session_user = row.user or host.cfg.current_user
        host._attach_session(session_user, row.name)

    def maybe_show_session_choice(
        self,
        *,
        target_dir: str,
        target_label: str,
        agent_id: str,
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
        try:
            existing = (
                probe()
                if probe is not None
                else host.cfg.on_probe_existing_sessions(target_dir, agent_id)
            )
        except CallbackError as exc:
            # A probe failure shouldn't silently swallow the launch.
            # Surface the message and abort — the operator can retry.
            host.app.notify(f"Session probe failed: {exc}", severity="error", timeout=6)
            return
        if not existing:
            on_new()
            return

        launch_user = host.cfg.launch_user or host.cfg.current_user

        def after_choice(result):
            if result is None:
                return
            action, name = result
            if action == "attach" and name:
                # Route through the Screen's thin delegator so a test that
                # overrides ``_attach_session`` on a stub host still sees
                # the call (and audit/LaunchRequest construction stays in
                # one place).
                host._attach_session(launch_user, name)
            elif action == "new":
                on_new()

        host.app.push_screen(
            SessionChoiceScreen(target_label=target_label, existing=existing),
            after_choice,
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

        Shared by ``_launch_cwd`` and ``_launch_existing``. Order: gate that
        ``launch_user`` may launch in ``target_dir`` (write access + inside
        ``allowed_roots``) → probe ``target_dir`` for git worktrees off the
        event loop (§4.2) → push the launch-options screen. When the folder is
        a git repo the screen shows the WORKSPACE column (primary tree +
        existing worktrees + ``+ New worktree…``); a non-git folder degrades to
        the plain agent/mode screen (§3 degradation).

        The launchability gate is the same predicate for both entry points,
        which matters now that any of them can create a worktree on disk.
        ``launchable`` is a pre-resolved value (``cwd`` passes its reactive
        slot; "open existing" passes ``None``); when ``None`` the gate's
        ``sudo`` probe runs in the SAME off-loop worker as the worktree probe
        (never on the event loop, cross-user ``sudo -iu`` would otherwise
        freeze the UI) and ``on_probed(value)`` lets the caller persist the
        result (cwd updates its slot + dashboard row — a cwd-only concern
        kept out of here).

        ``commit_primary(agent_id, mode_id)`` is the folder-specific launch
        (``on_launch_cwd`` vs ``on_launch_existing``) used for the primary tree
        and the non-git case; worktree create / attach is generic given the
        probed ``repo_root`` + branch, so it lives here once rather than being
        duplicated per entry point.
        """
        host = self.host

        def commit_existing_worktree(
            agent_id: str, mode_id: str, repo_root: str, path: str, branch: str
        ) -> None:
            try:
                req = host.cfg.on_launch_existing_worktree(
                    repo_root, branch, path, agent_id, mode_id
                )
            except CallbackError as exc:
                host.app.notify(str(exc), severity="error", timeout=6)
                return
            host.app.request_launch(req)  # type: ignore[attr-defined]

        def commit_new_worktree(agent_id: str, mode_id: str, repo_root: str, branch: str) -> None:
            try:
                req = host.cfg.on_create_worktree(repo_root, branch, agent_id, mode_id)
            except CallbackError as exc:
                host.app.notify(str(exc), severity="error", timeout=6)
                return
            host.app.request_launch(req)  # type: ignore[attr-defined]

        def dispatch_workspace(agent_id: str, mode_id: str, choice) -> None:
            kind = choice[0]
            if kind == "primary":
                # Primary tree keeps the plain path-based planner + probe
                # (§3) — launch into the folder exactly as the non-worktree
                # path does.
                self.maybe_show_session_choice(
                    target_dir=target_dir,
                    target_label=target_label,
                    agent_id=agent_id,
                    on_new=lambda: commit_primary(agent_id, mode_id),
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
                    on_new=lambda: commit_existing_worktree(
                        agent_id, mode_id, repo_root, path, branch
                    ),
                    probe=lambda: host.cfg.on_probe_existing_worktree_sessions(
                        path, repo_root, branch, agent_id
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
                on_new=lambda: commit_primary(agent_id, mode_id),
            )

        def push_with_workspaces(workspaces) -> None:
            # The primary working tree carries its own path == repo_root;
            # thread it into the screen + the dispatch closures so neither
            # has to re-resolve the repo root on the event loop (§4.2).
            host._workspace_repo_root = next(
                (w.path for w in workspaces if getattr(w, "is_primary", False)),
                target_dir,
            )
            host.app.push_screen(  # type: ignore[attr-defined]
                LaunchOptionsScreen(
                    host.cfg, host.state, workspaces=workspaces, repo_root=host._workspace_repo_root
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

        def on_probed_workspaces(resolved: bool | None, workspaces) -> None:
            # On-loop callback (off the worker thread). ``resolved is None``
            # ⟺ the caller pre-resolved launchability and no worker probe
            # ran, so ``on_probed`` (cwd slot persist) fires only on a fresh
            # probe — matching the original never-loaded-only refresh.
            if resolved is not None and on_probed is not None:
                on_probed(bool(resolved))
            if resolved is False:
                deny()
                return
            push_with_workspaces(workspaces)

        # Pre-resolved False (cwd's populated slot) gates inline — no I/O, no
        # fresh probe to persist. Otherwise the launchability ``sudo`` probe
        # rides the same off-loop worker as the worktree probe; a pre-resolved
        # True skips it (probe_launchable=None → resolved=None).
        if launchable is False:
            deny()
            return
        host.app.probe_workspaces_then(  # type: ignore[attr-defined]
            target_dir,
            on_probed_workspaces,
            probe_launchable=(
                None if launchable is True else lambda: host.cfg.on_probe_dir_launchable(target_dir)
            ),
        )

    def launch_cwd(self) -> None:
        host = self.host

        def commit_primary(agent_id: str, mode_id: str) -> None:
            try:
                req = host.cfg.on_launch_cwd(agent_id, mode_id)
            except CallbackError as exc:
                host.app.notify(str(exc), severity="error", timeout=6)
                return
            host.app.request_launch(req)  # type: ignore[attr-defined]

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

        def after_opts(name: str, git_profile: str):
            # Built WITHOUT ``workspaces`` → only ever a 2-tuple at runtime
            # (B2). The annotation covers the screen's widened result type
            # so pyright accepts the callback; ``result[:2]`` is robust if a
            # 3-tuple ever reaches here.
            def _on_opts(result: tuple[str, str] | tuple[str, str, object] | None) -> None:
                if result is None:
                    return
                agent_id, mode_id = result[0], result[1]

                def commit_new() -> None:
                    try:
                        req = host.cfg.on_launch_new(name, agent_id, mode_id, git_profile)
                    except CallbackError as exc:
                        host.app.notify(str(exc), severity="error", timeout=6)
                        return
                    host.app.request_launch(req)  # type: ignore[attr-defined]

                self.maybe_show_session_choice(
                    target_dir=os.path.join(host.cfg.new_project_root, name),
                    target_label=name,
                    agent_id=agent_id,
                    on_new=commit_new,
                )

            return _on_opts

        def after_git(name: str):
            def _on_git(git_profile: str | None) -> None:
                if git_profile is None:
                    return  # user cancelled the whole chain
                host.app.push_screen(
                    LaunchOptionsScreen(host.cfg, host.state), after_opts(name, git_profile)
                )

            return _on_git

        def after_name(name: str | None) -> None:
            if not name:
                return
            if host.cfg.git_create_enabled and host.cfg.git_remote_profile_options:
                host.app.push_screen(
                    GitProfileScreen(
                        host.cfg.git_remote_profile_options,
                        default_profile=host.cfg.default_git_remote_profile,
                    ),
                    after_git(name),
                )
            else:
                host.app.push_screen(
                    LaunchOptionsScreen(host.cfg, host.state), after_opts(name, "")
                )

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

            target_dir = os.path.join(host.cfg.new_project_root, name)

            def commit_primary(agent_id: str, mode_id: str) -> None:
                try:
                    req = host.cfg.on_launch_existing(name, agent_id, mode_id)
                except CallbackError as exc:
                    host.app.notify(str(exc), severity="error", timeout=6)
                    return
                host.app.request_launch(req)  # type: ignore[attr-defined]

            # Same worktree-aware flow as launch-cwd: a git project shows the
            # WORKSPACE column, a non-git one degrades to agent/mode only (§3).
            self.begin_launch_in_folder(
                target_dir=target_dir,
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

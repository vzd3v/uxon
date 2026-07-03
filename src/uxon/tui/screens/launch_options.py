"""LaunchOptionsScreen — pick agent (left), permission mode, workspace.

Dismiss value (variable arity, B2):
- constructed WITHOUT ``workspaces`` → a 2-tuple ``(agent_id, mode_id)``
  (the project-create / project-open flows that have no workspace column);
- constructed WITH a non-empty ``workspaces`` → a 3-tuple
  ``(agent_id, mode_id, workspace_choice)`` where ``workspace_choice`` is
  one of ``("primary", repo_root)`` / ``("worktree", path, branch)`` /
  ``("new", None)``;
- ``None`` on cancel.

Up to three panels: AGENT (left) lists enabled + available agents and is
hidden when a single agent is enabled; PERMISSION lists permission modes
for the focused agent; WORKSPACE (shown whenever ``workspaces`` is not
``None`` — i.e. the folder was probed) lists the primary working tree + one
row per existing worktree + a ``+ New worktree…`` row, or — when the probe
returned no worktrees (an empty list) — a single non-selectable row: a
``git not initialized`` hint for a git-less folder, or a ``git error: …``
row when the probe raised (``probe_error`` set).

Key map: ↑/↓ navigate within the focused panel; ←/→ cycle only the VISIBLE
panels (``next_launch_panel`` skips hidden columns). No h/j/k/l bindings —
those letters are free on ``MainScreen`` but avoided here to prevent
misfires when the modal sits above the main screen.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import ListItem, ListView, Static

from ..keymap import bindings_with_aliases
from ..state import (
    launch_commit_decision,
    launch_options_state,
    launch_profile_list_label,
    launch_profile_users_differ,
    next_launch_panel,
    pick_visible_agent,
    update_launch_options_after_availability,
)


class LaunchOptionsScreen(ModalScreen["tuple[str, str] | tuple[str, str, object] | None"]):
    DEFAULT_CSS = """
    LaunchOptionsScreen { align: center middle; }
    LaunchOptionsScreen > Horizontal {
        width: 80; height: auto; padding: 1 2;
        border: round $accent; background: $surface;
    }
    LaunchOptionsScreen Vertical { width: 1fr; }
    LaunchOptionsScreen .panel-title { text-style: bold; margin-bottom: 1; }
    LaunchOptionsScreen ListView { height: auto; min-height: 3; }
    """

    BINDINGS: ClassVar[list[Binding]] = bindings_with_aliases(
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("left", "focus_left", "Prev", show=True),
        Binding("right", "focus_right", "Next", show=True),
        Binding("enter", "commit", "Select", show=True, priority=True),
    )

    def __init__(
        self,
        cfg,
        state=None,
        workspaces: list | None = None,
        repo_root: str = "",
        probe_error: str | None = None,
    ) -> None:
        super().__init__()
        # ``cfg`` is the static rebuild snapshot (carries launch profiles +
        # the static availability seed); ``state`` is the App-owned live
        # :class:`TuiState`. Availability is read live from
        # ``state.agent_availability`` when present, falling back to the
        # ``cfg`` seed for unit tests that build a bare cfg without an
        # App. ``state`` is optional so those bare-cfg tests need not
        # construct a TuiState.
        self.cfg = cfg
        self._state = state
        # The WORKSPACE column is folder-selection only (§3). The rows are
        # built from ``workspaces`` (filled by the launch-screen worker —
        # ``repo_root`` is the primary repo root the probe
        # resolved off the event loop; ``action_commit`` reads it for the
        # ``("primary", repo_root)`` choice without re-resolving on the loop.
        #
        # Tri-state: ``None`` ⟺ the caller never probed (project-create flow)
        # → no WORKSPACE column at all; ``[]`` ⟺ probed but the folder is not a
        # git repo → the column shows a single non-selectable
        # "git not initialized" hint; a non-empty list ⟺ git target → primary
        # tree + worktrees + "+ New worktree…". Visibility keys off
        # ``is not None``; focusability/selection keys off truthiness, so the
        # hint column renders but ←/→ skips it and Enter falls through to the
        # plain 2-tuple launch.
        #
        # ``probe_error`` is set (and ``workspaces`` is ``[]``) when the git
        # probe RAISED — a real repo whose ``git worktree list`` failed, not a
        # git-less folder. The same non-interactive column then shows an error
        # row instead of the benign hint, so a genuine failure isn't
        # mislabelled as "no repo here".
        self._workspaces = None if workspaces is None else list(workspaces)
        self._probe_error = probe_error
        self._repo_root = repo_root
        # Compute the initial visible set from current availability.
        # ``_rebuild_agent_list`` re-reads on every probe-result
        # dispatch so the modal reflects fresh data without a re-open.
        opts = launch_options_state(
            enabled_agents=self._enabled_profiles(),
            default_agent=self._default_profile(),
            availability=self._availability_now(),
            catalog_ids=self._catalog_profile_ids(),
            auto_mode=self._launch_auto_mode(),
        )
        self._visible_agents = list(opts.visible_agents)
        self._single_agent = opts.single_agent
        self._active_panel = opts.active_panel
        self._current_agent = opts.current_agent
        self._panel_order = self._compute_panel_order()

    def _compute_panel_order(self) -> tuple[str, ...]:
        """Visible-column sequence for ←/→ cycling (a subset of
        ``agent``/``mode``/``workspace``). AGENT drops under a single
        agent; WORKSPACE is present only when ``workspaces`` were passed.
        """
        order: list[str] = []
        if not self._single_agent:
            order.append("agent")
        order.append("mode")
        if self._workspaces:
            order.append("workspace")
        return tuple(order)

    def _availability_now(self) -> dict:
        """Read the current availability dict from the live slot store.

        Prefers the injected ``state`` (or the App's live state when the
        screen is attached), falling back to the ``cfg`` availability
        seed for unit tests that build a bare cfg without a state.
        """
        state = self._state if self._state is not None else getattr(self.app, "state", None)
        if state is not None and state.agent_availability.value is not None:
            return state.agent_availability.value
        return self.cfg.agent_availability

    def _enabled_profiles(self) -> tuple[str, ...]:
        return tuple(getattr(self.cfg, "enabled_profiles", ()) or self.cfg.enabled_agents)

    def _default_profile(self) -> str:
        return str(getattr(self.cfg, "default_profile", "") or self.cfg.default_agent)

    def _launch_profiles(self) -> dict:
        return dict(getattr(self.cfg, "launch_profiles", {}) or {})

    def _launch_auto_mode(self) -> bool:
        return bool(getattr(self.cfg, "launch_auto_mode", False)) or not self._enabled_profiles()

    def _catalog_profile_ids(self) -> tuple[str, ...]:
        profiles = self._enabled_profiles()
        if profiles:
            return profiles
        return tuple(self.cfg.agents)

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="agent-panel"):
                yield Static("Profile", classes="panel-title")
                items = []
                avail = self._availability_now()
                profiles = self._launch_profiles()
                show_launch_user = launch_profile_users_differ(self._visible_agents, profiles)
                for idx, aid in enumerate(self._visible_agents, start=1):
                    items.append(
                        ListItem(
                            Static(
                                launch_profile_list_label(
                                    idx,
                                    aid,
                                    profiles.get(aid),
                                    avail.get(aid),
                                    show_launch_user=show_launch_user,
                                )
                            ),
                            id=f"agent-{aid}",
                        )
                    )
                yield ListView(*items, id="agent-list")
            with Vertical(id="mode-panel"):
                yield Static("Permission mode", classes="panel-title")
                yield ListView(id="mode-list")
            if self._workspaces is not None:
                with Vertical(id="workspace-panel"):
                    yield Static("Workspace", classes="panel-title")
                    yield ListView(id="workspace-list")

    async def on_mount(self) -> None:
        if not self._visible_agents:
            # No usable agent — surface a toast and dismiss; do NOT
            # force-push the unavailable modal here. The host probe
            # worker re-arms the gate via the transition path.
            self.app.notify(
                "No agents installed — install one and press 'r' to retry.",
                severity="warning",
                timeout=6,
            )
            self.dismiss(None)
            return
        agent_panel = self.query_one("#agent-panel", Vertical)
        agent_panel.display = not self._single_agent
        # Sync the agent ListView's highlighted index with _current_agent
        # so the initial Highlighted event (if any) doesn't race the
        # explicit rebuild below.
        if not self._single_agent:
            agent_list = self.query_one("#agent-list", ListView)
            try:
                agent_list.index = self._visible_agents.index(self._current_agent)
            except ValueError:
                agent_list.index = 0
        await self._rebuild_mode_list(self._current_agent)
        if self._workspaces is not None:
            await self._populate_workspace_list()
        self._reflect_focus()

    async def _populate_workspace_list(self) -> None:
        """One row per workspace + a final ``+ New worktree…`` row.

        The primary row carries a ``(primary)`` suffix and is the default
        highlight (index 0) so Enter from any panel commits the common
        "launch in the primary tree" case (§3 degradation).

        When the probe returned no worktrees (an empty list, distinct from the
        ``None`` "never probed" case that hides the column entirely) the column
        instead shows a single disabled row and omits "+ New worktree…": either
        the benign "git not initialized" hint (a git-less folder) or, when the
        probe RAISED, a "git error: …" row carrying the failure message — a
        real git failure must not read as "no repo here". Both are
        non-selectable; the panel is left out of ``_panel_order``
        (focusability keys off truthiness), so ←/→ skips it and Enter falls
        through to ``action_commit``'s plain 2-tuple launch.
        """
        workspace_list = self.query_one("#workspace-list", ListView)
        await workspace_list.clear()
        if not self._workspaces:
            if self._probe_error:
                # First line only, capped — git stderr can be multi-line/long
                # and the column is one third of an 80-cell modal.
                detail = self._probe_error.splitlines()[0][:60]
                label = f"git error: {detail}"
            else:
                label = "git not initialized"
            await workspace_list.extend(
                [ListItem(Static(label), id="workspace-none", disabled=True)]
            )
            return
        items = []
        for idx, w in enumerate(self._workspaces):
            label = f"{w.label}  (primary)" if w.is_primary else w.label
            items.append(ListItem(Static(label), id=f"workspace-{idx}"))
        items.append(ListItem(Static("+ New worktree…"), id="workspace-new"))
        await workspace_list.extend(items)
        workspace_list.index = 0

    async def _rebuild_mode_list(self, agent_id: str) -> None:
        mode_list = self.query_one("#mode-list", ListView)
        # clear() and extend() are async — must be awaited, otherwise the
        # removal of the previous agent's modes can race with mounting the
        # new ones and the list ends up showing stale entries (e.g. claude's
        # "auto" remains visible after switching to cursor).
        await mode_list.clear()
        profile = self._launch_profiles().get(agent_id)
        agent_key = profile.agent if profile is not None else agent_id
        spec = self.cfg.agents.get(agent_key)
        if spec is None:
            return
        items = [
            ListItem(Static(f"{idx} {mode.label}"), id=f"mode-{mode.id}")
            for idx, mode in enumerate(spec.permission_modes, start=1)
        ]
        if items:
            await mode_list.extend(items)
        mode_list.index = 0

    def _reflect_focus(self) -> None:
        if self._active_panel == "agent":
            self.query_one("#agent-list", ListView).focus()
        elif self._active_panel == "workspace" and self._workspaces:
            self.query_one("#workspace-list", ListView).focus()
        else:
            self.query_one("#mode-list", ListView).focus()

    def action_focus_left(self) -> None:
        self._active_panel = next_launch_panel(self._active_panel, -1, self._panel_order)
        self._reflect_focus()

    def action_focus_right(self) -> None:
        self._active_panel = next_launch_panel(self._active_panel, +1, self._panel_order)
        self._reflect_focus()

    async def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        # Stock ListView consumes arrow keys before any screen-level
        # binding can see them, so we can't rebuild the mode list from
        # row_up/row_down actions. Listen to Highlighted instead — it
        # fires for both keyboard cursor moves and mouse hover/click.
        lv = event.list_view
        if lv.id != "agent-list":
            return
        idx = lv.index or 0
        new_agent = pick_visible_agent(tuple(self._visible_agents), idx, self._current_agent)
        if new_agent == self._current_agent:
            return
        self._current_agent = new_agent
        await self._rebuild_mode_list(new_agent)

    def action_commit(self) -> None:
        decision = launch_commit_decision(
            active_panel=self._active_panel,
            current_agent=self._current_agent,
            availability=self._availability_now(),
            mode_index=(self.query_one("#mode-list", ListView).index or 0),
            agents=self.cfg.agents,
            launch_profiles=self._launch_profiles(),
        )
        if decision.action == "ignore":
            return
        if decision.action == "switch-to-mode":
            self._active_panel = "mode"
            self._reflect_focus()
            return
        if decision.action == "dismiss" or decision.mode_id is None:
            self.dismiss(None)
            return
        # B2 dismiss arity: 2-tuple without a workspace column (the
        # untouched project create/open callers), 3-tuple with one.
        if not self._workspaces:
            self.dismiss((self._current_agent, decision.mode_id))
            return
        self.dismiss((self._current_agent, decision.mode_id, self._workspace_choice()))

    def _workspace_choice(self) -> object:
        """Resolve the highlighted ``#workspace-list`` row into a choice tuple.

        Read regardless of which panel committed, so Enter from the AGENT /
        PERMISSION columns launches into the default-highlighted primary
        row. ``+ New worktree…`` → ``("new", None)``; the primary row →
        ``("primary", repo_root)``; an existing worktree row →
        ``("worktree", path, branch)``.
        """
        workspace_list = self.query_one("#workspace-list", ListView)
        idx = workspace_list.index or 0
        if not self._workspaces or idx >= len(self._workspaces):
            return ("new", None)
        w = self._workspaces[idx]
        if w.is_primary:
            return ("primary", self._repo_root)
        return ("worktree", w.path, w.branch)

    def action_cancel(self) -> None:
        self.dismiss(None)

    async def _rebuild_agent_list(self) -> None:
        """Recompute visible agents from availability and repopulate the left
        ListView in place. Called on mount-time update and whenever a probe
        result arrives after the screen is already showing.

        Defensive: ``call_later`` from the app-level probe handler can race
        with screen dismiss — by the time this coroutine runs, the screen
        may have been popped and its DOM detached. Bail out quietly when
        the panels are no longer in the tree.
        """
        avail = self._availability_now()
        update = update_launch_options_after_availability(
            enabled_agents=self._enabled_profiles(),
            default_agent=self._default_profile(),
            availability=avail,
            current_agent=self._current_agent,
            active_panel=self._active_panel,
            catalog_ids=self._catalog_profile_ids(),
            auto_mode=self._launch_auto_mode(),
        )
        visible = list(update.visible_agents)
        self._visible_agents = visible
        self._single_agent = update.single_agent
        self._active_panel = update.active_panel

        try:
            agent_panel = self.query_one("#agent-panel", Vertical)
        except Exception:
            return
        agent_panel.display = not self._single_agent

        agent_list = self.query_one("#agent-list", ListView)
        await agent_list.clear()
        new_items = []
        profiles = self._launch_profiles()
        show_launch_user = launch_profile_users_differ(visible, profiles)
        for idx, aid in enumerate(visible, start=1):
            new_items.append(
                ListItem(
                    Static(
                        launch_profile_list_label(
                            idx,
                            aid,
                            profiles.get(aid),
                            avail.get(aid),
                            show_launch_user=show_launch_user,
                        )
                    ),
                    id=f"agent-{aid}",
                )
            )
        if new_items:
            # extend() returns AwaitMount — must be awaited before we set
            # .index on the list, otherwise the index points into a
            # still-empty DOM and the ListView renders as an empty box.
            await agent_list.extend(new_items)

        if update.dismiss:
            mode_list = self.query_one("#mode-list", ListView)
            await mode_list.clear()
            # Same toast pattern as on_mount: surface a hint and let the
            # background host probe re-arm the unavailable modal.
            self.app.notify(
                "No agents installed — install one and press 'r' to retry.",
                severity="warning",
                timeout=6,
            )
            self.dismiss(None)
            return

        # Clamp current selection to the new list.
        if self._current_agent != update.current_agent:
            self._current_agent = update.current_agent
            await self._rebuild_mode_list(self._current_agent)
        if visible:
            try:
                agent_list.index = visible.index(self._current_agent)
            except ValueError:
                agent_list.index = 0
        self._reflect_focus()

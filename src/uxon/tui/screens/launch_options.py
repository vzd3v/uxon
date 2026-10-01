"""Pick a launch profile and an explicit permission mode.

Returns ``(profile_id, mode_id)`` or ``None`` on cancel. Workspace discovery
follows this choice so its filesystem probes use the selected profile's user.
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
    launch_permission_modes,
    launch_profile_list_label,
    launch_profile_users_differ,
    next_launch_panel,
    pick_visible_agent,
    update_launch_options_after_availability,
)


class LaunchOptionsScreen(ModalScreen["tuple[str, str] | None"]):
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
        # Compute the initial visible set from current availability.
        # ``_rebuild_agent_list`` re-reads on every probe-result
        # dispatch so the modal reflects fresh data without a re-open.
        opts = launch_options_state(
            enabled_profiles=self._enabled_profiles(),
            default_profile=self._default_profile(),
            availability=self._availability_now(),
            catalog_ids=self._catalog_profile_ids(),
            auto_mode=self._launch_auto_mode(),
        )
        self._visible_agents = opts.visible_agents
        self._single_agent = opts.single_agent
        self._active_panel = opts.active_panel
        self._current_agent = opts.current_agent
        self._panel_order = self._compute_panel_order()

    def _compute_panel_order(self) -> tuple[str, ...]:
        """The profile panel is omitted when there is only one choice."""
        order: list[str] = []
        if not self._single_agent:
            order.append("agent")
        order.append("mode")
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
        return self.cfg.enabled_profiles

    def _default_profile(self) -> str:
        return self.cfg.default_profile

    def _launch_profiles(self) -> dict:
        return dict(self.cfg.launch_profiles)

    def _launch_auto_mode(self) -> bool:
        return self.cfg.launch_auto_mode

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
        self._reflect_focus()

    async def _rebuild_mode_list(self, profile_id: str) -> None:
        mode_list = self.query_one("#mode-list", ListView)
        # clear() and extend() are async — must be awaited, otherwise the
        # removal of the previous agent's modes can race with mounting the
        # new ones and the list ends up showing stale entries (e.g. claude's
        # "auto" remains visible after switching to cursor).
        await mode_list.clear()
        modes = launch_permission_modes(self.cfg.agents, profile_id, self._launch_profiles())
        items = [
            ListItem(Static(f"{idx} {mode.label}"), id=f"mode-{mode.id}")
            for idx, mode in enumerate(modes, start=1)
        ]
        if items:
            await mode_list.extend(items)
        mode_list.index = 0

    def _reflect_focus(self) -> None:
        if self._active_panel == "agent":
            self.query_one("#agent-list", ListView).focus()
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
        new_agent = pick_visible_agent(self._visible_agents, idx, self._current_agent)
        if new_agent == self._current_agent:
            return
        self._current_agent = new_agent
        await self._rebuild_mode_list(new_agent)

    def action_commit(self) -> None:
        selected = self.query_one("#mode-list", ListView).highlighted_child
        selected_mode_id = selected.id.removeprefix("mode-") if selected and selected.id else None
        decision = launch_commit_decision(
            active_panel=self._active_panel,
            current_agent=self._current_agent,
            availability=self._availability_now(),
            selected_mode_id=selected_mode_id,
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
        self.dismiss((self._current_agent, decision.mode_id))

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
            enabled_profiles=self._enabled_profiles(),
            default_profile=self._default_profile(),
            availability=avail,
            current_agent=self._current_agent,
            active_panel=self._active_panel,
            catalog_ids=self._catalog_profile_ids(),
            auto_mode=self._launch_auto_mode(),
        )
        visible = update.visible_agents
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

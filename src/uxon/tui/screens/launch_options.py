"""LaunchOptionsScreen — pick agent (left) and permission mode (right).

Dismiss value: ``(agent_id, mode_id)`` or ``None`` on cancel.

Two-panel layout: left panel lists enabled + available agents; right
panel lists permission modes for the focused agent. When only one agent
is enabled the left panel is hidden and the modal behaves like the old
``PermissionsScreen`` (single-panel, modes only).

Key map: arrow keys navigate within the focused panel; left/right
arrows switch panels. No h/j/k/l bindings — those letters are free on
``MainScreen`` but avoided here to prevent misfires when the modal sits
above the main screen.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import ListItem, ListView, Static

from ..state import (
    agent_list_label,
    launch_commit_decision,
    launch_options_state,
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

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("left", "focus_left", "Agent", show=True),
        Binding("right", "focus_right", "Mode", show=True),
        Binding("enter", "commit", "Select", show=True, priority=True),
    ]

    def __init__(self, ctx) -> None:
        super().__init__()
        self.ctx = ctx
        # Stage 8 commit 5c: ``availability`` is no longer
        # snapshot-once-at-construction. Compute the initial visible
        # set from whatever availability is current right now (read
        # through ``ctx.agent_availability``, which after commit 5b
        # is a read-through onto ``state.agent_availability.value``).
        # ``_rebuild_agent_list`` re-reads on every probe-result
        # dispatch so the modal reflects fresh data without a
        # re-open.
        state = launch_options_state(
            enabled_agents=tuple(ctx.enabled_agents),
            default_agent=ctx.default_agent,
            availability=self._availability_now(),
        )
        self._visible_agents = list(state.visible_agents)
        self._single_agent = state.single_agent
        self._active_panel = state.active_panel
        self._current_agent = state.current_agent

    def _availability_now(self) -> dict:
        """Read the current availability dict from the live slot store.

        Stage 8 commit 5c: prefers ``app.state.agent_availability.value``
        over going through the ``ctx.agent_availability`` shim, so the
        modal reads the canonical store directly. The shim is the
        compatibility path for unit tests that build a bare ctx; the
        screen-side prefer-state path is what production runs.
        """
        state = getattr(self.app, "state", None)
        if state is not None and state.agent_availability.value is not None:
            return state.agent_availability.value
        return self.ctx.agent_availability

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="agent-panel"):
                yield Static("Agent", classes="panel-title")
                items = []
                avail = self._availability_now()
                for idx, aid in enumerate(self._visible_agents, start=1):
                    items.append(
                        ListItem(
                            Static(agent_list_label(idx, aid, avail.get(aid))),
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

    async def _rebuild_mode_list(self, agent_id: str) -> None:
        from uxon import agents as uxon_agents

        mode_list = self.query_one("#mode-list", ListView)
        # clear() and extend() are async — must be awaited, otherwise the
        # removal of the previous agent's modes can race with mounting the
        # new ones and the list ends up showing stale entries (e.g. claude's
        # "auto" remains visible after switching to cursor).
        await mode_list.clear()
        if agent_id not in uxon_agents.CATALOG:
            return
        spec = uxon_agents.CATALOG[agent_id]
        items = [
            ListItem(Static(f"{idx} {mode.label}"), id=f"mode-{mode.id}")
            for idx, mode in enumerate(spec.permission_modes, start=1)
        ]
        if items:
            await mode_list.extend(items)
        mode_list.index = 0

    def _reflect_focus(self) -> None:
        agent_list = self.query_one("#agent-list", ListView)
        mode_list = self.query_one("#mode-list", ListView)
        if self._active_panel == "agent":
            agent_list.focus()
        else:
            mode_list.focus()

    def action_focus_left(self) -> None:
        if self._single_agent:
            return
        self._active_panel = "agent"
        self._reflect_focus()

    def action_focus_right(self) -> None:
        self._active_panel = "mode"
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
            enabled_agents=tuple(self.ctx.enabled_agents),
            default_agent=self.ctx.default_agent,
            availability=avail,
            current_agent=self._current_agent,
            active_panel=self._active_panel,
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
        for idx, aid in enumerate(visible, start=1):
            new_items.append(
                ListItem(
                    Static(agent_list_label(idx, aid, avail.get(aid))),
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

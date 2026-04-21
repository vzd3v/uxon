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
        Binding("up", "row_up", "", show=False),
        Binding("down", "row_down", "", show=False),
    ]

    def __init__(self, ctx) -> None:
        super().__init__()
        self.ctx = ctx
        enabled = list(ctx.enabled_agents)
        avail = ctx.agent_availability
        # Visible agents: enabled + not confirmed missing/timeout.
        self._visible_agents = [
            aid for aid in enabled
            if avail.get(aid) is None
            or getattr(avail.get(aid), "status", "pending") in ("pending", "ok")
        ]
        self._single_agent = len(self._visible_agents) <= 1
        self._active_panel: str = "agent" if not self._single_agent else "mode"
        initial_agent = (
            ctx.default_agent
            if ctx.default_agent in self._visible_agents
            else (self._visible_agents[0] if self._visible_agents else ctx.default_agent)
        )
        self._current_agent = initial_agent

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="agent-panel"):
                yield Static("Agent", classes="panel-title")
                items = []
                for idx, aid in enumerate(self._visible_agents, start=1):
                    avail_obj = self.ctx.agent_availability.get(aid)
                    status = getattr(avail_obj, "status", None) if avail_obj else None
                    label = f"{idx} {aid}"
                    if status == "pending":
                        label += "  (checking…)"
                    items.append(ListItem(Static(label), id=f"agent-{aid}"))
                yield ListView(*items, id="agent-list")
            with Vertical(id="mode-panel"):
                yield Static("Permission mode", classes="panel-title")
                yield ListView(id="mode-list")

    def on_mount(self) -> None:
        if not self._visible_agents:
            # No usable agent — let the app-level gate handle the hint.
            self.dismiss(None)
            return
        agent_panel = self.query_one("#agent-panel", Vertical)
        agent_panel.display = not self._single_agent
        self._rebuild_mode_list(self._current_agent)
        self._reflect_focus()

    def _rebuild_mode_list(self, agent_id: str) -> None:
        import ccw_agents
        mode_list = self.query_one("#mode-list", ListView)
        mode_list.clear()
        if agent_id not in ccw_agents.CATALOG:
            return
        spec = ccw_agents.CATALOG[agent_id]
        for idx, mode in enumerate(spec.permission_modes, start=1):
            mode_list.append(ListItem(Static(f"{idx} {mode.label}"), id=f"mode-{mode.id}"))
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

    def action_row_up(self) -> None:
        lv = self._focused_list()
        if lv.index is not None and lv.index > 0:
            lv.index -= 1
        self._maybe_rebuild_mode()

    def action_row_down(self) -> None:
        lv = self._focused_list()
        if lv.index is not None and lv.index < len(lv.children) - 1:
            lv.index += 1
        self._maybe_rebuild_mode()

    def _maybe_rebuild_mode(self) -> None:
        if self._active_panel != "agent":
            return
        agent_list = self.query_one("#agent-list", ListView)
        idx = agent_list.index or 0
        if idx < len(self._visible_agents):
            new_agent = self._visible_agents[idx]
            if new_agent != self._current_agent:
                self._current_agent = new_agent
                self._rebuild_mode_list(new_agent)

    def action_commit(self) -> None:
        if self._active_panel == "agent":
            # Block commit while agent is still pending
            avail_obj = self.ctx.agent_availability.get(self._current_agent)
            if avail_obj is not None and getattr(avail_obj, "status", None) == "pending":
                return
            self._active_panel = "mode"
            self._reflect_focus()
            return
        # mode panel
        import ccw_agents
        if self._current_agent not in ccw_agents.CATALOG:
            self.dismiss(None)
            return
        spec = ccw_agents.CATALOG[self._current_agent]
        mode_list = self.query_one("#mode-list", ListView)
        mode_idx = mode_list.index or 0
        if mode_idx < len(spec.permission_modes):
            mode_id = spec.permission_modes[mode_idx].id
        else:
            mode_id = "normal"
        self.dismiss((self._current_agent, mode_id))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _focused_list(self) -> ListView:
        return self.query_one(
            "#agent-list" if self._active_panel == "agent" else "#mode-list",
            ListView,
        )

    async def on__agent_availability_updated(self, event) -> None:
        """Availability arrived — rebuild left panel contents and visibility."""
        await self._rebuild_agent_list()

    async def _rebuild_agent_list(self) -> None:
        """Recompute visible agents from availability and repopulate the left
        ListView in place. Called on mount-time update and whenever a probe
        result arrives after the screen is already showing."""
        enabled = list(self.ctx.enabled_agents)
        avail = self.ctx.agent_availability
        visible = [
            aid for aid in enabled
            if avail.get(aid) is None
            or getattr(avail.get(aid), "status", "pending") in ("pending", "ok")
        ]
        self._visible_agents = visible
        self._single_agent = len(visible) <= 1

        agent_panel = self.query_one("#agent-panel", Vertical)
        agent_panel.display = not self._single_agent

        agent_list = self.query_one("#agent-list", ListView)
        await agent_list.clear()
        for idx, aid in enumerate(visible, start=1):
            avail_obj = avail.get(aid)
            status = getattr(avail_obj, "status", None) if avail_obj else None
            label = f"{idx} {aid}"
            if status == "pending":
                label += "  (checking…)"
            agent_list.append(ListItem(Static(label), id=f"agent-{aid}"))

        # Clamp current selection to the new list.
        if self._current_agent not in visible:
            self._current_agent = visible[0] if visible else self._current_agent
            self._rebuild_mode_list(self._current_agent)
        if visible:
            try:
                agent_list.index = visible.index(self._current_agent)
            except ValueError:
                agent_list.index = 0

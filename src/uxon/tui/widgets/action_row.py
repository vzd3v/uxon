"""ActionRow — a single clickable, hoverable action row on MainScreen.

Replaces the blessed-era hand-rolled "+ Create new project" rows. Each
row is a :class:`Static` widget with ``can_focus=True`` so arrow-key
navigation and Tab cycling route through the standard focus machinery.
Activation (Enter or left-click) posts a :class:`Activated` message
so the parent screen (``MainScreen``) routes it to the correct
launch-callback.

The ``disabled=True`` state (used when e.g. ``cwd_writable=False``)
greys the row and suppresses activation — the row still renders a
hint describing why it's disabled.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.widgets import Static

# Container ID that signals a row-of-buttons group. ActionRows whose
# parent carries this id get cyclic ←/→ navigation and a single-step
# ↑/↓ exit; rows under any other parent fall through to the standard
# focus chain. The id-based marker is robust to wrapping (a future
# Lazy / styled wrapper around the container can't break the test
# the way an isinstance(parent, Horizontal) check would).
ACTION_GROUP_CONTAINER_ID = "top-actions"


def action_row_can_activate(enabled: bool) -> bool:
    return enabled


class ActionRow(Static):
    """A focusable row with a keyboard/mouse-activated payload.

    Parent screens declare their own BINDINGS for digit-jump etc. and
    use :attr:`ActionRow.kind` to dispatch activation. The widget emits
    :class:`Activated` on Enter or a mouse click release.
    """

    can_focus = True

    DEFAULT_CSS = """
    ActionRow {
        width: 1fr;
        height: 1;
        padding: 0 1;
        content-align: left middle;
    }
    ActionRow:focus {
        background: $accent 30%;
        text-style: bold;
    }
    ActionRow:hover {
        background: $boost;
    }
    ActionRow.-disabled {
        color: $text-muted;
        text-style: dim;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("enter", "activate", "Activate", show=False),
        # Cyclic ←/→ within a Horizontal group of ActionRows. No-op when
        # the row sits in a Vertical (settings / kill-all rows).
        Binding("left", "cycle(-1)", "", show=False),
        Binding("right", "cycle(1)", "", show=False),
        # In a Horizontal group, ↑/↓ skip past the whole group; outside
        # one, fall through to the standard focus_previous/focus_next.
        Binding("up", "leave_group(-1)", "", show=False),
        Binding("down", "leave_group(1)", "", show=False),
    ]

    class Activated(Message):
        """Posted when the row is activated (Enter or click release)."""

        def __init__(self, row: ActionRow) -> None:
            super().__init__()
            self.row = row

    def __init__(
        self,
        *,
        kind: str,
        label: str,
        detail: str = "",
        digit: int | None = None,
        enabled: bool = True,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.kind = kind
        self.label = label
        self.detail = detail
        self.digit = digit
        self._enabled = enabled
        self._render_text()

    def _render_text(self) -> None:
        t = Text()
        if self.digit is not None:
            t.append(f"{self.digit} ", style="dim")
        else:
            t.append("  ")
        t.append("+ ", style="bold green")
        t.append(self.label, style="bold")
        if self.detail:
            t.append(f"  {self.detail}", style="dim")
        self.update(t)
        if not self._enabled:
            self.add_class("-disabled")
        else:
            self.remove_class("-disabled")

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self._render_text()

    # ── Activation ───────────────────────────────────────────────────

    def action_activate(self) -> None:
        if not action_row_can_activate(self._enabled):
            return
        self.post_message(self.Activated(self))

    async def _on_click(self, event: events.Click) -> None:  # type: ignore[override]
        event.stop()
        self.focus()
        if action_row_can_activate(self._enabled):
            self.post_message(self.Activated(self))

    # ── Group navigation ─────────────────────────────────────────────

    def _group_siblings(self) -> list[ActionRow] | None:
        """Return sibling ActionRows when this row sits in the action group.

        Group membership is keyed off the parent's id
        (:data:`ACTION_GROUP_CONTAINER_ID`), not its widget class — so
        a future styled wrapper or a switch from ``Horizontal`` to
        another layout container can't silently disable arrow-key
        cycling. ``None`` means the row is a singleton (e.g. settings,
        kill-all): ←/→ are no-ops and ↑/↓ fall through to the standard
        focus chain.
        """
        parent = self.parent
        if parent is None or parent.id != ACTION_GROUP_CONTAINER_ID:
            return None
        return [c for c in parent.children if isinstance(c, ActionRow)]

    def action_cycle(self, delta: int) -> None:
        siblings = self._group_siblings()
        if siblings is None or len(siblings) <= 1:
            return
        try:
            idx = siblings.index(self)
        except ValueError:
            return
        new_idx = (idx + delta) % len(siblings)
        siblings[new_idx].focus()

    def action_leave_group(self, direction: int) -> None:
        siblings = self._group_siblings()
        # Use ``app.action_focus_*`` to match the existing convention
        # in :class:`FocusReleasingDataTable` and the MainScreen
        # ↑/↓ bindings — one consistent spelling for "step the focus
        # chain" across the TUI.
        if siblings is None:
            # Singleton — preserve default ↑/↓ focus-chain traversal.
            if direction < 0:
                self.app.action_focus_previous()
            else:
                self.app.action_focus_next()
            return
        # Inside a group: walk the focus chain past every sibling so
        # ↑/↓ exit the row of buttons in one keystroke. The bound
        # ``len(siblings) + 1`` covers the normal case (one step per
        # sibling plus the step that lands outside the group). If
        # the screen has no other focusable widget, the focus chain
        # wraps back into the group and ``seen`` aborts the loop —
        # focus then stays on a sibling rather than escaping. Real
        # screens always have other focusable widgets (search bar,
        # dashboard table) so this best-effort fallback is fine in
        # practice.
        screen = self.screen
        seen: set[int] = set()
        for _ in range(len(siblings) + 1):
            if direction < 0:
                self.app.action_focus_previous()
            else:
                self.app.action_focus_next()
            focused = screen.focused
            if focused is None:
                return
            fid = id(focused)
            if fid in seen:
                return
            seen.add(fid)
            if not isinstance(focused, ActionRow) or focused not in siblings:
                return


@dataclass(frozen=True)
class ActionRowSpec:
    """Helper: declarative description of one MainScreen action row."""

    kind: str
    label: str
    detail: str
    digit: int
    enabled: bool

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


@dataclass(frozen=True)
class ActionRowSpec:
    """Helper: declarative description of one MainScreen action row."""

    kind: str
    label: str
    detail: str
    digit: int
    enabled: bool

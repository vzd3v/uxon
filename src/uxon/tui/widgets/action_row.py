"""ActionRow — a single clickable, hoverable action row on MainScreen.

Each row is a :class:`Static` widget with ``can_focus=True`` so arrow-key
navigation and Tab cycling route through the standard focus machinery.
Activation (Enter or left-click) posts a :class:`Activated` message
so the parent screen (``MainScreen``) routes it to the correct
launch-callback.

All action rows share one flat single-line visual — bold label plus a
dim detail suffix — whether they sit in the top launch stack or the
bottom superuser block. (Earlier layouts drew the top three as tall
bordered "buttons"; the flat unification keeps the session table the
visually dominant element of the screen.)

The ``disabled=True`` state (used when e.g. ``cwd_writable=False``)
greys the row and suppresses activation — the row still renders a
hint describing why it's disabled.
"""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.widgets import Static

from uxon.infra.events import debug as _debug


def action_row_can_activate(enabled: bool) -> bool:
    return enabled


class ActionRow(Static):
    """A focusable row with a keyboard/mouse-activated payload.

    Parent screens use :attr:`ActionRow.kind` to dispatch activation.
    The widget emits :class:`Activated` on Enter or a mouse click
    release.
    """

    can_focus = True

    DEFAULT_CSS = """
    ActionRow {
        width: 1fr;
        height: 1;
        padding: 0 1;
        content-align: left middle;
        background: transparent;
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
        # ↑/↓ step the standard focus chain one widget at a time — the
        # action rows are a vertical stack, so spatial navigation and
        # the focus chain agree. The only extra over the default is the
        # dashboard-edge snap in :meth:`action_leave` below.
        Binding("up", "leave(-1)", "", show=False),
        Binding("down", "leave(1)", "", show=False),
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
        enabled: bool = True,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.kind = kind
        self.label = label
        self.detail = detail
        self._enabled = enabled
        # ``(label, detail, enabled)`` of the last painted state — the
        # widget-boundary change gate for :meth:`_render_text`. ``None``
        # until the first paint (from this ``__init__``).
        self._last_painted: tuple[str, str, bool] | None = None
        self._render_text()

    def _render_text(self) -> None:
        # Widget-boundary change gate: every caller (apply_ctx_refresh's
        # cwd/open/new rows, launch_flow, the cwd-writable landing, the
        # superuser kill row) routes through here, so a tick that leaves
        # label/detail/enabled untouched issues zero writes.
        key = (self.label, self.detail, self._enabled)
        if key == self._last_painted:
            return
        self._last_painted = key
        t = Text()
        t.append(self.label, style="bold")
        if self.detail:
            t.append(f"  {self.detail}", style="dim")
        # ``layout=False``: the row's box is a fixed cell — a label/detail
        # text swap never changes its size, so skip the screen-global
        # relayout the default ``update`` would request.
        self.update(t, layout=False)
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

    # ── ↑/↓ navigation ───────────────────────────────────────────────

    def action_leave(self, direction: int) -> None:
        """Step the focus chain one widget; snap the dashboard cursor.

        Uses ``app.action_focus_*`` to match the existing convention in
        :class:`SessionListView` — one consistent spelling for "step the
        focus chain" across the TUI. When the step lands on the session
        table, force its cursor to the edge nearest the keypress so the
        visual transition matches: ↓ into the table selects the first
        row, ↑ into it selects the last. Without this the list preserves
        its prior ``cursor_row`` and the focus appears to teleport.
        """
        _debug("keys", at="action_row_leave", row=self.id, direction=direction)
        if direction < 0:
            self.app.action_focus_previous()
        else:
            self.app.action_focus_next()
        focused = self.screen.focused
        # Imported locally to keep ``action_row`` import-light.
        from .session_list_view import SessionListView

        if isinstance(focused, SessionListView) and focused.row_count > 0:
            target = focused.row_count - 1 if direction < 0 else 0
            focused.move_cursor(row=target)

"""SearchBar — Input + match counter, scoped Esc behaviour."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Input, Static


class FilterChanged(Message):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class SearchBar(Widget):
    """Always-visible search bar above the dashboard.

    Esc behaviour (priority binding):
      - non-empty input: clear text, keep focus
      - empty input + focused: blur (move focus to dashboard)
    """

    DEFAULT_CSS = """
    SearchBar {
        height: 1;
        padding: 0 1;
    }
    SearchBar > Horizontal { height: 1; }
    SearchBar Input {
        width: 1fr;
        height: 1;
        border: none;
        padding: 0;
        background: $surface;
    }
    SearchBar Input:focus { border: none; }
    SearchBar #match-count { width: auto; color: $text-muted; }
    """

    BINDINGS = [
        Binding("escape", "scope_cancel", "", show=False, priority=True),
    ]

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.input = Input(placeholder="/ to search", id="search-input")
        self._counter = Static("", id="match-count")

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield self.input
            yield self._counter

    def on_input_changed(self, event: Input.Changed) -> None:
        self.post_message(FilterChanged(event.value))

    def set_match_count(self, count: int) -> None:
        if not self.input.value:
            self._counter.update("")
            return
        self._counter.update(f"{count} match{'es' if count != 1 else ''}")

    def action_scope_cancel(self) -> None:
        if self.input.value:
            self.input.value = ""
            return
        # blur to dashboard
        try:
            from .session_dashboard_table import SessionDashboardTable

            self.app.query_one(SessionDashboardTable).focus()
            return
        except Exception:
            pass
        # No dashboard mounted (e.g. unit-test harness): plain blur.
        try:
            self.screen.set_focus(None)
        except Exception:
            pass

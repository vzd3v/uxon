"""FilterInput — text input + match counter, emits :class:`FilterChanged`.

Layout-only primitive. Visibility, focus chain management, and Esc
semantics belong to the consumer:

* :class:`uxon.tui.widgets.search_bar.SearchBar` wraps it as a
  summon-on-demand bar for the main session dashboard.
* :class:`uxon.tui.screens.existing.ExistingProjectScreen` embeds
  it as an always-visible filter for project picking.

Both consumers receive the same :class:`FilterChanged` message and
call :meth:`FilterInput.set_match_count` after recomputing matches.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Input, Static


class FilterChanged(Message):
    """Posted whenever the filter text changes."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class FilterInput(Widget):
    """Input + match-count counter; one event per keystroke."""

    DEFAULT_CSS = """
    FilterInput {
        height: 1;
        padding: 0 1;
    }
    FilterInput > Horizontal { height: 1; }
    FilterInput Input {
        width: 1fr;
        height: 1;
        border: none;
        padding: 0;
        background: $surface;
    }
    FilterInput Input:focus { border: none; }
    FilterInput #match-count { width: auto; color: $text-muted; }
    """

    def __init__(self, *, placeholder: str = "search…", id: str | None = None) -> None:
        super().__init__(id=id)
        self.input = Input(placeholder=placeholder, id="filter-input")
        self._counter = Static("", id="match-count")

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield self.input
            yield self._counter

    def on_input_changed(self, event: Input.Changed) -> None:
        # The inner Input.Changed is an implementation detail — the
        # public event is FilterChanged. ``stop()`` keeps the raw
        # message from leaking to consumers that mix a FilterInput
        # with a sibling Input on the same screen.
        event.stop()
        self.post_message(FilterChanged(event.value))

    def set_match_count(self, count: int) -> None:
        if not self.input.value:
            self._counter.update("")
            return
        self._counter.update(f"{count} match{'es' if count != 1 else ''}")

    @property
    def value(self) -> str:
        return self.input.value

    @value.setter
    def value(self, text: str) -> None:
        self.input.value = text

    def focus_input(self) -> None:
        self.input.focus()

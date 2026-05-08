"""SearchBar — Input + match counter, summoned on demand.

Hidden by default (CSS ``visibility: hidden`` reserves the line so
the segment header below doesn't shift when the bar shows or
hides). The screen binds ``s`` / ``/`` to :meth:`show`; ``Esc``
inside the bar runs :meth:`action_scope_cancel`.

Esc behaviour:

* non-empty input → clear text, keep bar open and focused.
* empty input + focused → hide the bar and return focus to the
  caller-supplied widget (defaults to ``#action-cwd``).

While hidden, the inner :class:`textual.widgets.Input` has its
``can_focus`` flag flipped off so the surrounding focus chain
(Tab / Shift+Tab) skips it — operators can't tab into an
invisible input.
"""

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
    DEFAULT_CSS = """
    SearchBar {
        height: 1;
        padding: 0 1;
        visibility: hidden;
    }
    SearchBar.-shown {
        visibility: visible;
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
        self.input = Input(placeholder="search…", id="search-input")
        self._counter = Static("", id="match-count")
        self._fallback_focus_id = "action-cwd"

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield self.input
            yield self._counter

    def on_mount(self) -> None:
        # Hidden by default — keep the input out of the focus chain
        # until :meth:`show` opens the bar.
        self.input.can_focus = False

    def on_input_changed(self, event: Input.Changed) -> None:
        self.post_message(FilterChanged(event.value))

    def set_match_count(self, count: int) -> None:
        if not self.input.value:
            self._counter.update("")
            return
        self._counter.update(f"{count} match{'es' if count != 1 else ''}")

    def show(self, *, return_focus_id: str | None = None) -> None:
        """Reveal the bar and focus the input.

        ``return_focus_id`` is remembered for :meth:`hide` so a
        subsequent Esc returns focus to the same widget the
        operator summoned the bar from.
        """
        # ``None`` means "caller didn't supply one — keep the existing
        # fallback". An explicit empty string would be a caller bug;
        # ignore it the same way to avoid clobbering the default.
        if return_focus_id:
            self._fallback_focus_id = return_focus_id
        elif return_focus_id is None:
            # Reset to the default so a previous show() doesn't leak
            # a stale fallback into a later id-less invocation.
            self._fallback_focus_id = "action-cwd"
        self.add_class("-shown")
        self.input.can_focus = True
        self.input.focus()

    def hide(self) -> None:
        """Clear the filter, hide the bar, restore caller focus."""
        self.input.value = ""  # FilterChanged fires with "" — caller clears.
        self.remove_class("-shown")
        self.input.can_focus = False
        try:
            self.screen.query_one(f"#{self._fallback_focus_id}").focus()
        except Exception:  # pragma: no cover — caller widget gone
            pass

    def action_scope_cancel(self) -> None:
        if self.input.value:
            self.input.value = ""
            return
        self.hide()

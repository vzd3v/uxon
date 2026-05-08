"""SearchBar — summon-on-demand wrapper around :class:`FilterInput`.

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

The text-input + match-counter primitive lives in
:mod:`uxon.tui.widgets.filter_input` and is shared with always-on
embeddings (e.g. :class:`uxon.tui.screens.existing.ExistingProjectScreen`).
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widget import Widget

from .filter_input import FilterChanged, FilterInput

# Re-exported so existing ``from ..widgets.search_bar import FilterChanged``
# imports keep working — FilterChanged is now defined alongside the
# primitive that emits it.
__all__ = ["FilterChanged", "SearchBar"]


class SearchBar(Widget):
    DEFAULT_CSS = """
    SearchBar {
        height: 1;
        visibility: hidden;
    }
    SearchBar.-shown {
        visibility: visible;
    }
    """

    BINDINGS = [
        Binding("escape", "scope_cancel", "", show=False, priority=True),
    ]

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._filter = FilterInput(placeholder="search…")
        self._fallback_focus_id = "action-cwd"

    def compose(self) -> ComposeResult:
        yield self._filter

    def on_mount(self) -> None:
        # Hidden by default — keep the input out of the focus chain
        # until :meth:`show` opens the bar.
        self._filter.input.can_focus = False

    @property
    def input(self):  # noqa: D401 — backward-compat alias for tests/callers.
        """Inner :class:`Input` (delegated through the FilterInput primitive)."""
        return self._filter.input

    def set_match_count(self, count: int) -> None:
        self._filter.set_match_count(count)

    def show(self, *, return_focus_id: str | None = None) -> None:
        """Reveal the bar and focus the input.

        ``return_focus_id`` is remembered for :meth:`hide` so a
        subsequent Esc returns focus to the same widget the
        operator summoned the bar from.
        """
        # Truthy id pins the fallback. Falsy (``None`` or ``""``)
        # resets to the default so a previous show() doesn't leak a
        # stale fallback into a later id-less invocation.
        if return_focus_id:
            self._fallback_focus_id = return_focus_id
        else:
            self._fallback_focus_id = "action-cwd"
        self.add_class("-shown")
        self._filter.input.can_focus = True
        self._filter.focus_input()

    def hide(self) -> None:
        """Clear the filter, hide the bar, restore caller focus."""
        self._filter.value = ""  # FilterChanged fires with "" — caller clears.
        self.remove_class("-shown")
        self._filter.input.can_focus = False
        try:
            self.screen.query_one(f"#{self._fallback_focus_id}").focus()
        except Exception:  # pragma: no cover — caller widget gone
            pass

    def action_scope_cancel(self) -> None:
        if self._filter.value:
            self._filter.value = ""
            return
        self.hide()

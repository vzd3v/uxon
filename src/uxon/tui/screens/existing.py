"""ExistingProjectScreen — pick from existing directories under project root.

Search-as-you-type: the filter input owns focus on mount, narrowing
the list with every keystroke; ``Esc`` clears a non-empty filter
(otherwise dismisses), ``Enter`` picks the row under the ListView
cursor.

Dismiss values:
  - ``str`` — chosen project directory name.
  - ``None`` — user cancelled.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, ListItem, ListView, Static

from ..keymap import bindings_with_aliases
from ..state import filter_existing_projects
from ..widgets.filter_input import FilterChanged, FilterInput


def _row_label(name: str, mtime: str) -> str:
    # ``{name:<60.60}`` pads/truncates so the mtime column lines up
    # regardless of name length. The 70-cell modal width minus
    # padding leaves ~66 cells; mtime takes 5 + 1 space.
    return f"{name:<60.60} {mtime:>5}"


class ExistingProjectScreen(ModalScreen["str | None"]):
    DEFAULT_CSS = """
    ExistingProjectScreen {
        align: center middle;
    }
    ExistingProjectScreen > Vertical {
        width: 70;
        height: 90%;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    ExistingProjectScreen .title {
        text-style: bold;
        margin-bottom: 1;
    }
    ExistingProjectScreen FilterInput {
        margin-top: 1;
        margin-bottom: 1;
    }
    ExistingProjectScreen ListView {
        height: 1fr;
        scrollbar-gutter: stable;
    }
    ExistingProjectScreen ListItem {
        padding: 0 1;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = bindings_with_aliases(
        # ``priority=True`` on every binding so they fire even while
        # focus sits on the FilterInput's inner Input — operators
        # navigate / pick / cancel without ever leaving the search
        # field.
        Binding("escape", "cancel", "Cancel", show=True, priority=True),
        Binding("enter", "pick", "Select", show=True, priority=True),
        Binding("up", "cursor_up", "", show=False, priority=True),
        Binding("down", "cursor_down", "", show=False, priority=True),
        Binding("k", "cursor_up", "", show=False, priority=True),
        Binding("j", "cursor_down", "", show=False, priority=True),
    )

    def __init__(self, projects: list[tuple[str, str]], project_root: str) -> None:
        super().__init__()
        # Each entry: (name, compact_mtime). See _list_existing_projects.
        self.projects = list(projects)
        self.project_root = project_root
        # Filtered view drives the ListView render and Enter's row
        # resolution; kept in sync with the input via ``on_filter_changed``.
        self._filtered: list[tuple[str, str]] = list(projects)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Open existing project", classes="title")
            yield Static(f"  {self.project_root}/")
            yield FilterInput(placeholder="filter…", id="project-filter")
            items = [ListItem(Label(_row_label(name, mtime))) for (name, mtime) in self._filtered]
            yield ListView(*items, id="existing-list")

    def on_mount(self) -> None:
        # Land directly on the filter input so the operator can type
        # immediately. ListView navigation and Enter still work via
        # the screen's priority bindings — no need to surrender focus.
        self.query_one(FilterInput).focus_input()
        self._sync_match_count()

    def action_cancel(self) -> None:
        fi = self.query_one(FilterInput)
        if fi.value:
            # Non-empty: clear the filter (the resulting FilterChanged
            # rebuilds the list) and keep the modal open.
            fi.value = ""
            return
        self.dismiss(None)

    def action_pick(self) -> None:
        if not self._filtered:
            return
        lv = self.query_one(ListView)
        idx = lv.index if lv.index is not None else 0
        if 0 <= idx < len(self._filtered):
            self.dismiss(self._filtered[idx][0])

    def action_cursor_up(self) -> None:
        self._move_cursor_wrapped(-1)

    def action_cursor_down(self) -> None:
        self._move_cursor_wrapped(1)

    def _move_cursor_wrapped(self, delta: int) -> None:
        if not self._filtered:
            return
        lv = self.query_one(ListView)
        current = lv.index if lv.index is not None else 0
        lv.index = (current + delta) % len(self._filtered)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.action_pick()

    def on_filter_changed(self, event: FilterChanged) -> None:
        self._filtered = filter_existing_projects(self.projects, event.text)
        lv = self.query_one(ListView)
        lv.clear()
        for name, mtime in self._filtered:
            lv.append(ListItem(Label(_row_label(name, mtime))))
        # Land the cursor on the top match so Enter picks something
        # meaningful even after a wide narrowing.
        lv.index = 0 if self._filtered else None
        self._sync_match_count()

    def _sync_match_count(self) -> None:
        self.query_one(FilterInput).set_match_count(len(self._filtered))

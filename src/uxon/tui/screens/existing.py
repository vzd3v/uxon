"""ExistingProjectScreen — browse existing directories under the project root.

Search-as-you-type: the filter input owns focus on mount, narrowing
the list with every keystroke; ``Esc`` clears a non-empty filter
(otherwise dismisses), ``Enter`` picks the row under the ListView
cursor. Left/right browse directories without moving the input cursor.

Dismiss values:
  - ``str`` — chosen project path relative to the project root.
  - ``None`` — user cancelled.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import PurePosixPath
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Input, Label, ListItem, ListView, Static

from ..keymap import bindings_with_aliases
from ..state import ProjectBrowserLevel, filter_existing_projects
from ..widgets.filter_input import FilterChanged, FilterInput
from .modal_base import CardModal


def _row_label(name: str, mtime: str) -> str:
    # ``{name:<60.60}`` pads/truncates so the mtime column lines up
    # regardless of name length. The 70-cell modal width minus
    # padding leaves ~66 cells; mtime takes 5 + 1 space.
    return f"{name:<60.60} {mtime:>5}"


class ExistingProjectScreen(CardModal["str | None"]):
    # Card chrome + Esc binding come from CardModal; this screen overrides
    # the width and makes the card full-height (a long, scrolling list),
    # then layers on the FilterInput/ListView styling below.
    DEFAULT_CSS = """
    ExistingProjectScreen .modal-card {
        width: 70;
        height: 90%;
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
    ExistingProjectScreen #browser-status {
        height: auto;
        color: $text-muted;
    }
    /* Focus lives on the FilterInput, so Textual would render the
       ListView cursor in its dim ``blurred`` palette — operators read
       that as "nothing selected" and press ``down`` to navigate, then
       complain the cursor "skipped to row 2". Force the bright,
       focused-style cursor regardless of where focus actually sits. */
    ExistingProjectScreen ListView > ListItem.-highlight {
        color: $block-cursor-foreground;
        background: $block-cursor-background;
        text-style: $block-cursor-text-style;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = bindings_with_aliases(
        # ``priority=True`` on every binding so they fire even while
        # focus sits on the FilterInput's inner Input — operators
        # navigate / pick / cancel without ever leaving the search
        # field. No vim ``j``/``k`` aliases here: when the operator
        # is typing into the filter, navigation letters are noise,
        # and arrow keys cover the gesture cleanly.
        Binding("escape", "cancel", "Cancel", show=True, priority=True),
        Binding("enter", "pick", "Select", show=True, priority=True),
        Binding("up", "cursor_up", "", show=False, priority=True),
        Binding("down", "cursor_down", "", show=False, priority=True),
        Binding("left", "parent_directory", "Back", show=True, priority=True),
        Binding("right", "enter_directory", "Inside", show=True, priority=True),
    )

    # Framework-managed initial focus (rationale: SessionChoiceScreen):
    # Textual focuses this on compose AND on resume after a child modal
    # dismisses, with no synchronous-``on_mount`` race. ``#filter-input``
    # is the Input nested inside the FilterInput widget.
    AUTO_FOCUS = "#filter-input"

    def __init__(
        self,
        projects: list[tuple[str, str]],
        project_root: str,
        *,
        list_directories: Callable[[str], list[tuple[str, str]]] = lambda path: [],
    ) -> None:
        super().__init__()
        # Each entry: (name, compact_mtime).
        self.projects = list(projects)
        self.project_root = project_root
        self._list_directories = list_directories
        self._directory = ""
        self._history: list[ProjectBrowserLevel] = []
        self._loading = False
        self._navigation_epoch = 0
        # Filtered view drives the ListView render and Enter's row
        # resolution; kept in sync with the input via ``on_filter_changed``.
        self._filtered: list[tuple[str, str]] = list(projects)

    def compose(self) -> ComposeResult:
        with self.card():
            yield Static("Open existing project", classes="title")
            yield Static(f"  {self.project_root}/", id="project-path", markup=False)
            yield FilterInput(placeholder="filter…", id="project-filter")
            items = [
                ListItem(Label(_row_label(name, mtime), markup=False))
                for (name, mtime) in self._filtered
            ]
            yield ListView(*items, id="existing-list")
            yield Static("", id="browser-status", markup=False)
            yield Footer()

    def on_mount(self) -> None:
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
        if self._loading:
            return
        path = self._snapshot().selected_path
        if path is not None:
            self.dismiss(path)

    def _snapshot(self) -> ProjectBrowserLevel:
        return ProjectBrowserLevel(
            self._directory,
            tuple(self.projects),
            self.query_one(FilterInput).value,
            self.query_one(ListView).index,
        )

    def action_enter_directory(self) -> None:
        if self._loading:
            return
        path = self._snapshot().selected_path
        if path is None:
            return
        self._loading = True
        self._navigation_epoch += 1
        epoch = self._navigation_epoch
        self.query_one("#browser-status", Static).update("Loading…")
        self.app.run_off_loop(  # type: ignore[attr-defined]
            lambda: self._list_directories(path),
            on_success=lambda projects: self.call_later(
                self._enter_loaded_directory, epoch, path, projects
            ),
            on_error=lambda exc: self._directory_failed(epoch, exc),
            label="project_directories",
        )

    async def _enter_loaded_directory(
        self, epoch: int, path: str, projects: list[tuple[str, str]]
    ) -> None:
        if not self.is_mounted or epoch != self._navigation_epoch:
            return
        self._history.append(self._snapshot())
        self._directory = path
        self.projects = list(projects)
        self._filtered = list(projects)
        fi = self.query_one(FilterInput)
        with fi.input.prevent(Input.Changed):
            fi.value = ""
        await self._render_list()
        self._loading = False
        self._sync_browser_status()

    def _directory_failed(self, epoch: int, exc: Exception) -> None:
        if not self.is_mounted or epoch != self._navigation_epoch:
            return
        self._loading = False
        self.query_one("#browser-status", Static).update(str(exc))

    def action_parent_directory(self) -> None:
        # Cancel pending navigation before it can land in a different directory.
        self._navigation_epoch += 1
        self._loading = True
        # Serialize DOM changes with filter updates and completed reads.
        self.call_later(self._restore_parent_directory)

    async def _restore_parent_directory(self) -> None:
        if not self.is_mounted:
            return
        if not self._history:
            self._loading = False
            self.query_one("#browser-status", Static).update("")
            return
        level = self._history.pop()
        self._directory = level.directory
        self.projects = list(level.projects)
        self._filtered = level.filtered
        fi = self.query_one(FilterInput)
        with fi.input.prevent(Input.Changed):
            fi.value = level.needle
        await self._render_list(level.index)
        self._loading = False
        self._sync_browser_status()

    def action_cursor_up(self) -> None:
        self._move_cursor_wrapped(-1)

    def action_cursor_down(self) -> None:
        self._move_cursor_wrapped(1)

    def _move_cursor_wrapped(self, delta: int) -> None:
        if self._loading or not self._filtered:
            return
        lv = self.query_one(ListView)
        current = lv.index if lv.index is not None else 0
        lv.index = (current + delta) % len(self._filtered)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.action_pick()

    async def on_filter_changed(self, event: FilterChanged) -> None:
        if event.text != self.query_one(FilterInput).value:
            return
        self._filtered = filter_existing_projects(self.projects, event.text)
        await self._render_list()

    async def _render_list(self, index: int | None = 0) -> None:
        # Async + await is load-bearing: ``ListView.clear()`` returns an
        # ``AwaitRemove`` and ``extend()`` an ``AwaitMount``. Assigning
        # ``lv.index`` before those complete races the still-pending
        # DOM mutation — the highlight lands on a stale row (or
        # disappears) until the operator nudges the list. Awaiting both
        # keeps Enter pointing at the visible top match.
        lv = self.query_one(ListView)
        await lv.clear()
        if self._filtered:
            await lv.extend(
                [
                    ListItem(Label(_row_label(name, mtime), markup=False))
                    for name, mtime in self._filtered
                ]
            )
            lv.index = min(index or 0, len(self._filtered) - 1)
        else:
            lv.index = None
        self._sync_match_count()
        path = str(PurePosixPath(self.project_root) / self._directory)
        self.query_one("#project-path", Static).update(f"  {path}/")
        self._sync_browser_status()

    def _sync_browser_status(self) -> None:
        status = (
            "" if self._filtered else "No matching folders" if self.projects else "No subfolders"
        )
        self.query_one("#browser-status", Static).update("Loading…" if self._loading else status)

    def _sync_match_count(self) -> None:
        self.query_one(FilterInput).set_match_count(len(self._filtered))

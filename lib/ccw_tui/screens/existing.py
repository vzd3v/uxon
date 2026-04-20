"""ExistingProjectScreen — pick from existing directories under project root.

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
    ExistingProjectScreen ListView {
        height: 1fr;
        scrollbar-gutter: stable;
    }
    ExistingProjectScreen ListItem {
        padding: 0 1;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("enter", "pick", "Select", show=True),
        Binding("1", "pick_digit(1)", "1-9 pick", show=True),
        Binding("2", "pick_digit(2)", "", show=False),
        Binding("3", "pick_digit(3)", "", show=False),
        Binding("4", "pick_digit(4)", "", show=False),
        Binding("5", "pick_digit(5)", "", show=False),
        Binding("6", "pick_digit(6)", "", show=False),
        Binding("7", "pick_digit(7)", "", show=False),
        Binding("8", "pick_digit(8)", "", show=False),
        Binding("9", "pick_digit(9)", "", show=False),
    ]

    def __init__(
        self, projects: list[tuple[str, str]], project_root: str
    ) -> None:
        super().__init__()
        # Each entry: (name, compact_mtime). See _list_existing_projects.
        self.projects = list(projects)
        self.project_root = project_root

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Open existing project", classes="title")
            yield Static(f"  {self.project_root}/")
            # ``{i+1:>2}`` right-aligns the digit so single- and double-
            # digit prefixes share one column. ``{name:<50.50}`` pads/
            # truncates the name so the mtime column lines up no matter
            # how long each name is.
            items = [
                ListItem(Label(f"{i + 1:>2} {name:<50.50} {mtime:>5}"))
                for i, (name, mtime) in enumerate(self.projects)
            ]
            yield ListView(*items, id="existing-list")

    def on_mount(self) -> None:
        self.query_one(ListView).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_pick(self) -> None:
        lv = self.query_one(ListView)
        idx = lv.index or 0
        if 0 <= idx < len(self.projects):
            self.dismiss(self.projects[idx][0])

    def action_pick_digit(self, n: int) -> None:
        idx = n - 1
        if 0 <= idx < len(self.projects):
            self.dismiss(self.projects[idx][0])

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.action_pick()

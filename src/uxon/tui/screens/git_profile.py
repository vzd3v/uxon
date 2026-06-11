"""GitProfileScreen — pick a git remote profile or skip.

Dismiss values:
  - ``""``   — user chose the skip sentinel (don't create a git remote).
  - profile name — the name of the chosen profile.
  - ``None`` — user cancelled the whole new-project flow.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Label, ListItem, ListView, Static

from ..keymap import bindings_with_aliases
from ..state import pick_index
from .modal_base import CardModal


class GitProfileScreen(CardModal["str | None"]):
    # Card chrome + Esc→cancel come from CardModal; only width is custom.
    DEFAULT_CSS = """
    GitProfileScreen .modal-card {
        width: 90;
        max-height: 30;
    }
    GitProfileScreen ListView {
        height: auto;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = bindings_with_aliases(
        Binding("enter", "pick", "Select", show=True),
        Binding("0", "pick_digit(0)", "0-9 pick", show=True),
        Binding("1", "pick_digit(1)", "", show=False),
        Binding("2", "pick_digit(2)", "", show=False),
        Binding("3", "pick_digit(3)", "", show=False),
        Binding("4", "pick_digit(4)", "", show=False),
        Binding("5", "pick_digit(5)", "", show=False),
        Binding("6", "pick_digit(6)", "", show=False),
        Binding("7", "pick_digit(7)", "", show=False),
        Binding("8", "pick_digit(8)", "", show=False),
        Binding("9", "pick_digit(9)", "", show=False),
    )

    # Framework-managed initial focus (rationale: SessionChoiceScreen).
    AUTO_FOCUS = "#git-profile-list"

    def __init__(
        self,
        options: list[tuple[str, str]],
        default_profile: str = "",
    ) -> None:
        super().__init__()
        # First row is a skip sentinel (empty profile name).
        self.rows: list[tuple[str, str]] = [("", "skip — don't create any git remote")]
        self.rows.extend(options)
        self.default_profile = default_profile

    def compose(self) -> ComposeResult:
        with self.card():
            yield Static("Create git remote?", classes="title")
            items: list[ListItem] = []
            self._default_index = 0
            for i, (name, desc) in enumerate(self.rows):
                if name and name == self.default_profile:
                    self._default_index = i
                label = "skip" if not name else name
                items.append(ListItem(Label(f"{i} {label}  {desc}")))
            yield ListView(*items, id="git-profile-list")

    def on_mount(self) -> None:
        # Default-highlight; focus is handled by AUTO_FOCUS.
        self.query_one(ListView).index = self._default_index

    def action_pick(self) -> None:
        lv = self.query_one(ListView)
        idx = lv.index or 0
        self._select(idx)

    def action_pick_digit(self, n: int) -> None:
        self._select(n)

    def _select(self, idx: int) -> None:
        picked = pick_index(self.rows, idx)
        if picked is not None:
            self.dismiss(picked)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        lv = self.query_one(ListView)
        idx = lv.index or 0
        self._select(idx)

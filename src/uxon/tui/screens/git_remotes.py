"""GitRemotesScreen — read-only view of git_remote_profiles table.

Row tuple shape:
    (name, host, owner, auth, creds_user_display, visibility, token_file)
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header

from ..keymap import bindings_with_aliases


class GitRemotesScreen(ModalScreen[None]):
    DEFAULT_CSS = """
    GitRemotesScreen {
        layout: vertical;
    }
    #git-remotes-table {
        width: 1fr;
        height: 1fr;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = bindings_with_aliases(
        Binding("escape", "back", "Back", show=True),
        Binding("q", "back", "Back", show=False),
    )

    def __init__(self, rows: list[tuple]) -> None:
        super().__init__()
        self.rows = list(rows)

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="git-remotes-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one(DataTable)
        t.add_columns("NAME", "HOST", "OWNER", "AUTH", "CREDS", "VISIBILITY", "TOKEN")
        for row in self.rows:
            # Pad the row to 7 columns if callers supply fewer.
            padded = list(row) + [""] * (7 - len(row))
            t.add_row(*padded[:7])
        t.focus()

    def action_back(self) -> None:
        self.dismiss(None)

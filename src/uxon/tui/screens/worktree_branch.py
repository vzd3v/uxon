"""WorktreeBranchScreen — one Input for a new worktree's branch name.

Dismiss value: the entered branch name (stripped) or ``None`` on cancel.
Unlike NewProjectScreen, slashes are allowed — git branch names routinely
contain ``/`` (``feature/auth``). BINDINGS-only key handling (no on_key),
per the AGENTS.md drift guard.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from ..keymap import bindings_with_aliases


def worktree_branch_valid(value: str) -> bool:
    """Accept a non-empty branch name. Permits ``/`` (unlike project names).

    Rejects only what git itself forbids cheaply up front: empty, leading
    ``-``, whitespace, and the obvious bad tokens; git's own ``worktree
    add`` is the authority for the rest (and surfaces a clear error via
    plan_worktree_launch's §8 handling).
    """
    name = value.strip()
    if not name or name.startswith("-"):
        return False
    if name in (".", ".."):
        return False
    return not any(c.isspace() for c in name)


class WorktreeBranchScreen(ModalScreen["str | None"]):
    DEFAULT_CSS = """
    WorktreeBranchScreen { align: center middle; }
    WorktreeBranchScreen > Vertical {
        width: 64; height: auto; padding: 1 2;
        border: round $accent; background: $surface;
    }
    WorktreeBranchScreen .title { text-style: bold; margin-bottom: 1; }
    """

    BINDINGS: ClassVar[list[Binding]] = bindings_with_aliases(
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("enter", "submit", "Create", show=True, priority=True),
    )

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("New worktree — branch name", classes="title")
            yield Input(placeholder="feature/auth", id="branch-input")

    def on_mount(self) -> None:
        self.query_one("#branch-input", Input).focus()

    def action_submit(self) -> None:
        value = self.query_one("#branch-input", Input).value.strip()
        if not worktree_branch_valid(value):
            self.app.notify("Enter a valid branch name.", severity="warning", timeout=4)
            return
        self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)

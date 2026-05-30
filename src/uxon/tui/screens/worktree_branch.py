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
from textual.widgets import Input, Static

from ..keymap import bindings_with_aliases
from .modal_base import CardModal


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


class WorktreeBranchScreen(CardModal["str | None"]):
    # Card chrome + Esc→cancel come from CardModal; only width is custom.
    DEFAULT_CSS = """
    WorktreeBranchScreen .modal-card { width: 64; }
    """

    BINDINGS: ClassVar[list[Binding]] = bindings_with_aliases(
        Binding("enter", "submit", "Create", show=True, priority=True),
    )

    # Framework-managed initial focus (rationale: SessionChoiceScreen).
    AUTO_FOCUS = "#branch-input"

    def compose(self) -> ComposeResult:
        with self.card():
            yield Static("New worktree — branch name", classes="title")
            yield Input(placeholder="feature/auth", id="branch-input")

    def action_submit(self) -> None:
        value = self.query_one("#branch-input", Input).value.strip()
        if not worktree_branch_valid(value):
            self.app.notify("Enter a valid branch name.", severity="warning", timeout=4)
            return
        self.dismiss(value)

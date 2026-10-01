"""Choose a workspace after resolving the launch profile's filesystem access."""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import ListItem, ListView, Static

from uxon.infra.worktrees import Workspace

from ..keymap import bindings_with_aliases
from .modal_base import CardModal


class WorkspaceScreen(CardModal["tuple | None"]):
    DEFAULT_CSS = """
    WorkspaceScreen .modal-card { width: 72; }
    WorkspaceScreen ListView { height: auto; max-height: 20; }
    """
    BINDINGS: ClassVar[list[Binding]] = bindings_with_aliases(
        Binding("enter", "submit", "Select", show=True, priority=True),
    )
    AUTO_FOCUS = "#workspace-list"

    def __init__(self, workspaces: list[Workspace], *, repo_root: str) -> None:
        super().__init__()
        self._workspaces = tuple(workspaces)
        self._repo_root = repo_root

    def compose(self) -> ComposeResult:
        with self.card():
            yield Static("Workspace", classes="title")
            items = [
                ListItem(
                    Static(f"{w.label}  (primary)" if w.is_primary else w.label),
                    id=f"workspace-{index}",
                )
                for index, w in enumerate(self._workspaces)
            ]
            items.append(ListItem(Static("+ New worktree…"), id="workspace-new"))
            yield ListView(*items, id="workspace-list")

    def action_submit(self) -> None:
        selected = self.query_one("#workspace-list", ListView).highlighted_child
        if selected is None or selected.id is None:
            return
        if selected.id == "workspace-new":
            self.dismiss(("new", None))
            return
        workspace = self._workspaces[int(selected.id.removeprefix("workspace-"))]
        choice = (
            ("primary", self._repo_root)
            if workspace.is_primary
            else ("worktree", workspace.path, workspace.branch)
        )
        self.dismiss(choice)

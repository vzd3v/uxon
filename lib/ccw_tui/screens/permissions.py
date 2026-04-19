"""PermissionsScreen — regular vs ``--dangerously-skip-permissions``.

Dismiss values:
  - ``False`` — regular (safe) permissions.
  - ``True`` — all-permissions (``--dsp``).
  - ``None`` — user cancelled.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class PermissionsScreen(ModalScreen["bool | None"]):
    DEFAULT_CSS = """
    PermissionsScreen {
        align: center middle;
    }
    PermissionsScreen > Vertical {
        width: 60;
        height: auto;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    PermissionsScreen .title {
        text-style: bold;
        margin-bottom: 1;
    }
    PermissionsScreen Button {
        width: 100%;
        margin-bottom: 1;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("1", "pick_regular", "Regular", show=True),
        Binding("2", "pick_dsp", "All perms", show=True),
    ]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Launch permissions", classes="title")
            yield Button(
                "1 Regular permissions  (default, safe mode)",
                variant="primary",
                id="btn-regular",
            )
            yield Button(
                "2 ALL PERMISSIONS  (--dangerously-skip-permissions)",
                variant="warning",
                id="btn-dsp",
            )

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_pick_regular(self) -> None:
        self.dismiss(False)

    def action_pick_dsp(self) -> None:
        self.dismiss(True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-regular":
            self.dismiss(False)
        elif event.button.id == "btn-dsp":
            self.dismiss(True)

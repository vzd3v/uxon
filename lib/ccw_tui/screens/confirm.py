"""Generic confirmation modals.

Two shapes:
  - :class:`ConfirmYesNo` — yes/no buttons + y/n keyboard.
  - :class:`ConfirmPhrase` — user must type the phrase verbatim
    (matches the legacy ``kill-all`` / ``kill-all-global`` gesture).

Both return ``True`` on positive confirmation, ``False`` on cancel.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from ..state import confirm_phrase_matches


class ConfirmYesNo(ModalScreen[bool]):
    """Yes/No confirmation. ``y`` = yes, ``n``/``Esc`` = no."""

    DEFAULT_CSS = """
    ConfirmYesNo {
        align: center middle;
    }
    ConfirmYesNo > Vertical {
        width: 60;
        height: auto;
        padding: 1 2;
        border: round $warning;
        background: $surface;
    }
    ConfirmYesNo .prompt {
        text-style: bold;
        margin-bottom: 1;
    }
    ConfirmYesNo .buttons {
        height: auto;
        align: center middle;
    }
    ConfirmYesNo Button {
        margin: 0 1;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("y", "confirm", "Yes", show=True),
        Binding("n", "cancel", "No", show=True),
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("up", "app.focus_previous", "", show=False),
        Binding("down", "app.focus_next", "", show=False),
        Binding("left", "app.focus_previous", "", show=False),
        Binding("right", "app.focus_next", "", show=False),
    ]

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.prompt, classes="prompt")
            with Horizontal(classes="buttons"):
                yield Button("Yes", variant="error", id="yes")
                yield Button("No", variant="primary", id="no")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class ConfirmPhrase(ModalScreen[bool]):
    """User must type ``phrase`` verbatim, then Enter to confirm."""

    DEFAULT_CSS = """
    ConfirmPhrase {
        align: center middle;
    }
    ConfirmPhrase > Vertical {
        width: 70;
        height: auto;
        padding: 1 2;
        border: round $error;
        background: $surface;
    }
    ConfirmPhrase .prompt {
        text-style: bold;
        color: $error;
        margin-bottom: 1;
    }
    ConfirmPhrase Input {
        margin-top: 1;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def __init__(self, prompt: str, phrase: str) -> None:
        super().__init__()
        self.prompt = prompt
        self.phrase = phrase

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.prompt, classes="prompt")
            yield Static(f"Type '{self.phrase}' to confirm:")
            yield Input(id="confirm-input")

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(confirm_phrase_matches(event.value, self.phrase))

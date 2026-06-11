"""Generic confirmation modals.

Two shapes:
  - :class:`ConfirmYesNo` — yes/no buttons + y/n keyboard.
  - :class:`ConfirmPhrase` — user must type the phrase verbatim
    (the ``kill-all`` / ``kill-all-global`` destructive gesture).

Both return ``True`` on positive confirmation, ``False`` on cancel.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Button, Input, Static

from ..keymap import bindings_with_aliases
from ..state import confirm_phrase_matches
from .modal_base import ButtonCardModal, CardModal


class ConfirmYesNo(ButtonCardModal[bool]):
    """Yes/No confirmation. ``y`` = yes, ``n``/``Esc`` = no.

    Card chrome and arrow focus cycling come from ButtonCardModal; the
    card border is recoloured to ``$warning`` and the ``Esc`` cancel is
    hidden from the footer (``y``/``n`` are the advertised gesture).
    """

    DEFAULT_CSS = """
    ConfirmYesNo .modal-card {
        width: 60;
        border: round $warning;
    }
    ConfirmYesNo .prompt {
        text-style: bold;
        margin-bottom: 1;
    }
    """

    # Esc dismisses with ``False`` (decline).
    CANCEL_RESULT = False

    BINDINGS: ClassVar[list[Binding]] = bindings_with_aliases(
        Binding("y", "confirm", "Yes", show=True),
        Binding("n", "cancel", "No", show=True),
        Binding("escape", "cancel", "Cancel", show=False),
    )

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with self.card():
            yield Static(self.prompt, classes="prompt")
            with Horizontal(classes="buttons"):
                yield Button("Yes", variant="error", id="yes")
                yield Button("No", variant="primary", id="no")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class ConfirmPhrase(CardModal[bool]):
    """User must type ``phrase`` verbatim, then Enter to confirm.

    A single-Input card (no buttons → plain CardModal). Card chrome and
    ``Esc``→cancel come from the base; only the ``$error`` recolour and
    the prompt/input spacing are local.
    """

    DEFAULT_CSS = """
    ConfirmPhrase .modal-card {
        width: 70;
        border: round $error;
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

    # Esc dismisses with ``False`` (decline).
    CANCEL_RESULT = False

    def __init__(self, prompt: str, phrase: str) -> None:
        super().__init__()
        self.prompt = prompt
        self.phrase = phrase

    def compose(self) -> ComposeResult:
        with self.card():
            yield Static(self.prompt, classes="prompt")
            yield Static(f"Type '{self.phrase}' to confirm:")
            yield Input(id="confirm-input")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(confirm_phrase_matches(event.value, self.phrase))

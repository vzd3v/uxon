"""NewProjectScreen — text input for new project name.

Dismiss values:
  - ``str`` (non-empty) — the chosen project name.
  - ``None`` — user cancelled.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.validation import ValidationResult, Validator
from textual.widgets import Button, Input, Static

from ..state import project_name_error, project_name_valid
from .modal_base import ButtonCardModal


def _validate_project_name(value: str) -> bool:
    return project_name_valid(value)


class _ProjectNameValidator(Validator):
    def validate(self, value: str) -> ValidationResult:
        if project_name_valid(value):
            return self.success()
        return self.failure(project_name_error(value))


class NewProjectScreen(ButtonCardModal["str | None"]):
    # Card chrome, Esc→cancel and arrow focus cycling come from
    # ButtonCardModal; only the width and the error line are custom.
    DEFAULT_CSS = """
    NewProjectScreen .modal-card {
        width: 70;
    }
    NewProjectScreen .error {
        color: $error;
        height: 1;
    }
    """

    def __init__(self, project_root: str) -> None:
        super().__init__()
        self.project_root = project_root

    def compose(self) -> ComposeResult:
        with self.card():
            yield Static("Create new project", classes="title")
            yield Static(f"Directory: {self.project_root}/")
            yield Input(
                id="name-input",
                placeholder="project name",
                validators=[_ProjectNameValidator()],
                validate_on=["changed", "submitted"],
            )
            yield Static("", id="error-label", classes="error")
            with Horizontal(classes="buttons"):
                yield Button("Submit", variant="primary", id="submit")
                yield Button("Cancel", id="cancel")

    def on_input_changed(self, event: Input.Changed) -> None:
        label = self.query_one("#error-label", Static)
        if event.validation_result and not event.validation_result.is_valid:
            label.update(event.validation_result.failure_descriptions[0])
        else:
            label.update("")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._try_submit(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "submit":
            inp = self.query_one("#name-input", Input)
            self._try_submit(inp.value)
        elif event.button.id == "cancel":
            self.dismiss(None)

    def _try_submit(self, value: str) -> None:
        name = value.strip()
        if _validate_project_name(value):
            self.dismiss(name)
        else:
            label = self.query_one("#error-label", Static)
            label.update(project_name_error(value))

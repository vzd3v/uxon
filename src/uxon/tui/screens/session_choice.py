"""SessionChoiceScreen — pick attach vs. start-new when sessions exist.

Pushed by the launch flows (``_launch_cwd`` / ``_launch_new`` /
``_launch_existing``) after the operator picks agent + permission mode,
when the probe callback reports one or more compatible sessions for the
target directory. Lets the operator either attach to one of the existing
sessions or knowingly start a parallel one — replaces the previous
silent auto-attach in the planner.

Dismiss values:
  - ``("attach", session_name)`` — attach to the highlighted session.
  - ``("new", None)`` — start a new (parallel) session.
  - ``None`` — cancel; abort the launch action.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Button, Label, ListItem, ListView, Static

from ..keymap import bindings_with_aliases
from .modal_base import CardModal


def _row_label(name: str, attached: bool) -> str:
    """Render one existing-session row."""
    marker = " (attached)" if attached else ""
    return f"{name}{marker}"


SessionChoiceResult = tuple[str, str | None] | None


class SessionChoiceScreen(CardModal[SessionChoiceResult]):
    """Modal asking attach-vs-new when compatible sessions already exist.

    The list shows every compatible session (one row each); the operator
    moves the highlight with ↑/↓ and confirms with ``a`` (or Enter) to
    attach to the highlighted session, or ``n`` to start a new parallel
    one. ``Esc`` cancels the launch entirely. Mouse: clicking a row picks
    "attach" for that row; the two buttons at the bottom mirror the ``a``
    / ``n`` keyboard shortcuts for users who prefer pointing.
    """

    # Card chrome (centred card, title, Esc→cancel) comes from CardModal;
    # only the width and the ListView sizing are screen-specific.
    DEFAULT_CSS = """
    SessionChoiceScreen .modal-card {
        width: 72;
        max-height: 80%;
    }
    SessionChoiceScreen ListView {
        height: auto;
        min-height: 3;
        max-height: 12;
        margin-bottom: 1;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = bindings_with_aliases(
        Binding("a", "attach", "Attach", show=True, priority=True),
        Binding("enter", "attach", "Attach", show=False, priority=True),
        Binding("n", "new_alongside", "New", show=True, priority=True),
    )

    # Initial focus via Textual's declarative AUTO_FOCUS — the framework
    # applies it at the right lifecycle moment (screen compose + resume),
    # NOT synchronously in ``on_mount``. A synchronous ``focus()`` races
    # screen activation: when this modal is pushed from another modal's
    # dismiss callback, the popped screen's deferred focus-restoration
    # fires afterwards and steals focus to a background widget, leaving
    # the modal keyboard-dead. Every modal in this package follows this
    # rule; this docstring is the canonical "why".
    AUTO_FOCUS = "#session-list"

    def __init__(
        self,
        target_label: str,
        existing: tuple[tuple[str, bool], ...],
    ) -> None:
        super().__init__()
        # ``target_label`` is the short, user-facing description of what's
        # being opened (cwd path, or project name). Display-only.
        self.target_label = target_label
        self.existing = tuple(existing)

    def compose(self) -> ComposeResult:
        with self.card():
            count = len(self.existing)
            noun = "session" if count == 1 else "sessions"
            yield Static(
                f"Existing {noun} for this project ({count})",
                classes="title",
            )
            yield Static(self.target_label, classes="desc")
            items = [
                ListItem(Label(_row_label(name, attached)), id=f"sess-{idx}")
                for idx, (name, attached) in enumerate(self.existing)
            ]
            yield ListView(*items, id="session-list")
            with Horizontal(classes="buttons"):
                yield Button("Attach", variant="primary", id="attach")
                yield Button("New alongside", variant="warning", id="new")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        # Default-highlight the first row; focus is handled by AUTO_FOCUS.
        if self.existing:
            self.query_one("#session-list", ListView).index = 0

    def _highlighted_name(self) -> str | None:
        if not self.existing:
            return None
        lv = self.query_one("#session-list", ListView)
        idx = lv.index if lv.index is not None else 0
        if 0 <= idx < len(self.existing):
            return self.existing[idx][0]
        return None

    def action_attach(self) -> None:
        name = self._highlighted_name()
        if name is None:
            # Defensive — modal should never be pushed with empty list.
            self.dismiss(None)
            return
        self.dismiss(("attach", name))

    def action_new_alongside(self) -> None:
        self.dismiss(("new", None))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        # Mouse click on a row → attach to that row. (Enter is captured
        # by the screen-level priority binding above and routes through
        # ``action_attach`` without ever reaching the ListView.)
        self.action_attach()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "attach":
            self.action_attach()
        elif event.button.id == "new":
            self.action_new_alongside()
        else:
            self.action_cancel()

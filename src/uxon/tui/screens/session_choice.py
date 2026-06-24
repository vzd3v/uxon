"""SessionChoiceScreen — pick attach vs. start-new when sessions exist.

Pushed by the launch flows (``_launch_cwd`` / ``_launch_new`` /
``_launch_existing``) after the operator picks agent + permission mode,
when the probe callback reports one or more compatible sessions for the
target directory. Lets the operator either attach to one of the existing
sessions or knowingly start a parallel one — replaces the previous
silent auto-attach in the planner.

A pure-``ListView`` card, identical in shape to the other list modals
(``GitProfileScreen``, ``ExistingProjectScreen``): the compatible
sessions are rows, and a final ``+ Start new session alongside`` row
carries the parallel-launch action — the same sentinel/extra-row idiom
``GitProfileScreen`` (skip row) and ``LaunchOptionsScreen`` (``+ New
worktree…`` row) already use. ↑/↓ move the highlight, Enter confirms the
highlighted row, Esc cancels. No button row: it would be the only
list+buttons modal in the package, and its buttons could not be operated
by keyboard (no focus cycling on a list card; a priority Enter binding
stole the gesture).

Dismiss values:
  - ``("attach", session_name, user)`` — attach to the highlighted session.
  - ``("new", None, None)`` — start a new (parallel) session.
  - ``None`` — cancel; abort the launch action.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Label, ListItem, ListView, Static

from ..keymap import bindings_with_aliases
from .modal_base import CardModal


def _row_label(name: str, attached: bool) -> str:
    """Render one existing-session row."""
    marker = " (attached)" if attached else ""
    return f"{name}{marker}"


# Label for the trailing action row that starts a parallel session.
_NEW_ROW_LABEL = "+ Start new session alongside"


SessionChoiceRow = tuple[str, bool] | tuple[str, str, bool]
SessionChoiceResult = tuple[str, str | None, str | None] | None


class SessionChoiceScreen(CardModal[SessionChoiceResult]):
    """Modal asking attach-vs-new when compatible sessions already exist.

    The list shows every compatible session (one row each) followed by a
    ``+ Start new session alongside`` row. The operator moves the
    highlight with ↑/↓ and confirms with Enter: a session row attaches to
    that session, the trailing row starts a new parallel one. ``Esc``
    cancels the launch entirely. ``n`` is a hidden shortcut for the
    new-alongside action. Mouse: clicking any row activates it.
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
    SessionChoiceScreen .hint {
        color: $text-muted;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = bindings_with_aliases(
        # Enter confirms the highlighted row; ``n`` is a hidden quick-path
        # to the new-alongside action. ``priority=True`` so the screen
        # owns Enter before the ListView's own Selected does: this modal is
        # pushed from another modal's dismiss callback, and the Enter that
        # dismissed the prior modal can otherwise leak into the freshly
        # mounted ListView and auto-confirm a row. The screen-level
        # priority binding absorbs that stray gesture (as ExistingProjectScreen
        # does); mouse clicks still confirm via on_list_view_selected.
        Binding("enter", "pick", "Select", show=True, priority=True),
        Binding("n", "new_alongside", "New session", show=False),
    )

    # Initial focus via Textual's declarative AUTO_FOCUS — the framework
    # applies it at the right lifecycle moment (screen compose + resume),
    # NOT synchronously in ``on_mount``. A synchronous ``focus()`` races
    # screen activation: when this modal is pushed from another modal's
    # dismiss callback, the popped screen's deferred focus-restoration
    # fires afterwards and steals focus to a background widget, leaving
    # the modal keyboard-dead. Every card modal in this package follows
    # this rule (LaunchOptionsScreen is the exception — its focus is
    # dynamic across panels, driven imperatively). This docstring is the
    # canonical "why".
    AUTO_FOCUS = "#session-list"

    def __init__(
        self,
        target_label: str,
        existing: tuple[SessionChoiceRow, ...],
    ) -> None:
        super().__init__()
        # ``target_label`` is the short, user-facing description of what's
        # being opened (cwd path, or project name). Display-only.
        self.target_label = target_label
        self.existing = tuple(
            (row[0], row[1], row[2]) if len(row) == 3 else ("", row[0], row[1]) for row in existing
        )

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
                for idx, (_user, name, attached) in enumerate(self.existing)
            ]
            # Trailing action row — the new-alongside choice, mirroring
            # LaunchOptionsScreen's "+ New worktree…" sentinel row.
            items.append(ListItem(Label(_NEW_ROW_LABEL), id="sess-new"))
            yield ListView(*items, id="session-list")
            yield Static(
                "↑/↓ select · enter confirm · esc cancel",
                classes="hint",
            )

    def on_mount(self) -> None:
        # Default-highlight the first row; focus is handled by AUTO_FOCUS.
        self.query_one("#session-list", ListView).index = 0

    def action_pick(self) -> None:
        """Confirm the highlighted row: session → attach, last → new."""
        lv = self.query_one("#session-list", ListView)
        idx = lv.index if lv.index is not None else 0
        if 0 <= idx < len(self.existing):
            user, name, _attached = self.existing[idx]
            self.dismiss(("attach", name, user or None))
            return
        # The trailing row (or any out-of-range index) → start new.
        self.dismiss(("new", None, None))

    def action_new_alongside(self) -> None:
        self.dismiss(("new", None, None))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        # Mouse click on a row → confirm it. Keyboard Enter is taken by the
        # screen-level ``priority`` binding (``action_pick``) before the
        # ListView can fire Selected, so this path is mouse-only.
        self.action_pick()

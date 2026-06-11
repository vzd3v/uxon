"""Shared base classes for the package's centred-card modals.

Every modal in this package renders the same visual *card* — a centred
``Vertical`` with a rounded border, surface background and a bold title —
and shares the same ``Esc`` cancel gesture. Before this module that chrome
(CSS + the ``escape`` binding + ``action_cancel``) was copy-pasted into
nine screens, drifting in width/padding and inviting bugs. It now lives
here once.

Two layers, by responsibility:

  - :class:`CardModal` — the card chrome and ``Esc`` → cancel. Used by
    every modal. The card container must carry the ``modal-card`` class
    (see :meth:`CardModal.card`); subclasses override only what genuinely
    differs (``width``, and the border colour for warning/error modals)
    via a higher-specificity ``<Subclass> .modal-card`` rule.

  - :class:`ButtonCardModal` — adds arrow-key focus cycling between the
    Input/buttons of a *form* card. List-form modals (a ``ListView`` whose
    own ↑/↓ drive selection) deliberately extend :class:`CardModal`
    directly so their arrows are not stolen by focus cycling.

Initial focus is handled by Textual's declarative ``AUTO_FOCUS`` on each
concrete screen — see the canonical rationale in
:mod:`...screens.session_choice`. It is intentionally not set here: the
base does not know which child should own focus.
"""

from __future__ import annotations

from typing import ClassVar, TypeVar

from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen

from ..keymap import bindings_with_aliases

# Parametrise the dismiss type so concrete modals keep their precise
# result type (e.g. ``CardModal[str | None]``). Plain ``TypeVar`` rather
# than PEP 695 syntax — the project targets Python >= 3.11.
_ResultType = TypeVar("_ResultType")


class CardModal(ModalScreen[_ResultType]):
    """A centred card modal with a shared ``Esc`` → cancel gesture.

    The card chrome is keyed on the ``modal-card`` class (applied via
    :meth:`card`) so subclasses can override ``width``/``border`` with a
    reliably-higher specificity. ``Esc`` dismisses with
    :attr:`CANCEL_RESULT` (``None`` by default; set ``False`` on
    ``ModalScreen[bool]`` subclasses). Subclasses needing custom cancel
    logic (e.g. "clear the filter first") override :meth:`action_cancel`.
    """

    DEFAULT_CSS = """
    CardModal {
        align: center middle;
    }
    CardModal .modal-card {
        height: auto;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    CardModal .title {
        text-style: bold;
        margin-bottom: 1;
    }
    CardModal .desc {
        color: $text-muted;
        margin-bottom: 1;
    }
    CardModal .buttons {
        height: auto;
        align: center middle;
        margin-top: 1;
    }
    CardModal Button {
        margin: 0 1;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = bindings_with_aliases(
        Binding("escape", "cancel", "Cancel", show=True),
    )

    # Dismiss value for the shared ``Esc`` / cancel path. ``None`` suits the
    # ``str | None`` and tuple-result modals; ``ModalScreen[bool]`` ones set
    # this to ``False``.
    CANCEL_RESULT: ClassVar = None

    @staticmethod
    def card(*children, **kwargs) -> Vertical:
        """Build the card container with the shared ``modal-card`` class.

        Use as the outer ``with`` of ``compose`` so the chrome CSS applies:
        ``with self.card(): ...``.
        """
        classes = ("modal-card " + kwargs.pop("classes", "")).strip()
        return Vertical(*children, classes=classes, **kwargs)

    def action_cancel(self) -> None:
        self.dismiss(self.CANCEL_RESULT)


class ButtonCardModal(CardModal[_ResultType]):
    """A :class:`CardModal` whose form fields are arrow-navigable.

    Adds ↑/↓ and ←/→ focus cycling so the operator can move between the
    Input and the Save/Cancel (or Yes/No) buttons without the mouse. Only
    button/input *form* cards extend this; list cards keep their arrows
    bound to the ``ListView``.
    """

    BINDINGS: ClassVar[list[Binding]] = bindings_with_aliases(
        Binding("up", "app.focus_previous", "", show=False),
        Binding("down", "app.focus_next", "", show=False),
        Binding("left", "app.focus_previous", "", show=False),
        Binding("right", "app.focus_next", "", show=False),
    )

"""Per-kind edit modals for :class:`~uxon.tui.screens.settings.SettingsScreen`.

One modal per setting kind (bool / enum / string / number / array /
table). :class:`SettingsScreen` picks the right class by
``entry.spec.kind`` and pushes it; each persists through the
:class:`~uxon.tui.screens.settings.SettingsCallbacks` glue passed in.
Split out of ``settings.py`` so that file owns only the screen.
"""

from __future__ import annotations

from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import (
    Button,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
)

from .modal_base import ButtonCardModal


class _EditModalBase(ButtonCardModal[bool]):
    """Shared base for the per-kind settings edit modals.

    Inherits the centred-card chrome and ↑/↓/←/→ focus cycling from
    :class:`ButtonCardModal`; ``Esc`` dismisses with ``False`` (no
    change). Only the card width differs from the package default.
    """

    DEFAULT_CSS = """
    _EditModalBase .modal-card {
        width: 80;
        max-height: 30;
    }
    """

    CANCEL_RESULT = False

    def __init__(self, entry: Any, cbs: Any) -> None:
        super().__init__()
        self.entry = entry
        self.cbs = cbs

    def _try_save(self, new_value: Any) -> None:
        try:
            self.cbs.save_setting(self.entry.spec.key, new_value)
        except Exception as exc:
            self.app.notify(f"Save failed: {exc}", severity="error", timeout=6)
            self.dismiss(False)
            return
        self.dismiss(True)

    def _title(self) -> str:
        return f"Edit {self.entry.spec.key}"


class BoolToggleModal(_EditModalBase):
    AUTO_FOCUS = "#true"

    def compose(self) -> ComposeResult:
        with self.card():
            yield Static(self._title(), classes="title")
            yield Static(self.entry.spec.description, classes="desc")
            with Horizontal(classes="buttons"):
                yield Button("True", variant="success", id="true")
                yield Button("False", variant="error", id="false")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self._try_save(event.button.id == "true")


class EnumCycleModal(_EditModalBase):
    AUTO_FOCUS = "#enum-list"

    def compose(self) -> ComposeResult:
        choices = self.entry.spec.choices or ()
        current = self.entry.value
        with self.card():
            yield Static(self._title(), classes="title")
            yield Static(self.entry.spec.description, classes="desc")
            items = []
            for c in choices:
                items.append(ListItem(Label(c)))
            lv = ListView(*items, id="enum-list")
            self._lv_default = 0
            for i, c in enumerate(choices):
                if c == current:
                    self._lv_default = i
            yield lv

    def on_mount(self) -> None:
        # Default-highlight; focus is handled by AUTO_FOCUS.
        self.query_one(ListView).index = self._lv_default

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        choices = self.entry.spec.choices or ()
        idx = self.query_one(ListView).index or 0
        if 0 <= idx < len(choices):
            self._try_save(choices[idx])


class _ValueInputModal(_EditModalBase):
    """Single-line text Input edited into a parsed value.

    Template method: the layout (title, description, Input, Save/Cancel)
    and submit wiring live here; subclasses supply only the per-kind
    behaviour —

      - :meth:`_initial_text` — how the current value prefills the field
        (default: ``str(value)``);
      - :meth:`_parse` — raw string → value to persist; raise
        ``ValueError`` to reject with a notification and stay open;
      - :meth:`_commit` — how the parsed value persists (default:
        ``save_setting`` via :meth:`_EditModalBase._try_save`).

    The Input id is unified (``value-input``) since there is exactly one
    per modal — ``AUTO_FOCUS`` therefore lives here, not per subclass.
    """

    AUTO_FOCUS = "#value-input"

    # Appended to the description line (e.g. an input-format hint).
    desc_suffix: ClassVar[str] = ""

    def _initial_text(self) -> str:
        # ``or ""`` would render a falsy-but-real value (notably ``0`` /
        # ``0.0`` for a number setting) as a blank field; guard on None.
        v = self.entry.value
        return "" if v is None else str(v)

    def _parse(self, raw: str) -> Any:
        raise NotImplementedError

    def _commit(self, value: Any) -> None:
        self._try_save(value)

    def compose(self) -> ComposeResult:
        with self.card():
            yield Static(self._title(), classes="title")
            yield Static(
                f"{self.entry.spec.description}{self.desc_suffix}",
                classes="desc",
            )
            yield Input(value=self._initial_text(), id="value-input")
            with Horizontal(classes="buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    def _submit(self, raw: str) -> None:
        try:
            value = self._parse(raw)
        except ValueError as exc:
            self.app.notify(str(exc) or "Invalid value.", severity="error", timeout=4)
            return
        self._commit(value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self._submit(self.query_one(Input).value)
        elif event.button.id == "cancel":
            self.dismiss(False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit(event.value)


class StringInputModal(_ValueInputModal):
    def _parse(self, raw: str) -> str:
        # Verbatim — strings are saved unstripped (whitespace may matter).
        return raw


class NumberInputModal(_ValueInputModal):
    def _parse(self, raw: str) -> float:
        try:
            return float(raw)
        except ValueError:
            raise ValueError("Expected a number.") from None


class ArrayCsvModal(_ValueInputModal):
    desc_suffix = "  (comma-separated)"

    def _initial_text(self) -> str:
        return ", ".join(self.entry.value or [])

    def _parse(self, raw: str) -> list[str]:
        return [p.strip() for p in raw.split(",") if p.strip()]


class TableMappingModal(_ValueInputModal):
    """Edit a ``table`` setting as ``key=value,key=value`` CSV."""

    desc_suffix = "  (key=value, comma-separated)"

    def _initial_text(self) -> str:
        return ", ".join(f"{k}={v}" for k, v in sorted((self.entry.value or {}).items()))

    def _parse(self, raw: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for part in raw.split(","):
            part = part.strip()
            if not part or "=" not in part:
                continue
            k, _, v = part.partition("=")
            k = k.strip()
            if k:
                result[k] = v.strip()
        return result

    def _commit(self, mapping: dict[str, str]) -> None:
        # ``table`` settings persist through the dedicated mapping sink.
        try:
            self.cbs.save_mapping(self.entry.spec.key, mapping)
        except Exception as exc:
            self.app.notify(f"Save failed: {exc}", severity="error", timeout=6)
            self.dismiss(False)
            return
        self.dismiss(True)

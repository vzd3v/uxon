"""SettingsScreen + per-kind edit modals.

Reuses :class:`SettingEntry` from the ``uxon_settings`` module —
the TUI-facing I/O contract lives there; this file owns the UI.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
)

from ..context import CallbackError
from ..keymap import bindings_with_aliases
from .modal_base import ButtonCardModal

GIT_REMOTES_VIEW_LABEL = "Git remote profiles (view)"


@dataclass
class SettingsCallbacks:
    """Thin glue that the settings UI calls to persist changes."""

    get_entries: Callable[[], list]  # -> list[SettingEntry]
    save_setting: Callable[[str, Any], None]  # (key, new_value)
    remove_setting: Callable[[str], None]  # (key) — revert to default
    save_mapping: Callable[[str, dict], None]  # (key, new_mapping)
    # Optional: returns full profile rows for a read-only subscreen.
    get_git_remote_profile_rows: Callable[[], list[tuple]] | None = None


class SettingsScreen(Screen):
    """DataTable of all setting entries + a virtual 'Git remote profiles' row."""

    DEFAULT_CSS = """
    SettingsScreen {
        layout: vertical;
    }
    #settings-table {
        width: 1fr;
        height: 1fr;
    }
    #settings-description {
        height: 1;
        color: $text-muted;
        padding: 0 1;
    }
    #settings-credits {
        height: 1;
        color: $text-muted;
        padding: 0 1;
        text-align: right;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = bindings_with_aliases(
        Binding("escape", "back", "Back", show=True),
        Binding("q", "back", "Back", show=False),
        Binding("x", "reset", "Reset", show=True),
        Binding("enter", "edit", "Edit", show=True),
    )

    # Framework-managed initial focus (rationale: SessionChoiceScreen).
    AUTO_FOCUS = "#settings-table"

    def __init__(self, cbs: Any) -> None:
        super().__init__()
        self.cbs = cbs
        self._entries: list = []
        self._has_git_view = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="settings-table", cursor_type="row")
        yield Static("", id="settings-description")
        yield Static(
            "uxon — Vasily Zakharov <vz@vz.team> · github.com/vzd3v",
            id="settings-credits",
        )
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#settings-table", DataTable)
        t.add_columns("KEY", "VALUE", "SOURCE")
        self._reload()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Delegate ``Enter`` on the table to :meth:`action_edit`."""
        self.action_edit()

    def _reload(self) -> None:
        t = self.query_one("#settings-table", DataTable)
        cursor = t.cursor_row
        t.clear()
        try:
            self._entries = list(self.cbs.get_entries())
        except CallbackError as exc:
            self._entries = []
            self.app.notify(f"Settings load failed: {exc}", severity="error", timeout=6)
            return
        self._has_git_view = getattr(self.cbs, "get_git_remote_profile_rows", None) is not None
        if self._has_git_view:
            t.add_row(GIT_REMOTES_VIEW_LABEL, "(Enter to view)", "")
        for entry in self._entries:
            t.add_row(
                entry.spec.key,
                _format_value(entry),
                _source_text(entry.source),
            )
        # Restore cursor within bounds.
        total = (1 if self._has_git_view else 0) + len(self._entries)
        if total > 0:
            t.move_cursor(row=min(cursor, total - 1))

    def _selected_entry(self) -> Any | None:
        t = self.query_one("#settings-table", DataTable)
        row = t.cursor_row
        if self._has_git_view:
            if row == 0:
                return None  # git-view sentinel
            idx = row - 1
        else:
            idx = row
        if 0 <= idx < len(self._entries):
            return self._entries[idx]
        return None

    # ── Bindings ─────────────────────────────────────────────────────

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_edit(self) -> None:
        t = self.query_one("#settings-table", DataTable)
        if self._has_git_view and t.cursor_row == 0:
            # Virtual row → open git remotes read-only screen.
            from .git_remotes import GitRemotesScreen

            try:
                rows = self.cbs.get_git_remote_profile_rows()
            except CallbackError as exc:
                self.app.notify(
                    f"Git remotes load failed: {exc}",
                    severity="error",
                    timeout=6,
                )
                return
            self.app.push_screen(GitRemotesScreen(rows))
            return
        entry = self._selected_entry()
        if entry is None or not entry.editable:
            if entry is not None:
                self.app.notify("Read-only (project-level).", severity="warning")
            return
        kind = entry.spec.kind

        def after_edit(changed: bool | None) -> None:
            if changed:
                self._reload()

        modal: _EditModalBase | None = None
        if kind == "bool":
            modal = BoolToggleModal(entry, self.cbs)
        elif kind == "enum":
            modal = EnumCycleModal(entry, self.cbs)
        elif kind == "string":
            modal = StringInputModal(entry, self.cbs)
        elif kind == "number":
            modal = NumberInputModal(entry, self.cbs)
        elif kind == "array":
            modal = ArrayCsvModal(entry, self.cbs)
        elif kind == "table":
            modal = TableMappingModal(entry, self.cbs)
        else:
            self.app.notify(f"Unsupported kind: {kind}", severity="error")
            return
        self.app.push_screen(modal, after_edit)

    def action_reset(self) -> None:
        entry = self._selected_entry()
        if entry is None or not entry.editable:
            return
        try:
            self.cbs.remove_setting(entry.spec.key)
        except Exception as exc:  # Settings I/O errors bubble as generic.
            self.app.notify(f"Reset failed: {exc}", severity="error", timeout=6)
            return
        self.app.notify(f"Reset {entry.spec.key}")
        self._reload()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        entry = self._selected_entry()
        desc = self.query_one("#settings-description", Static)
        t = self.query_one("#settings-table", DataTable)
        if self._has_git_view and t.cursor_row == 0:
            desc.update("Read-only view of [[git_remote_profiles]].")
            return
        if entry is None:
            desc.update("")
            return
        desc.update(entry.spec.description)


# ── Helpers ─────────────────────────────────────────────────────────


def _format_value(entry: Any) -> str:
    v = entry.value
    kind = entry.spec.kind
    if kind == "bool":
        return "true" if v else "false"
    if kind == "table":
        if not v:
            return "(empty)"
        return ", ".join(f"{k}->{vv}" for k, vv in sorted(v.items()))
    if kind == "array":
        return ", ".join(v) if v else "(empty)"
    if v is None or v == "":
        return "(unset)"
    return str(v)


def _source_text(source: str) -> str:
    if source == "repo":
        return "repo"
    if source == "default":
        return "default"
    return source  # project:<path>


# ── Per-kind edit modals ─────────────────────────────────────────────


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

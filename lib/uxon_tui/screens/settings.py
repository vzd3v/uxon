"""SettingsScreen + per-kind edit modals.

Reuses :class:`SettingEntry` from the pre-existing ``uxon_settings``
module — the TUI-facing I/O contract is unchanged; only the UI is
rewritten. :class:`SettingsCallbacks` moved from the retired
``ccw_tui_settings`` module.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
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
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "back", "Back", show=True),
        Binding("q", "back", "Back", show=False),
        Binding("x", "reset", "Reset", show=True),
        Binding("enter", "edit", "Edit", show=True),
    ]

    def __init__(self, cbs: Any) -> None:
        super().__init__()
        self.cbs = cbs
        self._entries: list = []
        self._has_git_view = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="settings-table", cursor_type="row")
        yield Static("", id="settings-description")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#settings-table", DataTable)
        t.add_columns("KEY", "VALUE", "SOURCE")
        self._reload()
        t.focus()

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

        modal: ModalScreen | None = None
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


class _EditModalBase(ModalScreen[bool]):
    DEFAULT_CSS = """
    _EditModalBase, BoolToggleModal, EnumCycleModal,
    StringInputModal, NumberInputModal, ArrayCsvModal, TableMappingModal {
        align: center middle;
    }
    _EditModalBase > Vertical, BoolToggleModal > Vertical,
    EnumCycleModal > Vertical, StringInputModal > Vertical,
    NumberInputModal > Vertical, ArrayCsvModal > Vertical, TableMappingModal > Vertical {
        width: 80;
        max-height: 30;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    .title {
        text-style: bold;
        margin-bottom: 1;
    }
    .desc {
        color: $text-muted;
        margin-bottom: 1;
    }
    .buttons {
        height: auto;
        align: center middle;
        margin-top: 1;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("up", "app.focus_previous", "", show=False),
        Binding("down", "app.focus_next", "", show=False),
    ]

    def __init__(self, entry: Any, cbs: Any) -> None:
        super().__init__()
        self.entry = entry
        self.cbs = cbs

    def action_cancel(self) -> None:
        self.dismiss(False)

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
    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._title(), classes="title")
            yield Static(self.entry.spec.description, classes="desc")
            with Horizontal(classes="buttons"):
                yield Button("True", variant="success", id="true")
                yield Button("False", variant="error", id="false")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self._try_save(event.button.id == "true")


class EnumCycleModal(_EditModalBase):
    def compose(self) -> ComposeResult:
        choices = self.entry.spec.choices or ()
        current = self.entry.value
        with Vertical():
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
        lv = self.query_one(ListView)
        lv.index = self._lv_default
        lv.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        choices = self.entry.spec.choices or ()
        idx = self.query_one(ListView).index or 0
        if 0 <= idx < len(choices):
            self._try_save(choices[idx])


class StringInputModal(_EditModalBase):
    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._title(), classes="title")
            yield Static(self.entry.spec.description, classes="desc")
            yield Input(value=str(self.entry.value or ""), id="string-input")
            with Horizontal(classes="buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self._try_save(self.query_one(Input).value)
        elif event.button.id == "cancel":
            self.dismiss(False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._try_save(event.value)


class NumberInputModal(_EditModalBase):
    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._title(), classes="title")
            yield Static(self.entry.spec.description, classes="desc")
            yield Input(value=str(self.entry.value or ""), id="number-input")
            with Horizontal(classes="buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self._save_value(self.query_one(Input).value)
        elif event.button.id == "cancel":
            self.dismiss(False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._save_value(event.value)

    def _save_value(self, raw: str) -> None:
        try:
            value = float(raw)
        except ValueError:
            self.app.notify("Expected a number.", severity="error", timeout=4)
            return
        self._try_save(value)


class ArrayCsvModal(_EditModalBase):
    def compose(self) -> ComposeResult:
        current = ", ".join(self.entry.value or [])
        with Vertical():
            yield Static(self._title(), classes="title")
            yield Static(
                f"{self.entry.spec.description}  (comma-separated)",
                classes="desc",
            )
            yield Input(value=current, id="array-input")
            with Horizontal(classes="buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def _parse(self, s: str) -> list[str]:
        return [p.strip() for p in s.split(",") if p.strip()]

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self._try_save(self._parse(self.query_one(Input).value))
        elif event.button.id == "cancel":
            self.dismiss(False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._try_save(self._parse(event.value))


class TableMappingModal(_EditModalBase):
    """Edit a ``table`` setting as ``key=value,key=value`` CSV."""

    def compose(self) -> ComposeResult:
        current = ", ".join(f"{k}={v}" for k, v in sorted((self.entry.value or {}).items()))
        with Vertical():
            yield Static(self._title(), classes="title")
            yield Static(
                f"{self.entry.spec.description}  (key=value, comma-separated)",
                classes="desc",
            )
            yield Input(value=current, id="table-input")
            with Horizontal(classes="buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def _parse(self, s: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for part in s.split(","):
            part = part.strip()
            if not part:
                continue
            if "=" not in part:
                continue
            k, _, v = part.partition("=")
            k = k.strip()
            v = v.strip()
            if k:
                result[k] = v
        return result

    def _try_save_mapping(self, mapping: dict[str, str]) -> None:
        try:
            self.cbs.save_mapping(self.entry.spec.key, mapping)
        except Exception as exc:
            self.app.notify(f"Save failed: {exc}", severity="error", timeout=6)
            self.dismiss(False)
            return
        self.dismiss(True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self._try_save_mapping(self._parse(self.query_one(Input).value))
        elif event.button.id == "cancel":
            self.dismiss(False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._try_save_mapping(self._parse(event.value))

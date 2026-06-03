"""SettingsScreen — the settings DataTable host.

Reuses :class:`SettingEntry` from the ``uxon_settings`` module —
the TUI-facing I/O contract lives there; this file owns the UI. The
per-kind edit modals live in :mod:`uxon.tui.screens.settings_modals`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Static,
)

from ..context import CallbackError
from ..keymap import bindings_with_aliases
from .settings_modals import (
    ArrayCsvModal,
    BoolToggleModal,
    EnumCycleModal,
    NumberInputModal,
    StringInputModal,
    TableMappingModal,
    _EditModalBase,
)

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

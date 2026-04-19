"""SessionTable — a ``DataTable`` subclass for ccw session rows.

Populates from a list of :class:`TuiSession`. Colour cues:

  - Attached session  → name column gets ``.attached`` class (green).
  - CPU > 50%         → cpu cell gets ``.cpu-hot`` class (red).
  - CPU 10..50%       → cpu cell gets ``.cpu-warm`` class (yellow).
  - Other-user row    → user cell gets ``.sudo-only`` class (yellow).

CSS rules live in ``styles.tcss`` (T17). Until then the widget emits
rich :class:`Text` with inline styles so colour behaves identically
across the migration.
"""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.widgets import DataTable

from ..context import TuiSession


class SessionTable(DataTable):
    """Session-list DataTable with an opinionated :meth:`populate`.

    Columns (when ``show_user=False``):
        ``name pid cpu ram new last cmd path``

    Columns (when ``show_user=True``):
        ``user name pid cpu ram new last cmd path``
    """

    DEFAULT_CSS = """
    SessionTable {
        width: 1fr;
        height: auto;
    }
    SessionTable > .datatable--hover {
        background: $boost;
    }
    """

    def __init__(self, *, show_user: bool = False, id: str | None = None) -> None:
        super().__init__(id=id)
        self.cursor_type = "row"
        self.zebra_stripes = True
        self.show_user = show_user
        self._session_index: list[TuiSession] = []

    COLUMN_KEYS: ClassVar[list[str]] = [
        "name", "pid", "cpu", "ram", "new", "last", "cmd", "path",
    ]

    def on_mount(self) -> None:
        if self.show_user:
            self.add_column("USER", key="user")
        self.add_columns("NAME", "PID", "CPU", "RAM", "NEW", "LAST", "CMD", "PATH")

    def populate(self, sessions: list[TuiSession]) -> None:
        """Replace all rows with the given sessions. Preserves cursor."""
        prev_cursor = self.cursor_row
        self.clear()
        self._session_index = list(sessions)
        for s in sessions:
            name_text = Text(s.short)
            if s.attached:
                name_text = Text(s.short, style="bold green")
                name_text.append(" ●", style="green")
            cpu_text = self._cpu_cell(s.cpu)
            row = []
            if self.show_user:
                row.append(Text(s.user, style="bold yellow"))
            row.extend([
                name_text,
                s.pid,
                cpu_text,
                s.ram,
                s.created,
                s.last_activity,
                s.cmd,
                s.path,
            ])
            self.add_row(*row)
        # Restore cursor within bounds.
        if sessions:
            self.move_cursor(row=min(prev_cursor, len(sessions) - 1))

    @staticmethod
    def _cpu_cell(cpu: str) -> Text:
        if cpu in ("", "-"):
            return Text(cpu)
        try:
            v = float(cpu)
        except ValueError:
            return Text(cpu)
        if v > 50:
            return Text(cpu, style="bold red")
        if v > 10:
            return Text(cpu, style="yellow")
        return Text(cpu)

    def session_at(self, row: int) -> "TuiSession | None":
        if 0 <= row < len(self._session_index):
            return self._session_index[row]
        return None

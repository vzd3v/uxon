"""SessionTable — a ``DataTable`` subclass for uxon session rows.

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

from ..context import TuiSession
from .focus_releasing_data_table import FocusReleasingDataTable


class SessionTable(FocusReleasingDataTable):
    """Session-list DataTable with an opinionated :meth:`populate`.

    Columns (when ``show_user=False``, ``show_agent_column=False``):
        ``name pid cpu ram new last cmd path``

    Columns (when ``show_user=True``):
        ``user name pid cpu ram new last cmd path``

    Columns (when ``show_agent_column=True``):
        ``[user] name agent pid cpu ram new last cmd path``

    Boundary-aware navigation, cursor-on-focus visibility, and the
    base CSS (width / height / hover) are inherited from
    :class:`FocusReleasingDataTable`; subclasses only own column shape
    and row content.
    """

    def __init__(
        self,
        *,
        show_user: bool = False,
        show_agent_column: bool = False,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.show_user = show_user
        self.show_agent_column = show_agent_column
        self._session_index: list[TuiSession] = []

    COLUMN_KEYS: ClassVar[list[str]] = [
        "name",
        "pid",
        "cpu",
        "ram",
        "new",
        "last",
        "cmd",
        "path",
    ]

    @staticmethod
    def column_labels(*, show_user: bool, show_agent_column: bool) -> tuple[str, ...]:
        labels = []
        if show_user:
            labels.append("USER")
        labels.append("NAME")
        if show_agent_column:
            labels.append("AGENT")
        labels.extend(("PID", "CPU", "RAM", "NEW", "LAST", "CMD", "PATH"))
        return tuple(labels)

    def on_mount(self) -> None:
        for label in self.column_labels(
            show_user=self.show_user,
            show_agent_column=self.show_agent_column,
        ):
            self.add_column(label)

    @staticmethod
    def _agent_label(session: TuiSession) -> str:
        """Return the agent cell value for a session row."""
        if session.legacy and session.agent == "claude":
            return "claude (legacy)"
        return session.agent

    @staticmethod
    def _display_name(session: TuiSession) -> str:
        return session.stem if session.stem else session.short

    def populate(self, sessions: list[TuiSession]) -> None:
        """Replace all rows with the given sessions. Preserves cursor."""
        prev_cursor = self.cursor_row
        self.clear()
        self._session_index = list(sessions)
        for s in sessions:
            display_name = self._display_name(s)
            name_text = Text(display_name)
            if s.attached:
                name_text = Text(display_name, style="bold green")
                name_text.append(" ●", style="green")
            cpu_text = self._cpu_cell(s.cpu)
            row = []
            if self.show_user:
                row.append(Text(s.user, style="bold yellow"))
            row.append(name_text)
            if self.show_agent_column:
                row.append(self._agent_label(s))
            row.extend(
                [
                    s.pid,
                    cpu_text,
                    s.ram,
                    s.created,
                    s.last_activity,
                    s.cmd,
                    s.path,
                ]
            )
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

    def session_at(self, row: int) -> TuiSession | None:
        if 0 <= row < len(self._session_index):
            return self._session_index[row]
        return None

"""RemoteSessionTable — a DataTable for sessions polled from peer hosts.

Distinct from :class:`SessionTable` because the input shape is
different: rows here come from wire-schema :class:`SessionRecord`
dicts (i.e. the JSON payload of ``uxon list --json`` parsed on a
peer), not local :class:`TuiSession` dataclasses. Splitting this
out keeps the local table's column rules and population logic
unaffected by remote-side concerns (no PIDs to attach to, no
cross-user marker, no kill-from-here action).

A single instance renders **all** configured peers' sessions in one
flat table. The optional ``HOST`` column is shown when more than
one peer is configured, so a single-host setup looks just like a
local table with a "remote" header above it.

This widget is read-only: it does not drive attach/kill — those
need a remote SSH gesture not yet wired (deferred to a later
commit). For now the rows surface the data; activation is a no-op.
"""

from __future__ import annotations

from typing import Any, ClassVar

from rich.text import Text
from textual.widgets import DataTable


class RemoteSessionTable(DataTable):
    """DataTable rendering remote-host sessions from wire-schema records.

    Columns (when ``show_host=False``):
        ``user name agent cmd path``

    Columns (when ``show_host=True``):
        ``host user name agent cmd path``

    The ``populate`` method takes pairs of ``(host_name, record)`` so
    a multi-host snapshot can be flattened in one pass with stable
    sort order — host first, then session name.
    """

    DEFAULT_CSS = """
    RemoteSessionTable {
        width: 1fr;
        height: 1fr;
        min-height: 3;
    }
    RemoteSessionTable > .datatable--hover {
        background: $boost;
    }
    """

    COLUMN_KEYS: ClassVar[list[str]] = [
        "host",
        "user",
        "name",
        "agent",
        "cmd",
        "path",
    ]

    def __init__(self, *, show_host: bool = False, id: str | None = None) -> None:
        super().__init__(id=id)
        self.cursor_type = "none"
        self.zebra_stripes = True
        self.show_host = show_host
        # Each row in the table maps back to a (host_name, record) tuple
        # so a future remote-attach handler can identify what was clicked.
        self._row_index: list[tuple[str, dict[str, Any]]] = []

    def on_focus(self) -> None:
        self.cursor_type = "row"

    def on_blur(self) -> None:
        self.cursor_type = "none"

    @staticmethod
    def column_labels(*, show_host: bool) -> tuple[str, ...]:
        labels: list[str] = []
        if show_host:
            labels.append("HOST")
        labels.extend(("USER", "NAME", "AGENT", "CMD", "PATH"))
        return tuple(labels)

    def on_mount(self) -> None:
        for label in self.column_labels(show_host=self.show_host):
            self.add_column(label)

    def populate(self, rows: list[tuple[str, dict[str, Any]]]) -> None:
        """Replace all rows with ``rows``.

        ``rows`` is a list of ``(host_name, session_record)`` tuples.
        The session record is a wire-schema dict; the only fields
        consumed here are ``user``, ``name``, ``short_id``,
        ``agent``, ``active_cmd``, ``active_path``, ``attached``.
        Missing fields render as ``-`` rather than failing — a peer
        running an older schema is degraded, not broken.
        """
        prev_cursor = self.cursor_row
        self.clear()
        self._row_index = list(rows)
        for host_name, rec in rows:
            short = rec.get("short_id") or rec.get("name") or "-"
            attached = bool(rec.get("attached"))
            name_text = Text(short, style="bold green") if attached else Text(short)
            if attached:
                name_text.append(" ●", style="green")
            cells: list[Any] = []
            if self.show_host:
                cells.append(Text(host_name, style="bold cyan"))
            cells.append(rec.get("user") or "-")
            cells.append(name_text)
            cells.append(rec.get("agent") or "-")
            cells.append(rec.get("active_cmd") or "-")
            cells.append(rec.get("active_path") or "-")
            self.add_row(*cells)
        if rows:
            self.move_cursor(row=min(prev_cursor, len(rows) - 1))

    def row_at(self, row: int) -> tuple[str, dict[str, Any]] | None:
        if 0 <= row < len(self._row_index):
            return self._row_index[row]
        return None

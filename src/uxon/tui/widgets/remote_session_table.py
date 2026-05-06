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

This widget surfaces wire-schema rows; activation is driven by
MainScreen.on_data_table_row_selected (Enter -> remote attach
via ctx.on_remote_attach over SSH) and MainScreen.action_kill_remote
(``k`` -> remote kill via ctx.on_remote_kill over SSH).
"""

from __future__ import annotations

from typing import Any, ClassVar

from rich.text import Text

from .focus_releasing_data_table import FocusReleasingDataTable


class RemoteSessionTable(FocusReleasingDataTable):
    """DataTable rendering remote-host sessions from wire-schema records.

    Columns (when ``show_host=False``):
        ``user name agent cmd path``

    Columns (when ``show_host=True``):
        ``host user name agent cmd path``

    The ``populate`` method takes pairs of ``(host_name, record)`` so
    a multi-host snapshot can be flattened in one pass with stable
    sort order — host first, then session name.

    Boundary-aware navigation, cursor-on-focus visibility, and the
    base CSS (width / height / hover) are inherited from
    :class:`FocusReleasingDataTable`.
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
        self.show_host = show_host
        # Each row in the table maps back to a (host_name, record) tuple;
        # the on_data_table_row_selected handler reads it via row_at().
        self._row_index: list[tuple[str, dict[str, Any]]] = []

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

    @staticmethod
    def _row_key(host_name: str, rec: dict[str, Any]) -> str:
        """Stable per-row key fed to DataTable's row-keyed API.

        Encodes ``host_name/<short_id-or-name>``. Two peers may run a
        session with the same name; the host prefix disambiguates.
        Missing both fields falls back to a placeholder so the key
        stays unique enough for ``add_row(key=...)`` not to collide.
        """
        ident = rec.get("short_id") or rec.get("name") or "-"
        return f"{host_name}/{ident}"

    def _build_cells(self, host_name: str, rec: dict[str, Any]) -> list[Any]:
        """Build the cell list for one row. Shared by ``populate``
        and :meth:`update_host_rows` so the rendering of a row stays
        consistent across the full-replace and per-host paths.
        """
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
        return cells

    def populate(self, rows: list[tuple[str, dict[str, Any]]]) -> None:
        """Replace all rows with ``rows``.

        ``rows`` is a list of ``(host_name, session_record)`` tuples.
        The session record is a wire-schema dict; the only fields
        consumed here are ``user``, ``name``, ``short_id``,
        ``agent``, ``active_cmd``, ``active_path``, ``attached``.
        Missing fields render as ``-`` rather than failing — a peer
        running an older schema is degraded, not broken.

        Used for the *initial* mount and any layout-change
        re-population (e.g. when adding/removing a peer host swaps
        the show_host column structure). Steady-state per-host
        landings go through :meth:`update_host_rows`, which only
        rewrites the rows for one peer.
        """
        prev_cursor = self.cursor_row
        self.clear()
        self._row_index = list(rows)
        for host_name, rec in rows:
            cells = self._build_cells(host_name, rec)
            self.add_row(*cells, key=self._row_key(host_name, rec))
        if rows:
            self.move_cursor(row=min(prev_cursor, len(rows) - 1))

    def update_host_rows(
        self,
        host_name: str,
        rows: list[tuple[str, dict[str, Any]]],
    ) -> None:
        """Replace the rows for one peer in place.

        ``rows`` is the new row list for ``host_name`` (typically the
        per-host slice of :func:`select_remote_rows`'s output). Other
        peers' rows are untouched: O(rows for this host), not
        O(total rows across all peers). This is the steady-state
        repaint path — every per-host source landing goes here, not
        through :meth:`populate`.

        Implementation:

        * Drop ``_row_index`` entries whose ``host_name`` matches and
          collect their row keys for removal from the underlying
          DataTable.
        * Append the new rows to ``_row_index`` (cursor mapping
          stays consistent with the visible order: surviving hosts'
          entries first, this host's new entries last).
        * Translate to DataTable's row-keyed API
          (``remove_row(key)`` / ``add_row(*cells, key=...)``).
          ``self.clear()`` is **never** called here — that would
          defeat the per-host point.
        """
        keep: list[tuple[str, dict[str, Any]]] = []
        drop_keys: list[str] = []
        for entry_host, rec in self._row_index:
            # Multi-host display names carry " (own only) [badge]"
            # suffixes; the bare prefix is the canonical name. The
            # row's stored DataTable key embeds the *display* name
            # the row was originally inserted with — must use
            # ``entry_host`` (not ``host_name``) when building the
            # drop key, otherwise ``remove_row`` silently fails on
            # the mismatch and the old row leaks alongside the new.
            if entry_host == host_name or entry_host.split(" ", 1)[0] == host_name:
                drop_keys.append(self._row_key(entry_host, rec))
            else:
                keep.append((entry_host, rec))
        for key in drop_keys:
            try:
                self.remove_row(key)
            except Exception:  # pragma: no cover — defensive
                pass
        self._row_index = keep
        for entry_host, rec in rows:
            cells = self._build_cells(entry_host, rec)
            try:
                self.add_row(*cells, key=self._row_key(entry_host, rec))
            except Exception:  # pragma: no cover — defensive
                pass
            self._row_index.append((entry_host, rec))

    def row_at(self, row: int) -> tuple[str, dict[str, Any]] | None:
        if 0 <= row < len(self._row_index):
            return self._row_index[row]
        return None

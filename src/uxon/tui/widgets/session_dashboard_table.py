"""SessionDashboardTable — DataTable subclass driven by the dashboard reconciler.

Subclass of :class:`FocusReleasingDataTable` (so it inherits the edge-release
navigation contract and base CSS). Mounts no rows on construction; takes
``columns: tuple[ColumnSpec, ...]`` and registers a column per entry, keyed by
``col.id`` so :meth:`update_cell` lookups work later.

The widget is consumed by ``MainScreen`` (commit 10) but lives in isolation
in this commit, fully tested. The reconciler in
:mod:`uxon.tui.dashboard.reconcile` produces the op stream that
:meth:`apply` dispatches.

Path note
---------

The unified plan put this file under ``dashboard/`` but the project convention
(per ``AGENTS.md`` § Code layout) is that custom Textual widgets live under
``widgets/``. The pure-data dashboard package keeps ``row.py``, ``columns.py``,
etc.; only this widget — which legitimately imports textual at module load —
sits in ``widgets/``.

RowAdd inline-insert algorithm
------------------------------

Textual's ``DataTable.add_row`` always **appends** — there is no public
"insert at index" API (see textual issue #2587). For ``RowAdd(key, cells,
before_key)`` the widget therefore:

* Appends when ``before_key is None`` or when ``before_key`` is the last
  visible row (in which case appending after it is correct anyway).
* Otherwise, locates ``before_key``'s current row index, removes every row
  at index ≥ that index in current visual order, appends the new row, then
  re-appends the removed rows in their original relative order.

Worst case is O(rows-after-insert-position) per inline-insert op. On our row
counts (≤ ~200) this is fine; switch to a future Textual public-API row-move
when it lands.

Module guard
------------

We rely on ``DataTable._row_locations`` (private) for the
:meth:`pin_cursor_to` lookup. The literal ``hasattr(_DataTable,
"_row_locations")`` check the plan suggests doesn't work because
``_row_locations`` is set in ``__init__``, not as a class attribute — so we
inspect the source of ``__init__`` instead. If a future Textual refactor moves
or renames the attribute, this assertion will fire at import time with a
pointer to the follow-up issue.
"""

from __future__ import annotations

import inspect
import time
from typing import TYPE_CHECKING

from textual.widgets._data_table import DataTable as _PrivateDataTable
from textual.widgets._data_table import RowKey

from ..dashboard.reconcile import CellUpdate, RowAdd, RowRemove
from ..events import debug
from .focus_releasing_data_table import FocusReleasingDataTable

if TYPE_CHECKING:
    from ..dashboard.columns import ColumnSpec
    from ..dashboard.reconcile import Op

# Import-time guard: fail loudly if Textual ever drops the private
# ``_row_locations`` attribute we depend on. We can't ``hasattr`` the class
# (the attribute is created inside ``__init__``); inspect the source instead.
assert "_row_locations" in inspect.getsource(_PrivateDataTable.__init__), (
    "Textual API changed: _row_locations no longer initialised on DataTable. "
    "See plan 2026-05-06-session-dashboard-unified.md and "
    "https://github.com/Textualize/textual/issues/2587 for the public-API "
    "follow-up. Pin Textual version in pyproject.toml until resolved."
)


class SessionDashboardTable(FocusReleasingDataTable):
    """Reconciler-driven DataTable for the unified session dashboard.

    Construction takes the active ``columns`` tuple (already filtered by
    layout flags). Columns are registered in ``on_mount`` keyed by
    ``col.id`` so the reconciler's :class:`CellUpdate` ops can look them
    up by id.
    """

    def __init__(
        self,
        columns: tuple[ColumnSpec, ...],
        *,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._columns: tuple[ColumnSpec, ...] = columns

    def on_mount(self) -> None:
        for col in self._columns:
            self.add_column(col.label, key=col.id)

    # ── op application ──────────────────────────────────────────────

    def apply(self, ops: tuple[Op, ...]) -> None:
        """Dispatch reconciler ops against the underlying DataTable.

        No-op apply (zero ops) emits NO ``tui-table`` debug line — silence
        is the contract that proves the identity-stable diff is working.
        """
        if not ops:
            # Silence-on-no-op is part of the contract; commit 9's perf
            # test asserts on the absence of any log line here.
            return
        t0 = time.perf_counter()
        counts = {"add": 0, "remove": 0, "update": 0}
        for op in ops:
            if isinstance(op, RowAdd):
                counts["add"] += 1
                self._apply_add(op)
            elif isinstance(op, RowRemove):
                counts["remove"] += 1
                self._apply_remove(op)
            elif isinstance(op, CellUpdate):
                counts["update"] += 1
                self._apply_update(op)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        debug("tui-table", ms=elapsed_ms, ops=counts, rows=self.row_count)

    def _apply_add(self, op: RowAdd) -> None:
        # Append-only fast path: no anchor, or anchor is the last visible row.
        if op.before_key is None:
            self.add_row(*op.cells, key=op.row_key)
            return
        anchor_idx = self._row_index_of(op.before_key)
        if anchor_idx is None or anchor_idx >= self.row_count:
            # Anchor not present (perhaps already removed) — append.
            self.add_row(*op.cells, key=op.row_key)
            return
        # Inline insert: snapshot rows that should sit *after* the new
        # row in the final layout, drop them, append the new row, then
        # re-append the snapshot in original order. Cost is bounded by
        # the diff batch which on our row counts (≤ ~200) is small.
        trailing: list[tuple[str, tuple[object, ...]]] = []
        ordered = list(self.ordered_rows)
        for row in ordered[anchor_idx:]:
            key_obj = row.key
            # Rows we add always carry a ``str`` key (see ``add_row(...,
            # key=...)`` call sites); the ``isinstance`` chain narrows
            # for pyright without a runtime assert (``-O`` would strip
            # it anyway), and the ``str(key_obj)`` fallback yields a
            # string in the structurally-impossible non-RowKey path.
            if isinstance(key_obj, RowKey) and isinstance(key_obj.value, str):
                key_str = key_obj.value
            else:
                key_str = str(key_obj)
            cells = tuple(self.get_row(key_obj))
            trailing.append((key_str, cells))
        for key_str, _ in trailing:
            self.remove_row(key_str)
        self.add_row(*op.cells, key=op.row_key)
        for key_str, cells in trailing:
            self.add_row(*cells, key=key_str)

    def _apply_remove(self, op: RowRemove) -> None:
        try:
            self.remove_row(op.row_key)
        except Exception:
            # Row-key collisions / out-of-band removals must not crash
            # the widget — log and continue. The reconciler's row-key
            # collision risk register entry mandates this defence.
            debug("tui-table", op="remove_miss", key=op.row_key)

    def _apply_update(self, op: CellUpdate) -> None:
        try:
            self.update_cell(op.row_key, op.col_id, op.value)
        except Exception:
            debug("tui-table", op="update_miss", key=op.row_key, col=op.col_id)

    # ── cursor management ──────────────────────────────────────────

    def pin_cursor_to(self, row_key: str | None) -> None:
        """Move the cursor to ``row_key`` if present.

        ``None`` leaves the cursor unchanged. When the key is missing
        (e.g. the row was just removed), the cursor falls back to the
        nearest surviving sibling — concretely
        ``min(prev_row, row_count - 1)`` — so the user never lands on a
        non-existent row.
        """
        if row_key is None:
            return
        idx = self._row_index_of(row_key)
        if idx is None:
            if self.row_count <= 0:
                return
            fallback = min(max(0, self.cursor_row), self.row_count - 1)
            self.move_cursor(row=fallback)
            return
        self.move_cursor(row=idx)

    # ── lookups ────────────────────────────────────────────────────

    def _row_index_of(self, row_key: str) -> int | None:
        """Return the visual row index of ``row_key`` or ``None``."""
        loc = self._row_locations.get(RowKey(row_key))
        return loc if isinstance(loc, int) else None

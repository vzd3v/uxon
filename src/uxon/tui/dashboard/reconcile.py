"""Pure dashboard reconciler: diff two ``tuple[SessionRow, ...]`` models.

The widget (commit 8) consumes the resulting op stream and applies it
to a Textual ``DataTable`` without re-rendering rows that have not
changed. Keeping diff-emission pure means the per-tick repaint cost
is bounded by *changed cells* rather than total rows.

Algorithm
---------

The single, deterministic algorithm — pinned here so tests can verify
the exact op stream:

1. Compute key→index maps for ``old`` and ``new``. Row-key is
   ``f"{host or 'local'}/{user}/{name}"`` (unique across the unified
   model: local rows have ``host=None`` so they share the ``local/``
   prefix; remote rows are namespaced by host).
2. **Removes first.** For every key in ``old`` that is missing from
   ``new`` emit ``RowRemove(key)`` in *old's* index order. Doing
   removes first lets the widget shrink its row map before any add
   shifts visual positions.
3. **Walk new in order.** For each ``(i, row_new)``:

   * If the key is missing from ``old`` → emit ``RowAdd(key, cells,
     before_key)``. ``cells`` is built once via the active columns'
     ``format`` callables. ``before_key`` is the next-new-row's key
     (``new[i+1]``) if any, else ``None`` for "append".
   * If the key is in ``old`` AND its position among the surviving
     keys (i.e. the relative order of keys present in both ``old``
     and ``new``) is unchanged → emit per-column ``CellUpdate`` only
     for cells whose formatted value differs.
   * If the key is in ``old`` but its surviving-relative position
     changed → treat as a move: emit ``RowRemove(key)`` then
     ``RowAdd(key, cells, before_key)``. The widget's data-table
     does not support native row moves; remove + re-add is correct
     and on our row counts (≤ ~200) the cost is negligible.

Cell equality
~~~~~~~~~~~~~

Cell values are compared with ``==``. Some columns return
:class:`rich.text.Text`; Rich's ``__eq__`` compares plain text and
span styles, but does *not* compare the top-level ``style=``
argument. In practice this is fine for the active formatters: any
top-level-style transition (e.g. CPU yellow→red) is co-incident with
a plain-text change (the numeric value differs). A formatter that
ever toggles top-level style without a plain-text change would
silently miss an update — flag and switch to comparing
``(cell.plain, cell.style)`` if such a formatter is added.

The diff function is pure: same inputs always produce equal op tuples.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .columns import ColumnSpec
    from .row import SessionRow


@dataclass(frozen=True, slots=True)
class CellUpdate:
    """Update one cell of an existing row."""

    row_key: str
    col_id: str
    value: Any


@dataclass(frozen=True, slots=True)
class RowAdd:
    """Insert a new row before ``before_key`` (or append when ``None``)."""

    row_key: str
    cells: tuple[Any, ...]
    before_key: str | None


@dataclass(frozen=True, slots=True)
class RowRemove:
    """Remove the row identified by ``row_key``."""

    row_key: str


# Type alias for the op union. Pyright handles the union with no
# runtime cost; downstream code can ``isinstance`` on the concrete
# dataclasses.
Op = CellUpdate | RowAdd | RowRemove


def _row_key(row: SessionRow) -> str:
    """Stable identity key for a row across diffs.

    ``host`` defaults to ``"local"`` when ``None`` so local rows share
    a deterministic prefix and never collide with peer-named rows.
    """
    return f"{row.host or 'local'}/{row.user}/{row.name}"


def _format_cells(row: SessionRow, columns: tuple[ColumnSpec, ...]) -> tuple[Any, ...]:
    """Render every active column for ``row``."""
    return tuple(c.format(row) for c in columns)


def diff(
    old: tuple[SessionRow, ...],
    new: tuple[SessionRow, ...],
    columns: tuple[ColumnSpec, ...],
) -> tuple[Op, ...]:
    """Compute a deterministic op stream from ``old`` to ``new``.

    See the module docstring for the full algorithm. ``columns`` is
    the active-column tuple (already filtered by layout flags); the
    diff only emits ops for these columns.
    """
    old_index: dict[str, int] = {}
    old_rows: dict[str, SessionRow] = {}
    for i, r in enumerate(old):
        k = _row_key(r)
        old_index[k] = i
        old_rows[k] = r

    new_index: dict[str, int] = {}
    for i, r in enumerate(new):
        new_index[_row_key(r)] = i

    ops: list[Op] = []

    # 1. Removes first (in old's index order).
    removed_keys = [
        k for k, _ in sorted(old_index.items(), key=lambda kv: kv[1]) if k not in new_index
    ]
    for k in removed_keys:
        ops.append(RowRemove(k))

    # 2. Compute the relative order of surviving keys in both old and
    # new. A key is "in place" iff its index among surviving keys
    # matches between old and new.
    survivors_in_old = [
        k for k, _ in sorted(old_index.items(), key=lambda kv: kv[1]) if k in new_index
    ]
    survivors_in_new = [_row_key(r) for r in new if _row_key(r) in old_index]
    old_pos_among_survivors = {k: i for i, k in enumerate(survivors_in_old)}
    new_pos_among_survivors = {k: i for i, k in enumerate(survivors_in_new)}

    # 3. Walk new in order.
    for i, row_new in enumerate(new):
        k = _row_key(row_new)
        before_key = _row_key(new[i + 1]) if i + 1 < len(new) else None

        if k not in old_index:
            # Pure add.
            ops.append(RowAdd(k, _format_cells(row_new, columns), before_key))
            continue

        moved = old_pos_among_survivors[k] != new_pos_among_survivors[k]
        if moved:
            # Re-add at new position.
            ops.append(RowRemove(k))
            ops.append(RowAdd(k, _format_cells(row_new, columns), before_key))
            continue

        # In-place: emit CellUpdate per changed column.
        row_old = old_rows[k]
        for col in columns:
            old_val = col.format(row_old)
            new_val = col.format(row_new)
            if old_val != new_val:
                ops.append(CellUpdate(k, col.id, new_val))

    return tuple(ops)

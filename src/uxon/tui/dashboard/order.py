"""Stable-order placement for the session dashboard (pure, textual-free).

:func:`place` is the frozen-order replacement for the per-tick re-sort the
model selector does today. Its contract (spec D2 / AC3 / AC4 / AC6):

* **existing keys keep their slot** — a row already present in
  ``persisted_order`` stays at its established rank, so a telemetry tick that
  only changes recency never moves a row (AC3);
* **new keys land at their position in ``current_rows``** — a row whose key is
  not in ``persisted_order`` is inserted immediately after the last
  already-placed row that precedes it in ``current_rows`` (front if none
  precede). ``current_rows`` is the model's already-grouped order — locals →
  cfg-order remote blocks, recency within each block
  (:func:`uxon.tui.dashboard.model._build`). Placing by *model order* rather
  than by global recency keeps the new arrival **inside its own host block**
  (AC6) at its recency-at-arrival slot within that block (AC4), instead of
  flattening the host grouping;
* **dead keys are dropped** — a key in ``persisted_order`` whose row is no
  longer in ``current_rows`` is removed.

Cold start (empty ``persisted_order``) therefore yields exactly
``current_rows`` — every host block is preserved verbatim.

The function is **pure**: it takes the persisted order (a tuple of row-key
strings) and the current row set, and returns the newly-ordered rows. It does
not mutate ``persisted_order`` — the caller persists the new order
(``tuple(r.key for r in result)``) back onto ``MainScreenUiState`` so it
survives a ctx rebuild and a background refresh never re-sorts.

No Textual import — this is the pure placement layer.

Why model-order, not global recency
------------------------------------

An earlier draft placed new arrivals by *global* ``-last_attached_epoch``. With
more than one host that flattened the host blocks: a fresh remote row could land
above the local block purely because it was more recently attached. Placing each
new arrival at its neighbour in ``current_rows`` (which is already host-grouped)
keeps it block-local — the recommended approach in the plan, satisfying both the
recency-at-arrival intent (AC4) and host-block preservation (AC6). The plan's
open-question fallback (append-to-block-tail) is therefore unnecessary: arrivals
are already block-local and land at their model-order slot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .row import SessionRow


def place(
    persisted_order: tuple[str, ...],
    current_rows: tuple[SessionRow, ...],
) -> tuple[SessionRow, ...]:
    """Map (persisted key order, current rows) → frozen-order row tuple.

    Existing keys keep their slot; new keys land at their position in
    ``current_rows`` (the model's host-grouped order); dead keys are
    dropped. Pure — see the module docstring for the full contract.
    """
    by_key = {row.key: row for row in current_rows}

    # 1. Existing rows, in their persisted order — skip dead keys (rows
    #    that have since departed) and any key that no longer resolves.
    ordered: list[SessionRow] = []
    placed: set[str] = set()
    for key in persisted_order:
        row = by_key.get(key)
        if row is None or key in placed:
            continue
        ordered.append(row)
        placed.add(key)

    new_keys = {row.key for row in current_rows if row.key not in placed}
    if not new_keys:
        return tuple(ordered)

    # 2. New arrivals — keys present now but not in the persisted order.
    #    Walk ``current_rows`` (model order = host-grouped, recency within
    #    block) and insert each new key immediately after the last
    #    already-placed row that precedes it in model order. This keeps the
    #    arrival inside its own host block (AC6) at its recency-at-arrival
    #    slot (AC4) without moving any established row (AC3). Cold start
    #    (everything new) reproduces ``current_rows`` exactly.
    #
    #    ``insert_pos`` tracks where the next new arrival goes: it advances
    #    past each already-placed row as we encounter it walking model
    #    order, so a run of consecutive new keys stays in model order.
    insert_pos = 0
    for row in current_rows:
        if row.key in new_keys:
            ordered.insert(insert_pos, row)
            insert_pos += 1
        elif row.key in placed:
            # An already-placed anchor: the next new arrival belongs after
            # it. Re-resolve its current index (earlier inserts shift it).
            insert_pos = ordered.index(row) + 1

    return tuple(ordered)

"""Pure stable-order placement for the session dashboard.

Existing rows keep their persisted positions, new rows enter at their position
in the host-grouped model order, and departed rows disappear. An empty
persisted order reproduces the current model order exactly. The function has no
Textual dependency and does not mutate either input.
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
    #    arrival inside its own host block at its recency-at-arrival
    #    slot without moving any established row. Cold start
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

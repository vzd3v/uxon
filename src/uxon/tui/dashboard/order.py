"""Stable-order placement for the session dashboard (pure, textual-free).

:func:`place` is the frozen-order replacement for the per-tick re-sort the
model selector does today. Its contract (spec D2 / AC3 / AC4):

* **existing keys keep their slot** — a row already present in
  ``persisted_order`` stays at its established rank, so a telemetry tick that
  only changes recency never moves a row (AC3);
* **new keys are inserted by recency-at-arrival** — a row whose key is not in
  ``persisted_order`` is placed among the *current* rows by the same recency
  notion the model uses for within-block ordering
  (:func:`uxon.tui.dashboard.model._within_block_key`: ``-last_attached_epoch``
  then name), at the moment it appears (AC4);
* **dead keys are dropped** — a key in ``persisted_order`` whose row is no
  longer in ``current_rows`` is removed.

The function is **pure**: it takes the persisted order (a tuple of row-key
strings) and the current row set, and returns the newly-ordered rows. It does
not mutate ``persisted_order`` — the caller persists the new order
(``tuple(r.key for r in result)``) back onto ``MainScreenUiState`` so it
survives a ctx rebuild and a background refresh never re-sorts.

No Textual import — this is the pure placement layer.

Open question (decided in Phase 2 from the live look)
-----------------------------------------------------

New-arrival placement is "insert by recency-at-arrival among current rows". If
that reads oddly against frozen neighbours that have since aged (a fresh row
landing *above* an older neighbour that has not moved), the fallback is
**append-to-block-tail**: place the new key at the end of its host/user block
rather than by recency. The hook for that lives here — ``_recency_rank`` is the
only ordering input, and a block-tail variant would slot the new key after the
last persisted key sharing its block prefix. Not implemented yet; the recency
placement is the default until the live look in Phase 2 says otherwise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .row import SessionRow


def _recency_rank(row: SessionRow) -> tuple[float, str]:
    """Recency ordering key — newer (larger epoch) first, then name.

    Mirrors :func:`uxon.tui.dashboard.model._within_block_key` so a new
    arrival is placed by the same recency notion the model uses for
    within-block ordering. Kept local (no import from ``model``) to keep
    this layer dependency-light; the formula is identical by contract.
    """
    last = row.last_attached_epoch if row.last_attached_epoch is not None else float("-inf")
    return (-last, (row.short or row.name or "").lower())


def place(
    persisted_order: tuple[str, ...],
    current_rows: tuple[SessionRow, ...],
) -> tuple[SessionRow, ...]:
    """Map (persisted key order, current rows) → frozen-order row tuple.

    Existing keys keep their slot; new keys are inserted by
    recency-at-arrival among the current rows; dead keys are dropped.
    Pure — see the module docstring for the full contract.
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

    # 2. New arrivals — keys present now but not in the persisted order.
    #    Insert each by its recency rank relative to the rows already
    #    placed, so a fresh row lands among current rows by recency
    #    (AC4) without disturbing the established slots (AC3).
    new_rows = [row for row in current_rows if row.key not in placed]
    if not new_rows:
        return tuple(ordered)

    # Place newest-first so equal-recency arrivals keep a deterministic
    # relative order, then bisect each into ``ordered`` by recency rank.
    for row in sorted(new_rows, key=_recency_rank):
        rank = _recency_rank(row)
        insert_at = len(ordered)
        for i, existing in enumerate(ordered):
            if rank < _recency_rank(existing):
                insert_at = i
                break
        ordered.insert(insert_at, row)

    return tuple(ordered)

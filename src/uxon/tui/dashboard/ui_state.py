"""Dashboard UI state + pure reducers.

The dashboard's *transient* state — what column the rows are sorted by,
which direction, and the operator's substring filter — lives here as an
immutable :class:`DashboardUiState`. The selector (commit 6) consumes
it from day one so the filter branch never goes through a "sometimes
present" code path; reducers below let the screen layer mutate the
state without learning the dataclass shape.

Every reducer is pure and deterministic. ``ui`` is returned by
``is`` identity on no-ops so identity-keyed memoisation downstream
(the reconciler diff in commit 7) stays correct without an explicit
equality check.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from .columns import ColumnSpec

# Fixed list of "interesting" sort columns the ``s`` keybinding cycles
# through. Other ids in the registry (``host``, ``user``, ``new``,
# ``cmd``, …) remain reachable via explicit config — only the rotation
# is curated. Order is the operator's expected mental model: "see the
# busy ones first" (cpu/ram), then recency (last), then alphabetical
# (name) as the tie-breaker.
_SORT_CYCLE: tuple[str, ...] = ("cpu", "ram", "last", "name")


@dataclass(frozen=True, slots=True)
class DashboardUiState:
    """Transient per-screen UI state for the session dashboard.

    Frozen + slotted so identity-keyed downstream caches stay valid
    when reducers return ``self`` on a no-op (and reliably invalidate
    when something actually changes). ``filter_text`` ships from day
    one even though the keybinding lands later — the selector
    consumes it unconditionally so the filter code path never goes
    "sometimes wired".
    """

    sort_by: str = "cpu"
    sort_dir: Literal["asc", "desc"] = "desc"
    filter_text: str = ""


def cycle_sort(
    ui: DashboardUiState,
    *,
    columns: tuple[ColumnSpec, ...],
) -> DashboardUiState:
    """Advance ``ui.sort_by`` to the next entry in the curated cycle.

    Filtered to columns currently visible (``columns`` reflects the
    runtime layout), so the cycle can never land on a hidden id even
    if ``cpu`` happens to be off. If no curated id is in the active
    set, returns ``ui`` unchanged. When the current ``sort_by`` is
    not in the cycle (e.g. operator-pinned from config), the next
    cycle call jumps to the first available cycle entry.

    Returns ``ui`` by identity when the result would be the same
    value (degenerate single-candidate cycle) so downstream identity
    checks stay sound.
    """
    visible_ids = {c.id for c in columns}
    candidates = [cid for cid in _SORT_CYCLE if cid in visible_ids]
    if not candidates:
        return ui
    try:
        idx = candidates.index(ui.sort_by)
    except ValueError:
        # ``sort_by`` isn't in the curated cycle — jump to the first
        # available cycle entry. ``replace`` always builds a new
        # instance; identity changes, which is correct here because
        # the value changes too.
        return replace(ui, sort_by=candidates[0])
    nxt = candidates[(idx + 1) % len(candidates)]
    if nxt == ui.sort_by:
        # Single-candidate cycle: the only entry is already active.
        return ui
    return replace(ui, sort_by=nxt)


def toggle_sort_dir(ui: DashboardUiState) -> DashboardUiState:
    """Flip ``sort_dir`` between ``"asc"`` and ``"desc"``.

    Always changes the value, so always returns a new instance. The
    ``is``-identity contract still applies in spirit: callers that
    depend on identity reuse on a no-op simply have no no-op here.
    """
    new: Literal["asc", "desc"] = "asc" if ui.sort_dir == "desc" else "desc"
    return replace(ui, sort_dir=new)


def set_filter(ui: DashboardUiState, text: str) -> DashboardUiState:
    """Set ``filter_text`` to ``text``.

    Returns ``ui`` by identity when ``text`` already matches — this
    is the hot-path reducer (one call per keystroke) and the no-op
    identity reuse keeps the reconciler from rebuilding rows when
    the filter hasn't actually moved.
    """
    if text == ui.filter_text:
        return ui
    return replace(ui, filter_text=text)

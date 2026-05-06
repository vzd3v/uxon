"""Pure dashboard model selector.

:func:`select_dashboard_model` is the single entry point that maps the
async-side :class:`uxon.tui.tui_state.TuiState`, the immutable
:class:`uxon.tui.config.TuiConfig`, and the transient
:class:`uxon.tui.dashboard.ui_state.DashboardUiState` to a flat
``tuple[SessionRow, ...]`` ready for the reconciler / widget.

The selector is deliberately pure (no Textual imports, no subprocess,
no filesystem) so it can be unit-tested without an event loop and
benchmarked in isolation.

Identity-stability mechanism
----------------------------

``state.main`` is replaced on every rebuild dispatch even when its
content is unchanged (see :mod:`uxon.tui.tui_state` module docstring),
so ``id(state.main)`` is not a valid cache key. Instead we keep the
previous output tuple at module scope and compare the freshly built
candidate to it via structural equality (``SessionRow`` is
``frozen=True, slots=True`` so equality is cheap). When equal we
return the cached reference — callers therefore observe ``is``
identity across no-op rebuilds without us having to track input
identity.

Module-level state is deliberate. Tests reset it via
``model._LAST_OUTPUT = ()`` in setUp / tearDown.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .columns import REGISTRY
from .row import SessionRow, from_tui_session, from_wire_record

if TYPE_CHECKING:
    from ..config import TuiConfig
    from ..tui_state import TuiState
    from .ui_state import DashboardUiState


# Previous-result cache. See module docstring for the identity-stability
# contract this implements. Reset by tests as needed.
_LAST_OUTPUT: tuple[SessionRow, ...] = ()


def _matches_filter(row: SessionRow, needle: str) -> bool:
    """Case-insensitive substring match across name / cmd / path / user."""
    return (
        needle in row.name.lower()
        or needle in row.cmd.lower()
        or needle in row.path.lower()
        or needle in row.user.lower()
    )


def _build(
    state: TuiState,
    cfg: TuiConfig,
    ui: DashboardUiState,
) -> tuple[SessionRow, ...]:
    """Build the candidate row tuple from the current state + config + ui."""
    rows: list[SessionRow] = []

    # 1. Local rows. ``state.main`` is ``None`` on cold start (no
    # rebuild has landed yet); treat that as zero local rows.
    if state.main is not None:
        for s in state.main.sessions:
            rows.append(from_tui_session(s))
        for s in state.main.other_sessions:
            rows.append(from_tui_session(s))

    # 2. Per-host remote rows in cfg-declared order. Local-before-remote
    # ordering is a stable-sort tiebreak operators expect.
    for host in cfg.remote_hosts:
        slot = state.remote.get(host.name)
        if slot is None:
            continue
        snap = slot.value
        if snap is None:
            continue
        for rec in snap.sessions:
            rows.append(from_wire_record(host.name, rec))

    # 3. Filter. Empty / all-whitespace string short-circuits the
    # predicate so the no-filter identity path is exact.
    needle = ui.filter_text.strip().lower()
    if needle:
        rows = [r for r in rows if _matches_filter(r, needle)]

    # 4. Global sort by the active column. Look the column up in the
    # registry rather than a layout-computed active set — the sort key
    # is always registry-keyed. Defensive fallback to ``cpu`` so a
    # stale ``sort_by`` from config never crashes the selector.
    column = next((c for c in REGISTRY if c.id == ui.sort_by), None)
    if column is None:
        column = next(c for c in REGISTRY if c.id == "cpu")
    rows.sort(key=column.sort_key, reverse=(ui.sort_dir == "desc"))

    return tuple(rows)


def select_dashboard_model(
    state: TuiState,
    cfg: TuiConfig,
    ui: DashboardUiState,
) -> tuple[SessionRow, ...]:
    """Build the unified dashboard row tuple from current state.

    Returns ``tuple[SessionRow, ...]`` containing every local
    (``host=None``) and remote (``host=<peer>``) row, filtered by
    ``ui.filter_text`` and globally sorted by ``ui.sort_by`` /
    ``ui.sort_dir``.

    ``cross_user`` is *not* part of the return type — the caller
    computes it from the returned rows when assembling
    :class:`uxon.tui.dashboard.layout.LayoutFlags`.

    Identity stability: a no-op rebuild returns the previous tuple
    by ``is`` (see module docstring).
    """
    new = _build(state, cfg, ui)
    global _LAST_OUTPUT
    if new == _LAST_OUTPUT:
        return _LAST_OUTPUT
    _LAST_OUTPUT = new
    return new

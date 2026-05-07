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

from .row import SessionRow, from_tui_session, from_wire_record

if TYPE_CHECKING:
    from ..config import TuiConfig
    from ..tui_state import TuiState
    from .ui_state import DashboardUiState


# Previous-result cache. See module docstring for the identity-stability
# contract this implements. Reset by tests as needed.
_LAST_OUTPUT: tuple[SessionRow, ...] = ()


_DEFAULT_SEARCH_FIELDS: tuple[str, ...] = ("name", "user")


def _matches_filter(row: SessionRow, needle: str, fields: tuple[str, ...]) -> bool:
    for f in fields:
        if f == "name" and needle in (row.short or row.name).lower():
            return True
        if f == "user" and needle in row.user.lower():
            return True
        if f == "host" and needle in (row.host or "local").lower():
            return True
        if f == "path" and needle in row.path.lower():
            return True
        if f == "cmd" and needle in row.cmd.lower():
            return True
    return False


def _within_block_key(row: SessionRow) -> tuple[float, str]:
    last = row.last_attached_epoch if row.last_attached_epoch is not None else float("-inf")
    return (-last, (row.short or row.name or "").lower())


def _build(
    state: TuiState,
    cfg: TuiConfig,
    ui: DashboardUiState,
) -> tuple[SessionRow, ...]:
    rows: list[SessionRow] = []
    if state.main is not None:
        for s in state.main.sessions:
            rows.append(from_tui_session(s))
        for s in state.main.other_sessions:
            rows.append(from_tui_session(s))
    for host in cfg.remote_hosts:
        slot = state.remote.get(host.name)
        if slot is None:
            continue
        snap = slot.value
        if snap is None:
            continue
        for rec in snap.sessions:
            rows.append(from_wire_record(host.name, rec))

    needle = ui.filter_text.strip().lower()
    if needle:
        fields = getattr(cfg, "tui_search_fields", _DEFAULT_SEARCH_FIELDS) or _DEFAULT_SEARCH_FIELDS
        rows = [r for r in rows if _matches_filter(r, needle, fields)]

    # Two stable sorts: within-block recency first, then by host
    # priority. Python's stable sort preserves the within-block
    # ordering during the host_priority pass.
    rows.sort(key=_within_block_key)
    host_priority: dict[str | None, int] = {None: -1}
    for idx, host in enumerate(cfg.remote_hosts):
        host_priority[host.name] = idx
    tail = len(cfg.remote_hosts)
    rows.sort(key=lambda r: host_priority.get(r.host, tail))
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

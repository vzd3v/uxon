"""Mutable :class:`TuiState` — the async-side of the TUI's state.

:class:`TuiState` is the async-source-owned container: it holds the
rebuild output (``main``), a counter (``refresh_tick``), one
:class:`SlotState` per async source (link health, cwd-write probe,
agent availability, detected agents), plus a per-host slot map for
remote SSH polls.

Field-level mutability only — the container is replaced *in place*
on each event-loop dispatch (``state.link_health = apply(...)``); the
slot values themselves are frozen :class:`SlotState` instances so a
selector that captured a previous slot identity can ``is``-compare to
detect change. Promoting :class:`TuiState` to ``frozen=True`` would
force a whole-state replacement on every slot landing (allocator
pressure on the hot path) and would defeat the per-host write
granularity that drives the per-host repaint optimisation.

``main`` uses ``None`` as the "never loaded" sentinel rather than a
module-level ``_NEVER_LOADED = object()`` symbol — Pyright reports
the latter as ``MainData | object``, which is incompatible with
type-narrowing helpers; ``MainData | None`` admits ``is None`` checks
at call sites.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .slot_state import SlotState

if TYPE_CHECKING:
    from .context import LinkHealthStatus
    from .main_data import MainData


def _empty_dict_slot() -> SlotState[dict[str, Any]]:
    """Default factory for dict-shaped slots.

    ``SlotState()`` produces a ``SlotState[T@SlotState]`` (unbound
    type variable), which Pyright reports as incompatible with the
    parameterised field annotations on :class:`TuiState`. A typed
    factory closes that gap without forcing a runtime narrowing.
    """
    return SlotState[dict[str, Any]]()


def _empty_link_health_slot() -> SlotState[LinkHealthStatus]:
    return SlotState["LinkHealthStatus"]()


def _empty_cwd_writable_slot() -> SlotState[bool | None]:
    return SlotState[bool | None]()


@dataclass
class TuiState:
    """Mutable container of frozen slots and one :class:`MainData` snapshot.

    Construction takes no arguments — every slot defaults to its zero
    state (``SlotState()``: ``value=None``, no ``last_attempt_at``,
    empty ring). ``main`` defaults to ``None`` (never loaded).
    ``remote`` is an empty dict — entries are inserted on the first
    landing for each peer.

    Type annotations on the slot fields use ``Any`` for the slot's
    value parameter rather than the concrete types
    (``AgentAvailability``, ``BinaryStatus``, …) to keep this module
    importable without pulling :mod:`uxon.agents` /
    :mod:`uxon.probes` into the TUI's import graph at module-load
    time. The dispatcher reaches the concrete types via the slot's
    ``apply`` reducer and the source's fetcher closure.
    """

    main: MainData | None = None
    refresh_tick: int = 0

    # ── Async slots (one per source) ─────────────────────────────────
    # ``agent_availability`` is a mapped type whose value is a dict —
    # one slot per probe tick replaces the whole dict via ``apply``.
    # ``link_health`` and ``cwd_writable`` are scalar slots.
    agent_availability: SlotState[dict[str, Any]] = field(default_factory=_empty_dict_slot)
    link_health: SlotState[LinkHealthStatus] = field(default_factory=_empty_link_health_slot)
    cwd_writable: SlotState[bool | None] = field(default_factory=_empty_cwd_writable_slot)

    # Per-host remote-SSH poll. One :class:`SlotState` per peer name;
    # entries are inserted lazily on the first landing for that host.
    # ``RemoteSnapshot`` lives in :mod:`uxon.remote_collector`; the
    # dict-of-slots shape is what makes per-host repaints O(rows in
    # changed host) instead of O(total sessions across all peers).
    remote: dict[str, SlotState[Any]] = field(default_factory=dict)

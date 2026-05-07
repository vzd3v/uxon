"""Pure data types and transition function for the per-source slot store.

Stage 8 of the multi-host design spec materialises the *slot store*: a
small per-source container that records the latest snapshot, when it
landed, whether it came from cache, and a small ring of recent fetch
costs. The slot store is consumed by selectors (stage 9) which derive
view-shaped data without coupling render code to fetch internals.

This module ships the *type fixtures* — :class:`SlotState`,
:class:`SlotResult`, :class:`Source`, :class:`CadencePolicy` — plus the
single pure transition :func:`apply`. No Textual, no I/O. Wiring into
the existing async streams (``_RefreshSourceLanded`` →
``_source_handles``) and folding the legacy ``TuiContext`` carry-list
into a TuiState/MainData split happens in follow-up commits.

The PEP 695 ``class Foo[T]:`` syntax is 3.12+; ``pyproject`` pins
``requires-python = ">=3.11"`` so we use the classic ``Generic[T]``
form instead. ``from __future__ import annotations`` keeps the rest of
the module's type hints parsed lazily.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Generic, TypeVar

T = TypeVar("T")

# Default cap on the elapsed-ms ring. Sized to roughly 8 minutes at the
# default 30 s cadence — enough history to spot a trend without holding
# unbounded memory in a process that runs for days.
_DEFAULT_RING_SIZE = 16


@dataclass(frozen=True)
class SlotState(Generic[T]):
    """Snapshot of one source's observed state.

    Frozen by design — :func:`apply` always returns a new instance so a
    selector that captured a previous value can compare with ``==`` or
    ``is`` to detect a change. The ``elapsed_ms_recent`` ring is
    bounded; new entries are appended, oldest evicted, length capped at
    the ring-size argument passed to :func:`apply`.

    Attributes:
        value: The most recently *successful* snapshot, or ``None``
            until the first success has been observed. A failure does
            **not** clear ``value`` — the previous good snapshot is
            preserved so the UI can keep rendering known-good data
            under transient errors.
        last_success_at: Wall-clock seconds at which the last
            successful fetch landed. ``None`` until the first success.
        last_attempt_at: Wall-clock seconds at which the most recent
            fetch attempt landed (success *or* failure). Drives the
            "stale for N seconds" badge logic in the selectors.
        last_error: Short error string from the most recent failure;
            cleared on success. ``None`` means "no error from the most
            recent attempt".
        from_cache: Whether the most recent value came from an on-disk
            cache rather than a live fetch. The ``[c]`` cache marker in
            the per-host badges reads this flag.
        in_flight: Whether a fetch worker is currently running. Set by
            the scheduler when it kicks a worker, cleared when the
            result lands. The slot-state itself does not enforce
            single-flight — that's the scheduler's job.
        consecutive_failures: Reset to zero on every success;
            incremented on every failure. Drives breaker decisions.
        elapsed_ms_recent: Bounded ring of recent fetch durations in
            milliseconds. Useful for cost diagnosis without spinning up
            a full metrics pipeline.
        p50_elapsed_ms: Pre-computed median over ``elapsed_ms_recent``,
            re-derived inside :func:`apply` on every call. Surfaces
            on the per-host health badge tooltip (commit 4) without
            forcing every render path to re-sort the ring. ``None``
            until the first attempt lands. Even-length rings use the
            *average* of the two middle values so a noisy [10, 30]
            history reports 20 — not the upper-median 30 — which
            matches how operators read latency budgets.
    """

    value: T | None = None
    last_success_at: float | None = None
    last_attempt_at: float | None = None
    last_error: str | None = None
    from_cache: bool = False
    in_flight: bool = False
    consecutive_failures: int = 0
    elapsed_ms_recent: tuple[int, ...] = field(default_factory=tuple)
    p50_elapsed_ms: int | None = None


@dataclass(frozen=True)
class SlotResult(Generic[T]):
    """Outcome of one fetch attempt, fed into :func:`apply`.

    Mirrors the shape of :class:`uxon.tui.refresh.SourceResult` plus
    the success / cache metadata the slot store needs.

    Attributes:
        value: Fetched payload on success; ``None`` on failure.
        error: Short error string on failure; ``None`` on success.
            ``value is None`` and ``error is None`` together mean
            "successful empty result", e.g. an empty list of remote
            sessions on a healthy host.
        elapsed_ms: Wall time the fetch took, in milliseconds.
            Appended to the slot's ring on every result.
        attempted_at: Wall-clock seconds at which this attempt landed.
            Drives ``last_attempt_at`` and (on success)
            ``last_success_at``.
        from_cache: Whether the value was loaded from an on-disk cache
            rather than a live fetch. Round-trips through :func:`apply`
            into :attr:`SlotState.from_cache`.
    """

    value: T | None
    error: str | None
    elapsed_ms: int
    attempted_at: float
    from_cache: bool = False


@dataclass(frozen=True)
class CadencePolicy:
    """How often a source should be kicked, plus optional breaker hookup.

    The TUI's existing ``set_interval`` machinery still drives the
    timer; ``CadencePolicy`` is the data contract a future
    breaker-aware scheduler will consume.

    Attributes:
        interval: Nominal seconds between ticks. The scheduler may
            apply jitter (see ``jitter_pct``) so two sources don't
            line up perfectly across instance boundaries.
        jitter_pct: ±% jitter applied to the nominal interval. ``0.0``
            means "no jitter" — useful for tests that want
            deterministic tick alignment.
        initial_offset: Seconds to delay the *first* tick. Spaces out
            a fan-out of N sources so they don't all hit at t=0.
        breaker: Optional :class:`uxon.host_breaker.BreakerSpec` (or
            similar). Typed as ``object | None`` here to keep this
            module decoupled from the breaker implementation; the
            scheduler is responsible for the duck-typed dispatch.
        coalesce_missed: When ``True`` (the default), a tick that
            would have fired while a worker was still in-flight is
            silently dropped — the next regular tick picks up the
            slack. ``False`` would queue missed ticks; we default to
            coalescing because the slot store always reflects the
            latest snapshot anyway.
    """

    interval: float
    jitter_pct: float = 0.0
    initial_offset: float = 0.0
    breaker: object | None = None
    coalesce_missed: bool = True


@dataclass(frozen=True)
class Source(Generic[T]):
    """Declarative description of one slot-backed source.

    The richer cousin of :class:`uxon.tui.refresh.SourceSpec` — adds an
    ``apply`` reducer (so per-source state transitions can be more
    interesting than "overwrite") and a structured cadence policy.
    Used by the stage-9 scheduler; not yet referenced by the existing
    fan-out path.

    Attributes:
        id: Stable identity string (used as worker group, slot key,
            etc.). Equivalent to :attr:`SourceSpec.name`.
        fetch: Synchronous callable that takes the live ``TuiConfig``
            (typed as ``object`` here to avoid pulling the config
            symbol into this module) and returns a :class:`SlotResult`.
        apply: Pure reducer ``(prev_state, result) -> next_state``.
            Defaults to the module-level :func:`apply`; sources with
            non-trivial merge semantics (e.g. partial-update streams)
            can supply a richer reducer.
        cadence: How often the scheduler should kick this source.
        kick_on_mount: Whether the source should be fetched once at
            mount time, before the first cadence tick. Defaults to
            True (matches the legacy registry behaviour).
    """

    id: str
    fetch: Callable[[object], SlotResult[T]]
    apply: Callable[[SlotState[T], SlotResult[T]], SlotState[T]]
    cadence: CadencePolicy
    kick_on_mount: bool = True


def _median(samples: tuple[int, ...]) -> int | None:
    """Median of ``samples`` as an int. ``None`` for the empty ring.

    Uses an even-length *average* (``(s[mid-1] + s[mid]) // 2``) so a
    bimodal latency history doesn't bias toward the upper sample.
    Pure; no module-level imports required (avoids ``statistics`` for
    one helper that runs on a 16-element tuple).
    """
    if not samples:
        return None
    s = sorted(samples)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) // 2


def apply(
    prev: SlotState[T],
    r: SlotResult[T],
    *,
    ring_size: int = _DEFAULT_RING_SIZE,
) -> SlotState[T]:
    """Pure transition: fold a :class:`SlotResult` into a :class:`SlotState`.

    Success semantics (``r.error is None``):
        * ``value`` is replaced with ``r.value`` (which may itself be
          ``None`` for a successful-but-empty fetch).
        * ``last_success_at`` and ``last_attempt_at`` are advanced to
          ``r.attempted_at``.
        * ``last_error`` is cleared.
        * ``consecutive_failures`` is reset to ``0``.
        * ``from_cache`` is taken from the result.

    Failure semantics (``r.error is not None``):
        * ``value`` is **preserved** — a transient error must not wipe
          the previously-good snapshot.
        * ``last_attempt_at`` is advanced; ``last_success_at`` is not.
        * ``last_error`` is set to ``r.error``.
        * ``consecutive_failures`` is incremented.
        * ``from_cache`` is preserved (the previous good snapshot's
          provenance is still accurate; the failed attempt didn't
          produce a new value).

    In both cases ``r.elapsed_ms`` is appended to the ring, with the
    oldest entry evicted once the ring exceeds ``ring_size``, and
    :attr:`SlotState.p50_elapsed_ms` is recomputed from the new ring.
    The ``in_flight`` flag is unchanged here — the scheduler owns that
    field; :func:`apply` only reflects post-completion state.

    Identity preservation on no-op success (commit 4): when the
    incoming success carries a *value-equal but distinct object* to
    the previously-stored value, the new :class:`SlotState`'s
    ``value`` reuses ``prev.value``'s identity. This makes
    ``id(slot.value)`` stable across a no-op tick — selectors that
    key on it (e.g. the dashboard's ``select_dashboard_model``)
    cache-hit and the per-host repaint path elides the row
    recompute. Other fields
    (``last_attempt_at``, the ring, ``from_cache``) still advance:
    the *attempt* did happen and must be visible to staleness logic.

    The function is pure: same ``(prev, r, ring_size)`` always returns
    an equal :class:`SlotState`.
    """
    new_ring = (*prev.elapsed_ms_recent, r.elapsed_ms)
    if len(new_ring) > ring_size:
        new_ring = new_ring[-ring_size:]
    p50 = _median(new_ring)

    if r.error is None:
        # Identity-stable substitution on no-op success.
        # Only kicks in when the result is a *different object* whose
        # equality matches — selectors keyed on ``id(slot.value)``
        # cache-hit across the no-op tick.
        if prev.value is not None and r.value is not prev.value and r.value == prev.value:
            r = replace(r, value=prev.value)
        return SlotState(
            value=r.value,
            last_success_at=r.attempted_at,
            last_attempt_at=r.attempted_at,
            last_error=None,
            from_cache=r.from_cache,
            in_flight=prev.in_flight,
            consecutive_failures=0,
            elapsed_ms_recent=new_ring,
            p50_elapsed_ms=p50,
        )
    return SlotState(
        value=prev.value,
        last_success_at=prev.last_success_at,
        last_attempt_at=r.attempted_at,
        last_error=r.error,
        from_cache=prev.from_cache,
        in_flight=prev.in_flight,
        consecutive_failures=prev.consecutive_failures + 1,
        elapsed_ms_recent=new_ring,
        p50_elapsed_ms=p50,
    )

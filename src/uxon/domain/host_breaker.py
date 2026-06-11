"""Per-host circuit-breaker state machine.

A scheduler maintains one :class:`HostBreaker` per remote peer. The
breaker decides whether the next periodic fetch tick should run
(``should_attempt``), and the scheduler reports the outcome back via
:meth:`HostBreaker.on_success` / :meth:`HostBreaker.on_failure`. There
is no second retry layer above or below this — the breaker is the
sole owner of "should we even try?".

State machine
=============

``closed``
    Normal cadence. Each failure increments an internal counter; the
    ``trip_after``-th consecutive failure trips us to ``open``.
``open``
    ``should_attempt`` returns ``False`` until the current wall-clock
    is past ``next_attempt_at``. At that point we transition to
    ``half_open``.
``half_open``
    Exactly one probe is admitted. The scheduler sets ``in_flight`` via
    :meth:`mark_inflight` while the probe runs; a second tick that
    arrives during the probe is dropped (``should_attempt`` returns
    ``False``). On success we reset to ``closed`` with a fresh backoff
    window; on failure we go back to ``open`` with the window
    multiplied by ``factor`` and capped at ``cap_seconds``.

Jitter
======

``next_attempt_at`` is jittered by ``±half_open_jitter_pct%`` at
``on_failure`` time. This prevents an outage that trips N peers at
once from half-opening them all on the same instant: the jitter
spreads the half-open transitions across roughly
``backoff * 2 * jitter_pct / 100`` seconds.

Test seams
==========

The constructor accepts injectable ``clock`` and ``rng`` callables so
unit tests can drive time and jitter deterministically without
patching globals. Production callers leave them defaulted to
``time.monotonic`` and a per-instance :class:`random.Random` seeded
from ``os.urandom``.

Wiring into the SSH fetch loop happens in stage 8 of the
multi-host design spec; this module only ships the state machine.
"""

from __future__ import annotations

import os
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class BreakerSpec:
    """Static policy parameters for one :class:`HostBreaker`.

    All four knobs are intentionally simple constants. A breaker spec
    is per-host (the per-host config block in the multi-host TOML may
    override fleet-wide defaults), so tuning a flaky peer doesn't
    touch the others.

    Attributes:
        factor: Multiplicative growth factor applied to the backoff
            window on each failure. ``2.0`` doubles the window.
        cap_seconds: Maximum backoff window. The window stops growing
            once it hits this cap; further failures keep waiting at
            the cap rather than escalating indefinitely.
        trip_after: Number of consecutive failures while ``closed``
            before we trip to ``open``. Set to ``1`` for a hair-trigger
            breaker (open on first failure); the spec default of ``3``
            tolerates a couple of transient blips per peer.
        half_open_jitter_pct: ±% jitter applied to ``next_attempt_at``
            at ``on_failure`` time. ``25.0`` means the next half-open
            probe lands within ±25% of the nominal backoff window.
    """

    factor: float = 2.0
    cap_seconds: float = 60.0
    trip_after: int = 3
    half_open_jitter_pct: float = 25.0


BreakerState = Literal["closed", "open", "half_open"]


class HostBreaker:
    """Mutable per-host circuit-breaker state.

    Owned by the scheduler; not thread-safe — the scheduler is the
    single writer for any one breaker instance. The class is a plain
    object (not a dataclass) because the state mutates and we want
    explicit transition methods rather than ad-hoc field writes.
    """

    def __init__(
        self,
        spec: BreakerSpec,
        *,
        clock: Callable[[], float] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        """Build a breaker in the ``closed`` state.

        Args:
            spec: Policy knobs. Captured by reference; we never mutate
                it.
            clock: Wall-clock source returning seconds. Defaults to
                :func:`time.monotonic` — monotonic is intentional so a
                wall-clock jump (NTP step) can't suddenly mark the
                breaker as ready / not-ready.
            rng: Random source for jitter. Defaults to a per-instance
                :class:`random.Random` seeded from :func:`os.urandom`.
                Passing a seeded ``Random`` is the standard test seam.
        """
        self._spec = spec
        self._clock = clock if clock is not None else time.monotonic
        # Per-instance RNG so two breakers in the same process don't
        # share state and so test seeds are isolated.
        if rng is None:
            seed = int.from_bytes(os.urandom(8), "big")
            self._rng = random.Random(seed)
        else:
            self._rng = rng

        self.state: BreakerState = "closed"
        # ``backoff_seconds`` is the *current* window; reset to ``cap``
        # / factor on the very first trip so the first open period is
        # still bounded sensibly. We pre-seed it to ``cap_seconds``
        # divided by ``factor`` so the first ``open`` window is
        # ``cap_seconds`` after one factor multiplication. In practice
        # the first wait is computed as ``cap_seconds`` for the spec
        # defaults (30s × 2.0 capped at 60s).
        self._initial_backoff: float = max(spec.cap_seconds / spec.factor, 1.0)
        self.backoff_seconds: float = self._initial_backoff
        self.next_attempt_at: float = 0.0
        self.in_flight: bool = False
        self._consecutive_failures: int = 0

    # ── outcome reporting ────────────────────────────────────────────

    def on_success(self) -> None:
        """Mark the latest probe as successful.

        Resets the breaker to ``closed`` with a fresh backoff window
        and clears the consecutive-failure counter. ``in_flight`` is
        cleared as a defensive measure; the scheduler should also call
        :meth:`clear_inflight` explicitly so the contract is symmetric
        with :meth:`mark_inflight`.
        """
        self.state = "closed"
        self.backoff_seconds = self._initial_backoff
        self.next_attempt_at = 0.0
        self._consecutive_failures = 0
        self.in_flight = False

    def on_failure(self) -> None:
        """Mark the latest probe as failed.

        State transitions:

        * ``closed`` → increment failure counter; if it reaches
          ``trip_after``, transition to ``open`` with the initial
          backoff window.
        * ``half_open`` → back to ``open`` with the window multiplied
          by ``factor`` (capped at ``cap_seconds``).
        * ``open`` → should not normally occur (we only fail when a
          probe completed), but treat as another open transition with
          the doubled window for safety.

        ``next_attempt_at`` is recomputed using the current clock plus
        the new backoff window plus per-call jitter.
        """
        if self.state == "closed":
            self._consecutive_failures += 1
            if self._consecutive_failures < self._spec.trip_after:
                # Still under the trip threshold; remain closed and
                # let the next normal tick try again.
                return
            # First trip: start at the initial backoff window.
            self.backoff_seconds = self._initial_backoff
        else:
            # Either ``half_open`` (probe failed) or a defensive
            # ``open`` re-failure: double the window and cap it.
            self.backoff_seconds = min(
                self.backoff_seconds * self._spec.factor,
                self._spec.cap_seconds,
            )

        self.state = "open"
        self.in_flight = False
        # Jitter: uniform on ``[-pct, +pct]`` of the current window.
        # Multiplying by ``backoff_seconds`` gives the absolute jitter
        # in seconds.
        pct = self._spec.half_open_jitter_pct / 100.0
        jitter = self._rng.uniform(-pct, pct) * self.backoff_seconds
        self.next_attempt_at = self._clock() + self.backoff_seconds + jitter

    # ── admission control ────────────────────────────────────────────

    def should_attempt(self, now: float | None = None) -> bool:
        """Return ``True`` iff the scheduler should fire a fetch now.

        The ``now`` argument is optional for ergonomics — when omitted
        we sample the configured clock. Tests usually pass an explicit
        ``now`` so the comparison is deterministic.

        Rules:

        * ``closed`` — always admit; the cadence layer is what gates
          frequency.
        * ``open`` — admit iff ``now >= next_attempt_at``; on the
          admit, transition to ``half_open`` so subsequent ticks see
          the gate.
        * ``half_open`` — admit iff no probe is currently in flight.
        """
        if now is None:
            now = self._clock()

        if self.state == "closed":
            # The in-flight gate also applies when closed — a slow
            # worker mustn't get a second concurrent invocation.
            return not self.in_flight

        if self.state == "open":
            if now < self.next_attempt_at:
                return False
            # Time's up: promote to half_open and admit this probe.
            self.state = "half_open"
            return not self.in_flight

        # half_open: exactly one probe at a time.
        return not self.in_flight

    # ── in-flight gate ───────────────────────────────────────────────

    def mark_inflight(self) -> None:
        """Tell the breaker a probe is currently running.

        The scheduler calls this immediately after :meth:`should_attempt`
        returns ``True`` and before kicking off the worker. The gate
        prevents a second tick that arrives during a slow probe from
        spawning a duplicate request.
        """
        self.in_flight = True

    def clear_inflight(self) -> None:
        """Tell the breaker the probe finished (any outcome).

        Pair with :meth:`mark_inflight`. :meth:`on_success` and
        :meth:`on_failure` also clear the gate as a safety net, but
        callers should treat the explicit clear as the contract.
        """
        self.in_flight = False

"""Single locus for render-cadence decisions.

Handlers that mutate ``state.main`` / ``state.remote`` call
:meth:`RenderScheduler.request` with a kind tag. The scheduler decides
when to fire the ``render`` callback, coalescing dirty kinds across
requests.

Algorithm — adaptive debounce with max-latency cap and leading-edge
immediate fire:

- Idle scheduler (no pending timer, no recent fire within
  ``debounce_ms``): first request fires the render immediately. First
  paint and idle-arrival are latency-critical.
- Burst (more requests within the debounce window of the last fire,
  or while a debounced fire is pending): coalesced into a single
  trailing render scheduled ``debounce_ms`` after the latest request,
  but no later than ``first_request_in_batch + max_latency_ms``.
- Render returns False (e.g. no ``MainScreen`` on top — modal is up):
  dirty is preserved. No auto-retry on a timer. The next ``request``
  call will see idle state (no fire happened) and fire immediately.
  If no further requests arrive while the modal is up, the dirty
  state waits until the next refresh-source landing.

Swap the algorithm by replacing this class. Callers depend only on
:meth:`request` and :meth:`shutdown`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from textual.app import App
    from textual.timer import Timer


class RenderScheduler:
    def __init__(
        self,
        app: App,
        *,
        debounce_ms: int,
        max_latency_ms: int,
        render: Callable[[frozenset[str]], bool],
    ) -> None:
        self._app = app
        self._debounce = debounce_ms / 1000.0
        self._max_latency = max_latency_ms / 1000.0
        self._render = render
        self._dirty: set[str] = set()
        self._timer: Timer | None = None
        self._batch_start: float | None = None
        self._last_fire: float | None = None

    def request(self, kind: str) -> None:
        now = time.monotonic()
        self._dirty.add(kind)
        if self._timer is not None:
            self._reschedule(now)
            return
        cool = self._last_fire is not None and (now - self._last_fire) < self._debounce
        if not cool:
            self._batch_start = now
            self._fire()
            return
        self._batch_start = now
        self._reschedule(now)

    def shutdown(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._dirty.clear()
        self._batch_start = None

    def _reschedule(self, now: float) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        assert self._batch_start is not None
        elapsed = now - self._batch_start
        remaining_cap = max(0.0, self._max_latency - elapsed)
        delay = min(self._debounce, remaining_cap)
        if delay <= 0:
            self._fire()
            return
        self._timer = self._app.set_timer(delay, self._fire)

    def _fire(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._batch_start = None
        if not self._dirty:
            return
        dirty = frozenset(self._dirty)
        rendered = self._render(dirty)
        if rendered:
            self._dirty -= dirty
            self._last_fire = time.monotonic()

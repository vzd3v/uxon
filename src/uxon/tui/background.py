"""Bounded local collectors shared across dashboard/terminal handoffs.

Fetching can start before Textual is imported. App instances only subscribe
to futures; cancelling a subscriber must not cancel a collector needed by
the next dashboard. No UI objects are owned by this executor.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from concurrent.futures import Future, ThreadPoolExecutor

from .refresh import SourceResult, SourceSpec, run_source


class BackgroundSources:
    def __init__(self) -> None:
        # Sessions, directories, metrics and one binary-discovery task.
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="uxon-local")
        self._lock = threading.Lock()
        self._futures: dict[str, tuple[str, Future[SourceResult]]] = {}

    def start(self, spec: SourceSpec, *, reuse: bool = False) -> Future[SourceResult]:
        with self._lock:
            previous = self._futures.get(spec.name)
            if previous is not None and previous[0] == spec.key:
                if reuse or not previous[1].done():
                    return previous[1]
            future = self._pool.submit(run_source, spec)
            self._futures[spec.name] = (spec.key, future)
            return future

    def prefetch(self, sources: Iterable[SourceSpec]) -> None:
        for spec in sources:
            if spec.prefetch:
                self.start(spec)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

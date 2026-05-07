# SPDX-License-Identifier: MIT
"""Stage 5 step 7: fleet-wide fetch-concurrency semaphore.

Pure-threading test. Reconstructs the same closure shape that
``_build_tui_context`` uses and verifies that no more than
``cfg.fetch_concurrency`` fetchers are in-flight at once.
"""

from __future__ import annotations

import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor


def _make_remote_fetch(sem: threading.Semaphore, body):
    def _fetch():
        sem.acquire()
        try:
            return body()
        finally:
            sem.release()

    return _fetch


class FetchConcurrencyTests(unittest.TestCase):
    def test_semaphore_caps_inflight_workers(self) -> None:
        """N=9 fetchers, semaphore=3 — observed max_inflight ≤ 3."""
        sem = threading.Semaphore(3)
        inflight = 0
        max_inflight = 0
        lock = threading.Lock()

        def body():
            nonlocal inflight, max_inflight
            with lock:
                inflight += 1
                max_inflight = max(max_inflight, inflight)
            time.sleep(0.05)
            with lock:
                inflight -= 1
            return "ok"

        fetchers = [_make_remote_fetch(sem, body) for _ in range(9)]
        with ThreadPoolExecutor(max_workers=9) as pool:
            results = list(pool.map(lambda f: f(), fetchers))

        self.assertEqual(results, ["ok"] * 9)
        self.assertLessEqual(max_inflight, 3, f"semaphore breached: max_inflight={max_inflight}")
        # Sanity: at least 2 actually ran in parallel — otherwise the
        # cap could be hiding a serialised executor instead of a
        # genuine concurrency limit.
        self.assertGreaterEqual(max_inflight, 2)

    def test_semaphore_releases_on_exception(self) -> None:
        """A fetcher that raises must still release its slot."""
        sem = threading.Semaphore(1)

        def boom():
            raise RuntimeError("simulated fetch failure")

        fetcher = _make_remote_fetch(sem, boom)
        with self.assertRaises(RuntimeError):
            fetcher()

        # After the failure the slot must be free; a second acquire
        # succeeds without blocking.
        acquired = sem.acquire(blocking=False)
        self.assertTrue(acquired, "semaphore not released after fetcher exception")
        sem.release()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

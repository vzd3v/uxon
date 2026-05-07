# SPDX-License-Identifier: MIT
"""Audit-channel performance gate.

Off by default — gated by ``UXON_PERF=1`` so it never runs on CI.  The
suite as a whole must stay deterministic; perf assertions belong here,
not in the unit tests.

Spec budgets (``docs/superpowers/specs/2026-05-05-audit-log-design.md``
§ Performance verification):

- cold first-call latency:  < 200 µs (sink detection + connect)
- steady-state median:      <  30 µs
- steady-state p99:          < 100 µs

The test harness patches ``_send_raw`` to a no-op — we measure the
hot-path machinery, not the kernel datagram round-trip.
"""

from __future__ import annotations

import os
import time
import unittest
from unittest.mock import patch

from uxon import audit as au


def _reset_audit_state() -> None:
    au.enabled = True
    au.sink = ""
    au._initialized = False
    au._socket = None
    au._prefix = {}
    au._prefix_subcmd = ""
    au._syslog_facility_name = "user"
    au._correlation_id = None


@unittest.skipUnless(os.environ.get("UXON_PERF") == "1", "UXON_PERF not set")
class AuditOverheadTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_audit_state()
        self.addCleanup(_reset_audit_state)

    def test_steady_state_latency_under_budget(self) -> None:
        with (
            patch.object(au, "_detect_sink", return_value="syslog"),
            patch.object(au, "_open_sink_socket", return_value=object()),
            patch.object(au, "_send_raw", lambda payload: None),
        ):
            au.configure(enabled=True, syslog_facility="user", subcmd="run")
            # Warm-up + cold-call timing.
            t0 = time.perf_counter_ns()
            au.audit("cli.start", flags=[], agents_enabled=["claude"])
            cold_us = (time.perf_counter_ns() - t0) / 1000.0

            samples: list[float] = []
            for _ in range(10_000):
                t = time.perf_counter_ns()
                au.audit("session.attach", session="s", target_user="u")
                samples.append((time.perf_counter_ns() - t) / 1000.0)

            samples.sort()
            median = samples[len(samples) // 2]
            p99 = samples[int(len(samples) * 0.99)]

            self.assertLess(cold_us, 200.0, f"cold call {cold_us:.1f} µs > 200 µs")
            self.assertLess(median, 30.0, f"median {median:.1f} µs > 30 µs")
            self.assertLess(p99, 100.0, f"p99 {p99:.1f} µs > 100 µs")


if __name__ == "__main__":
    unittest.main()

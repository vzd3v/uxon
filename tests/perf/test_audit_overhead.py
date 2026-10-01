# SPDX-License-Identifier: MIT
"""Audit-channel performance gate.

Off by default — gated by ``UXON_PERF=1`` so it never runs on CI.  The
suite as a whole must stay deterministic; perf assertions belong here,
not in the unit tests.

Channel-machinery budgets, after CLI module startup:

- cold first-call latency:  < 200 µs
- steady-state median:      <  30 µs
- steady-state p99:          < 100 µs

Only external OS, NSS, and socket boundaries are controlled. Real sink
detection, identity parsing, prefix construction, serialization, and send
dispatch remain in the timed path. A separate real Unix-datagram measurement
reports host-dependent first-call latency without imposing these budgets.
Budget assertions require an idle benchmark host; they include scheduling
delays and must not be interpreted as a portable response-time guarantee.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Every production CLI route imports identity before its first audit event.
import uxon.cli  # noqa: F401
from uxon.infra import audit as au
from uxon.infra import identity


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

    def test_channel_machinery_latency_under_budget(self) -> None:
        for sink in ("journal", "syslog"):
            with self.subTest(sink=sink):
                _reset_audit_state()
                datagram = _Datagram()
                socket_info = SimpleNamespace(st_mode=stat.S_IFSOCK)

                def inspect_sink(path, *, expected_sink=sink, info=socket_info):
                    if path == au._JOURNAL_SOCKET_PATH and expected_sink == "syslog":
                        raise FileNotFoundError(path)
                    return info

                def read_loginuid(path, **kwargs):
                    self.assertEqual(str(path), "/proc/self/loginuid")
                    return "1000\n"

                account = SimpleNamespace(pw_name="alice")

                def lookup_user(uid, *, record=account):
                    self.assertEqual(uid, 1000)
                    return record

                def open_datagram(family, kind, *, target=datagram):
                    self.assertEqual(family, socket.AF_UNIX)
                    self.assertTrue(kind & socket.SOCK_NONBLOCK)
                    return target

                with (
                    patch.object(au.os, "stat", inspect_sink),
                    patch.object(identity.Path, "read_text", read_loginuid),
                    patch.object(identity.pwd, "getpwuid", lookup_user),
                    patch.object(au.socket, "gethostname", lambda: "test-host"),
                    patch.object(au.socket, "socket", open_datagram),
                ):
                    au.configure(enabled=True, syslog_facility="user", subcmd="run")
                    t0 = time.perf_counter_ns()
                    au.audit("cli.start", flags=[], profiles_enabled=["claude"])
                    cold_us = (time.perf_counter_ns() - t0) / 1000.0
                    self.assertEqual(au.sink, sink)
                    self.assertTrue(au._initialized)
                    self.assertEqual(datagram.sent, 1)
                    expected_path = (
                        au._JOURNAL_SOCKET_PATH if sink == "journal" else au._DEV_LOG_PATH
                    )
                    self.assertEqual(datagram.connected, expected_path)
                    if sink == "journal":
                        fields = dict(
                            line.split("=", 1) for line in datagram.payload.decode().splitlines()
                        )
                        self.assertEqual(fields["EVENT"], "cli.start")
                        self.assertEqual(fields["PROCESS_USER"], "alice")
                        self.assertEqual(fields["PROCESS_UID"], "1000")
                        self.assertEqual(json.loads(fields["PROFILES_ENABLED"]), ["claude"])
                    else:
                        fields = json.loads(datagram.payload.decode().split("@cee: ", 1)[1])
                        self.assertEqual(fields["event"], "cli.start")
                        self.assertEqual(fields["process_user"], "alice")
                        self.assertEqual(fields["process_uid"], 1000)
                        self.assertEqual(fields["profiles_enabled"], ["claude"])

                    samples: list[float] = []
                    for _ in range(10_000):
                        t = time.perf_counter_ns()
                        au.audit("session.attach.dispatch", session="s", target_user="u")
                        samples.append((time.perf_counter_ns() - t) / 1000.0)
                    self.assertEqual(datagram.sent, 10_001)
                    samples.sort()
                    median = samples[len(samples) // 2]
                    p99 = samples[int(len(samples) * 0.99)]
                    print(
                        f"audit {sink} machinery: cold={cold_us:.1f}, median={median:.1f}, p99={p99:.1f} µs"
                    )
                    self.assertLess(cold_us, 200.0, f"cold call {cold_us:.1f} µs > 200 µs")
                    self.assertLess(median, 30.0, f"median {median:.1f} µs > 30 µs")
                    self.assertLess(p99, 100.0, f"p99 {p99:.1f} µs > 100 µs")

    def test_actual_host_first_call_reports_latency_and_delivers_event(self) -> None:
        with tempfile.TemporaryDirectory(prefix="uxon-audit-perf-") as tmp:
            endpoint = str(Path(tmp) / "syslog.sock")
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
                receiver.bind(endpoint)
                receiver.settimeout(1)
                with (
                    patch.object(au, "_JOURNAL_SOCKET_PATH", str(Path(tmp) / "absent")),
                    patch.object(au, "_DEV_LOG_PATH", endpoint),
                ):
                    try:
                        au.configure(enabled=True, syslog_facility="user", subcmd="run")
                        t0 = time.perf_counter_ns()
                        au.audit("perf.measurement")
                        elapsed_us = (time.perf_counter_ns() - t0) / 1000.0
                        payload = receiver.recv(65536)
                        fields = json.loads(payload.decode().split("@cee: ", 1)[1])
                        self.assertEqual(au.sink, "syslog")
                        self.assertEqual(fields["event"], "perf.measurement")
                        actor = identity.login_identity()
                        self.assertEqual(fields["process_uid"], actor.uid)
                        self.assertEqual(fields["process_user"], actor.user)
                        print(
                            f"audit actual host first call: {elapsed_us:.1f} µs (OS/NSS + local Unix datagram)"
                        )
                    finally:
                        if au._socket is not None:
                            au._socket.close()


class _Datagram:
    def __init__(self) -> None:
        self.connected = ""
        self.sent = 0
        self.payload = b""

    def connect(self, path: str) -> None:
        self.connected = path

    def send(self, payload: bytes) -> int:
        self.sent += 1
        self.payload = payload
        return len(payload)


if __name__ == "__main__":
    unittest.main()

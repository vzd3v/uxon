# SPDX-License-Identifier: MIT
"""Contract tests for ``uxon.infra.run.run_query`` and the loop-guard sanction marker.

Fast siblings of the deterministic pty gate in
``test_run_query_tty_isolation.py`` (AC6a, ``slow``). These exercise the public
contracts directly without a real pty:

  * AC1 — a ``run_query`` child is detached from the controlling terminal
    (``start_new_session=True`` → its own session leader → ``open('/dev/tty')``
    fails with ``ENXIO``).
  * AC5 — ``input=`` is fed via a ``PIPE`` (text and bytes / ``text=False``).
  * AC2 — the guard recognises the sanction marker: ``run_query`` /
    ``handoff_spawn`` never trip it; an unmarked raw spawn logs and (only under
    the strict opt-in env) raises ``UnsanctionedSpawnError``, and the guard never
    rewrites the spawn's kwargs.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import unittest
from unittest import mock

from uxon.infra import loop_guard
from uxon.infra.run import run_query

# Child that reports (a) whether it is its own session leader — the
# environment-independent proof that ``start_new_session`` took effect — and
# (b) what happens when it opens the controlling terminal.
_DETACH_PROBE = (
    "import os\n"
    "leader = os.getsid(0) == os.getpid()\n"
    "try:\n"
    "    fd = os.open('/dev/tty', os.O_RDWR)\n"
    "    os.close(fd)\n"
    "    tty = 'OPENED'\n"
    "except OSError as e:\n"
    "    tty = 'ERR:%d' % (e.errno or 0)\n"
    "import sys; sys.stdout.write('%s %s' % (leader, tty))\n"
)


class RunQueryContractTests(unittest.TestCase):
    def test_child_is_detached_from_controlling_tty(self) -> None:
        """AC1: the child leads its own session and cannot reach /dev/tty."""
        cp = run_query([sys.executable, "-c", _DETACH_PROBE], timeout=10)
        leader, tty = cp.stdout.split()
        self.assertEqual(
            leader, "True", "start_new_session must take effect: child is its own session leader"
        )
        self.assertNotEqual(tty, "OPENED", "a detached child must not reach the controlling tty")
        self.assertEqual(
            tty,
            f"ERR:{errno.ENXIO}",
            f"open('/dev/tty') must fail with ENXIO ({errno.ENXIO}); got {tty}",
        )

    def test_feeds_text_input_via_pipe(self) -> None:
        """AC5: input= is piped to the child (so stdin is a PIPE, not DEVNULL)."""
        cp = run_query(
            [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
            input="hello-pipe",
            timeout=10,
        )
        self.assertEqual(cp.stdout, "hello-pipe")

    def test_feeds_bytes_input_when_text_false(self) -> None:
        """AC5: the text=False bytes input case (settings.py:285's shape)."""
        payload = b"\x00\x01raw-bytes\xff"
        cp = run_query(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
            input=payload,
            text=False,
            timeout=10,
        )
        self.assertEqual(cp.stdout, payload)


class GuardSanctionTests(unittest.TestCase):
    """AC2: marker-based detection, opt-in strict raise, no kwargs mutation."""

    def setUp(self) -> None:
        loop_guard.install_subprocess_guard()  # idempotent — conftest already installed it
        os.environ.pop(loop_guard._STRICT_ENV, None)
        # Reset the process-global log-once latch so these tests are isolated
        # from any unsanctioned spawn elsewhere in the suite (and from each other).
        loop_guard._unsanctioned_logged = False

    def test_run_query_never_trips_the_strict_raise(self) -> None:
        with mock.patch.dict(os.environ, {loop_guard._STRICT_ENV: "1"}):
            cp = run_query(["true"], timeout=10)
        self.assertEqual(cp.returncode, 0)

    def test_handoff_spawn_never_trips_the_strict_raise(self) -> None:
        with mock.patch.dict(os.environ, {loop_guard._STRICT_ENV: "1"}):
            with loop_guard.handoff_spawn():
                cp = subprocess.run(["true"], capture_output=True)
        self.assertEqual(cp.returncode, 0)

    def test_unmarked_raw_spawn_raises_under_strict(self) -> None:
        with mock.patch.dict(os.environ, {loop_guard._STRICT_ENV: "1"}):
            with self.assertRaises(loop_guard.UnsanctionedSpawnError):
                subprocess.run(["true"], capture_output=True)

    def test_unmarked_raw_spawn_allowed_without_strict(self) -> None:
        # Default (env unset): a marker-absent spawn logs once and proceeds.
        cp = subprocess.run(["true"], capture_output=True)
        self.assertEqual(cp.returncode, 0)

    def test_guard_does_not_mutate_kwargs(self) -> None:
        # The reverted heuristic used to inject start_new_session/stdin on
        # capturing spawns. A raw capturing spawn must now stay un-detached:
        # its child is NOT its own session leader.
        cp = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os,sys; sys.stdout.write(str(os.getsid(0)==os.getpid()))",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            cp.stdout,
            "False",
            "the guard must not inject start_new_session — it never mutates kwargs",
        )


if __name__ == "__main__":
    unittest.main()

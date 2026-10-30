# SPDX-License-Identifier: MIT
"""Contract tests for ``uxon.infra.run.run_query`` and the loop-guard sanction marker.

Fast siblings of the deterministic pty gate in
``test_run_query_tty_isolation.py`` (``slow``). These exercise the public
contracts directly without a real pty:

  * A ``run_query`` child is detached from the controlling terminal
    (``start_new_session=True`` → its own session leader → ``open('/dev/tty')``
    fails with ``ENXIO``).
  * ``input=`` is fed via a ``PIPE`` (text and bytes / ``text=False``).
  * The guard recognises the sanction marker: ``run_query`` /
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
        """The child leads its own session and cannot reach /dev/tty."""
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

    def test_text_bytes_stderr_check_and_timeout_at_child_boundary(self) -> None:
        script = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()); sys.stderr.write('diagnostic')"
        for payload, text in (("hello-pipe", True), (b"\x00\x01raw-bytes\xff", False)):
            with self.subTest(text=text):
                cp = run_query([sys.executable, "-c", script], input=payload, text=text, timeout=10)
                self.assertEqual(cp.stdout, payload)
                self.assertEqual(cp.stderr, "diagnostic" if text else b"diagnostic")
                self.assertEqual(cp.returncode, 0)
        with self.assertRaises(subprocess.CalledProcessError) as failure:
            run_query(
                [sys.executable, "-c", "import sys;sys.stderr.write('failed');sys.exit(7)"],
                timeout=10,
                check=True,
            )
        self.assertEqual(failure.exception.returncode, 7)
        self.assertEqual(failure.exception.stderr, "failed")
        with self.assertRaises(subprocess.TimeoutExpired):
            run_query([sys.executable, "-c", "import time;time.sleep(10)"], timeout=0.1)


class GuardSanctionTests(unittest.TestCase):
    """Marker-based detection, opt-in strict raise, no kwargs mutation."""

    def test_strict_sanction_resets_after_success_and_failure_default_preserves_kwargs(
        self,
    ) -> None:
        loop_guard.install_subprocess_guard()
        with (
            mock.patch.object(loop_guard, "_unsanctioned_logged", False),
            mock.patch.dict(os.environ),
        ):
            os.environ[loop_guard._STRICT_ENV] = "1"

            def refused():
                with self.assertRaises(loop_guard.UnsanctionedSpawnError):
                    subprocess.run(["true"], capture_output=True, timeout=10)

            refused()
            self.assertEqual(run_query(["true"], timeout=10).returncode, 0)
            refused()
            with loop_guard.handoff_spawn():
                self.assertEqual(
                    subprocess.run(["true"], capture_output=True, timeout=10).returncode, 0
                )
            refused()
            with self.assertRaises(FileNotFoundError):
                run_query(["/nonexistent/uxon-test-command"], timeout=10)
            refused()
            with self.assertRaises(RuntimeError):
                with loop_guard.handoff_spawn():
                    raise RuntimeError("handoff failed")
            refused()
            os.environ.pop(loop_guard._STRICT_ENV)
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
                timeout=10,
            )
            self.assertEqual(cp.stdout, "False", "default guard must preserve caller spawn kwargs")
            self.assertEqual(cp.returncode, 0)


if __name__ == "__main__":
    unittest.main()

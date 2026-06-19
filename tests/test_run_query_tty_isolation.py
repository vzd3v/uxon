# SPDX-License-Identifier: MIT
"""AC6a — deterministic regression test for the controlling-tty keystroke flush.

This is the done-gate for the keystroke-swallow fix. It reproduces the exact
mechanism with a ``pty.fork`` harness and proves :func:`uxon.infra.run.run_query`
prevents it on the *real* spawn path:

  * positive: a tty-flushing child spawned via ``run_query`` gets ``ENXIO`` on
    ``/dev/tty`` (no controlling terminal) and **all buffered keystrokes
    survive** to ``os.read``.
  * negative control: the identical child spawned via a raw ``subprocess.run``
    inherits the controlling terminal, ``tcflush``es it, and **the keystrokes
    are lost**.

The negative control is mandatory: without it a green positive could merely mean
"nothing flushed this run", not "the fix worked". A buggy build must fail the
negative test (keys must be lost there), so the pair together is a real gate and
not a tautology.

Faithful to production: uxon sits on a pty slave (the controlling terminal); a
worker's child inherits it unless detached. Here the parent holds the master and
injects keystrokes into the kernel input buffer — exactly where the swallow
happens, below ``os.read`` and invisible to any in-process tap.

Heavy and timing-sensitive (real pty + sleeps), so the module is marked ``slow``
and deselected from the fast per-change suite. Run it with ``pytest -m slow`` or
``pytest tests/test_run_query_tty_isolation.py -m slow``.
"""

from __future__ import annotations

import os
import pty
import select
import subprocess
import sys
import tempfile
import time
import tty as ttymod
import unittest
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow

_PTY_OK = hasattr(pty, "fork")

# The buffered keys the user typed but uxon has not yet read (cf. the real
# capture's lost '7 6 5 4 3').
PAYLOAD = b"7654321"

# Child program: open the controlling terminal and flush its input queue,
# exactly as sudo/ssh/tmux do on entry or restore-at-exit. Records whether it
# reached the tty (flushed) or was denied one (no controlling tty -> ENXIO).
_FLUSHER = (
    "import os, termios, pathlib\n"
    "try:\n"
    "    fd = os.open('/dev/tty', os.O_RDWR)\n"
    "    termios.tcflush(fd, termios.TCIFLUSH)\n"
    "    os.close(fd)\n"
    "    pathlib.Path({marker!r}).write_text('flushed')\n"
    "except OSError as e:\n"
    "    pathlib.Path({marker!r}).write_text('no-tty:' + repr(e))\n"
)


def _child_session(use_run_query: bool, marker: str) -> None:
    """Runs inside the pty.fork child (fd 0/1 are the pty slave)."""
    ttymod.setraw(0)  # uxon's input thread keeps the tty in raw mode
    time.sleep(0.25)  # let the parent inject PAYLOAD into the kernel buffer
    argv = [sys.executable, "-c", _FLUSHER.format(marker=marker)]
    if use_run_query:
        from uxon.infra.run import run_query

        run_query(argv, timeout=10)
    else:
        # Negative control: raw spawn with no capture -> inherits the
        # controlling terminal -> can flush the buffered input.
        subprocess.run(argv, timeout=10)  # noqa: PLW1510 (intentional raw spawn)
    # Read whatever is still in the buffer after the child ran.
    survived = bytearray()
    end = time.monotonic() + 0.5
    while time.monotonic() < end:
        r, _, _ = select.select([0], [], [], 0.1)
        if not r:
            continue
        try:
            chunk = os.read(0, 4096)
        except OSError:
            break
        if not chunk:
            break
        survived.extend(chunk)
    os.write(1, b"SURVIVED:" + bytes(b for b in survived if b in PAYLOAD))
    os._exit(0)


def _run_case(use_run_query: bool, marker: str) -> tuple[bytes, str]:
    """Fork the pty child, inject PAYLOAD, return (survived_bytes, flusher_marker)."""
    pid, master = pty.fork()
    if pid == 0:  # child
        try:
            _child_session(use_run_query, marker)
        except BaseException:  # never let the child fall through to the test body
            os._exit(1)
    # Parent holds the master: inject the keystrokes into the slave's input buffer.
    time.sleep(0.1)
    os.write(master, PAYLOAD)
    out = bytearray()
    end = time.monotonic() + 5.0
    while time.monotonic() < end:
        r, _, _ = select.select([master], [], [], 0.1)
        if r:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            out.extend(chunk)
        if b"SURVIVED:" in bytes(out):
            break
    os.waitpid(pid, 0)
    os.close(master)
    txt = bytes(out)
    i = txt.find(b"SURVIVED:")
    survived = txt[i + len(b"SURVIVED:") :] if i >= 0 else b"?"
    try:
        marker_txt = Path(marker).read_text()
    except OSError:
        marker_txt = "(child wrote no marker)"
    return survived, marker_txt


@unittest.skipUnless(_PTY_OK, "pty.fork unavailable on this platform")
class RunQueryTtyIsolationTests(unittest.TestCase):
    def test_run_query_preserves_buffered_keystrokes(self) -> None:
        """A run_query child cannot reach /dev/tty, so no keystroke is flushed."""
        with tempfile.TemporaryDirectory() as d:
            marker = os.path.join(d, "flusher.mark")
            survived, child = _run_case(use_run_query=True, marker=marker)
        self.assertIn("no-tty", child, f"run_query child should be denied /dev/tty; got {child!r}")
        self.assertEqual(
            survived,
            PAYLOAD,
            f"run_query must preserve every buffered keystroke; survived={survived!r}",
        )

    def test_raw_spawn_loses_buffered_keystrokes(self) -> None:
        """Negative control: a raw spawn DOES flush — proves the harness can see the bug."""
        with tempfile.TemporaryDirectory() as d:
            marker = os.path.join(d, "flusher.mark")
            survived, child = _run_case(use_run_query=False, marker=marker)
        self.assertEqual(child, "flushed", f"raw-spawn child should reach /dev/tty; got {child!r}")
        self.assertNotEqual(
            survived,
            PAYLOAD,
            "negative control FAILED: a raw tty-inheriting spawn did not lose the "
            "buffered keystrokes — the test is not exercising the flush, so a green "
            "positive would be meaningless",
        )


if __name__ == "__main__":
    unittest.main()

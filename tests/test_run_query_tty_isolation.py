"""Paired kernel TTY control: detached children preserve keys, raw children flush."""

import errno
import os
import select
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from harness.pty_tui import _make_controlling_tty

from uxon.infra.loop_guard import handoff_spawn

pytestmark = pytest.mark.slow
PAYLOAD = b"7654321"

_CHILD = r'''
import array, fcntl, os, select, subprocess, sys, termios, time, tty
tty.setraw(0)
os.write(1, b"READY\n")
assert os.read({trigger}, 1) == b"!"
deadline = time.monotonic() + 5
buffered = array.array('i', [0])
while time.monotonic() < deadline:
    select.select([0], [], [], 0.1)
    fcntl.ioctl(0, termios.FIONREAD, buffered, True)
    if buffered[0] >= 7:
        break
assert buffered[0] >= 7, 'payload never reached the kernel input queue'
flusher = """import os, termios, pathlib
try:
    fd = os.open('/dev/tty', os.O_RDWR)
    termios.tcflush(fd, termios.TCIFLUSH)
    os.close(fd)
    outcome = 'flushed'
except OSError as exc:
    outcome = 'errno:' + str(exc.errno)
pathlib.Path({marker!r}).write_text(outcome)
"""
argv = [sys.executable, '-c', flusher]
if {detached}:
    from uxon.infra.run import run_query
    result = run_query(argv, timeout=5)
else:
    result = subprocess.run(argv, timeout=5)
assert result.returncode == 0
ready, _, _ = select.select([0], [], [], 0.2)
survived = os.read(0, 4096) if ready else b''
os.write(1, b"SURVIVED:" + survived)
'''


def _run_case(detached, marker):
    master, slave = os.openpty()
    trigger_read, trigger_write = os.pipe()
    process = None
    output = bytearray()
    try:
        with handoff_spawn():
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _CHILD.format(trigger=trigger_read, marker=str(marker), detached=detached),
                ],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                pass_fds=(trigger_read,),
                preexec_fn=lambda: _make_controlling_tty(slave),
            )
        os.close(slave)
        slave = -1
        os.close(trigger_read)
        trigger_read = -1
        deadline = time.monotonic() + 10
        injected = False
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master], [], [], 0.1)
            if readable:
                try:
                    chunk = os.read(master, 4096)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
                if not chunk:
                    break
                output.extend(chunk)
                if b"READY\n" in output and not injected:
                    os.write(master, PAYLOAD)
                    os.write(trigger_write, b"!")
                    injected = True
        assert injected, f"TTY child never acknowledged raw-mode readiness: {bytes(output)!r}"
        code = process.wait(timeout=1)
        assert code == 0, f"TTY child exit={code}: {bytes(output)!r}"
        assert b"SURVIVED:" in output, f"TTY child omitted result: {bytes(output)!r}"
        return bytes(output).split(b"SURVIVED:", 1)[1], marker.read_text()
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        for descriptor in (master, slave, trigger_read, trigger_write):
            if descriptor >= 0:
                os.close(descriptor)


def test_detachment_preserves_keys_with_real_flushing_negative_control():
    with tempfile.TemporaryDirectory() as directory:
        survived, flusher = _run_case(True, Path(directory) / "detached")
        assert flusher == f"errno:{errno.ENXIO}"
        assert survived == PAYLOAD
        survived, flusher = _run_case(False, Path(directory) / "raw")
        assert flusher == "flushed", "negative child must actually reach and flush /dev/tty"
        assert survived == b"", "negative control must lose every buffered key"

#!/usr/bin/env python3
"""Example Linux workload stopper; install read-only inside the runtime image."""

from __future__ import annotations

import argparse
import math
import os
import select
import signal
import stat
import sys
from pathlib import Path


def stop_workload(pidfile: Path, timeout: float) -> None:
    """Verify PID/start ticks and wait for TERM through a stable process handle."""
    with os.fdopen(
        os.open(pidfile, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), encoding="ascii"
    ) as record:
        info = os.fstat(record.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError("workload record is not an owned regular file")
        fields = record.read(1024).split()
    if len(fields) != 2 or not all(field.isdecimal() for field in fields):
        raise ValueError("invalid workload record: expected PID and start ticks")
    pid, start_ticks = map(int, fields)
    if pid <= 1 or start_ticks <= 0:
        raise ValueError("invalid workload process identity")
    try:
        process_fd = os.pidfd_open(pid)
    except ProcessLookupError:
        process_fd = None
    if process_fd is not None:
        try:
            try:
                live_stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            except FileNotFoundError:
                live_stat = ""
            if live_stat:
                live_ticks = int(live_stat.rsplit(") ", 1)[1].split()[19])
                if live_ticks != start_ticks:
                    raise ValueError("workload PID has been reused; refusing to signal")
                try:
                    signal.pidfd_send_signal(process_fd, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            poller = select.poll()
            poller.register(process_fd, select.POLLIN)
            if not poller.poll(math.ceil(timeout * 1000)):
                raise TimeoutError("workload did not exit after TERM")
        finally:
            os.close(process_fd)
    current = pidfile.stat(follow_symlinks=False)
    if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
        raise ValueError("workload record changed during stop")
    pidfile.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pidfile", type=Path)
    parser.add_argument("--timeout", type=float, required=True, help="TERM wait budget in seconds")
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be positive and finite")
    try:
        stop_workload(args.pidfile, args.timeout)
    except (OSError, ValueError, IndexError) as exc:
        print(f"workload stop failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

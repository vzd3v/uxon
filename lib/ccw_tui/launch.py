"""Launch-handoff helpers.

These run OUTSIDE the textual App — between an ``App.exit()`` and the
next ``App()`` re-enter. They execute the :class:`LaunchRequest` the
TUI emitted and hold the terminal open so the user can read stderr on
failure. No blessed / no textual imports at module level.
"""

from __future__ import annotations

import subprocess
import sys
import time as _time
from typing import Any

from .context import LaunchRequest


#: Threshold below which an rc=0 launch is treated as a silent fast-exit.
FAST_EXIT_THRESHOLD_SEC = 1.0


def _run_launch_request(req: "LaunchRequest") -> tuple[int, str, float]:
    """Execute a LaunchRequest via fork-and-wait.

    Runs each prelaunch command in order, aborting if any returns non-zero,
    then runs the main ``cmd``. Returns ``(rc, stage, wall_seconds)``.
    """
    t0 = _time.monotonic()
    for pre in req.prelaunch:
        rc = subprocess.call(list(pre))
        if rc != 0:
            return rc, "prelaunch", _time.monotonic() - t0
    rc = subprocess.call(list(req.cmd))
    return rc, "cmd", _time.monotonic() - t0


def pause_on_launch_failure(
    stream: Any, req: LaunchRequest, rc: int, stage: str, wall_seconds: float
) -> None:
    """Hold the terminal after a failed launch so the user can read stderr.

    Plain-text — no blessed escape codes. Stays silent on rc=0 unless
    the launch returned in under :data:`FAST_EXIT_THRESHOLD_SEC` (almost
    certainly a silent failure).
    """
    fast_zero = rc == 0 and wall_seconds < FAST_EXIT_THRESHOLD_SEC
    if rc == 130:  # user Ctrl-C
        return
    if rc == 0 and not fast_zero:
        return
    label = req.label or "launch"
    stream.write("\n")
    if fast_zero:
        stream.write(
            f"ccw: {label} exited immediately (rc=0 in {wall_seconds:.2f}s, stage={stage})\n"
        )
    else:
        stream.write(f"ccw: {label} failed (rc={rc}, stage={stage})\n")
    if stage == "prelaunch":
        first = list(req.prelaunch[0]) if req.prelaunch else []
        stream.write(f"  command: {' '.join(first)}\n")
    else:
        stream.write(f"  command: {' '.join(req.cmd)}\n")
    stream.write("  see output above for details\n")
    stream.write("press Enter to return to the ccw menu...\n")
    stream.flush()
    try:
        sys.stdin.readline()
    except Exception:
        pass

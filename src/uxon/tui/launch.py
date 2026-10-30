"""Launch-handoff helpers.

These run OUTSIDE the textual App — between an ``App.exit()`` and the
next ``App()`` re-enter. They execute the :class:`LaunchRequest` the
TUI emitted and hold the terminal open so the user can read stderr on
failure. No blessed / no textual imports at module level.
"""

from __future__ import annotations

import sys
from typing import Any

from uxon.domain.launch_request import LaunchRequest

#: Threshold below which an rc=0 launch is treated as a silent fast-exit.
FAST_EXIT_THRESHOLD_SEC = 1.0


def pause_on_launch_failure(
    stream: Any, req: LaunchRequest, rc: int, stage: str, wall_seconds: float
) -> None:
    """Hold the terminal after a failed launch so the user can read stderr.

    Plain-text — no blessed escape codes. Managed launches retain failed panes
    inside tmux, so a fast successful attach is not reclassified as an agent
    failure. Unmanaged fast exits still receive an explicit diagnostic.
    """
    fast_zero = (
        req.managed is None
        and req.handoff is None
        and rc == 0
        and wall_seconds < FAST_EXIT_THRESHOLD_SEC
    )
    if rc == 130:  # user Ctrl-C
        return
    if rc == 0 and not fast_zero:
        return
    label = req.label or "launch"
    stream.write("\n")
    if fast_zero:
        stream.write(
            f"uxon: {label} exited immediately (rc=0 in {wall_seconds:.2f}s, stage={stage})\n"
        )
    else:
        stream.write(f"uxon: {label} failed (rc={rc}, stage={stage})\n")
    if stage == "prelaunch":
        first = list(req.prelaunch[0]) if req.prelaunch else []
        stream.write(f"  command: {' '.join(first)}\n")
    else:
        stream.write(f"  command: {' '.join(req.cmd)}\n")
    if fast_zero:
        stream.write("  no diagnostic output was retained\n")
    else:
        stream.write("  command output, if any, was written directly above\n")
    stream.write("press Enter to return to the uxon menu...\n")
    stream.flush()
    try:
        sys.stdin.readline()
    except Exception:
        pass

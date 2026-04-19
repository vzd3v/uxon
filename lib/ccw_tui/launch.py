"""Launch-handoff helpers.

These run OUTSIDE the fullscreen TUI context — between a TUI app exit
and the next app re-enter. They execute the ``LaunchRequest`` the TUI
emitted, hold the terminal open so the user can read stderr on failure,
and drain any buffered keystrokes before the main screen re-renders.
"""

from __future__ import annotations

import subprocess
import sys
import time as _time
from typing import TYPE_CHECKING

from .context import LaunchRequest

if TYPE_CHECKING:
    from blessed import Terminal  # noqa: F401 — legacy type-only import


#: Threshold below which an rc=0 launch is treated as a silent fast-exit.
#: Empirically a healthy tmux attach that actually landed the user in a
#: claude session will be in the foreground for at least a few seconds;
#: anything sub-second that returns rc=0 is almost certainly a broken
#: command (missing binary, bad argv) that printed to stderr and exited
#: before the user could read it.
FAST_EXIT_THRESHOLD_SEC = 1.0


def _run_launch_request(req: "LaunchRequest") -> tuple[int, str, float]:
    """Execute a LaunchRequest via fork-and-wait (TUI context already exited).

    Runs each prelaunch command in order, aborting if any returns non-zero,
    then runs the main ``cmd``. Returns ``(rc, stage, wall_seconds)`` where
    ``stage`` is ``"prelaunch"`` if a prelaunch failed, else ``"cmd"``, and
    ``wall_seconds`` is the total elapsed time across prelaunch + cmd.
    Wall time is used by the caller to detect silent fast-exit launches
    (rc=0 but sub-second duration) that would otherwise strip the user
    of any context about why tmux didn't stick.
    """
    t0 = _time.monotonic()
    for pre in req.prelaunch:
        rc = subprocess.call(list(pre))
        if rc != 0:
            return rc, "prelaunch", _time.monotonic() - t0
    rc = subprocess.call(list(req.cmd))
    return rc, "cmd", _time.monotonic() - t0


def _format_launch_status(t: "Terminal", req: "LaunchRequest", rc: int, stage: str) -> str:
    """Render a short status-line message about a returned-from-tmux launch.

    Kept for backward compatibility with the legacy blessed runner. The
    textual migration formats status via ``self.notify`` / plain text.
    """
    from ccw_tui_widgets import dim as _dim_widget  # local to avoid hard dep

    label = req.label or "launch"
    if stage == "prelaunch":
        return t.red(f"{label}: prelaunch failed (rc={rc})")
    if rc == 0:
        return ""
    if rc == 130:
        return _dim_widget(t, f"{label}: cancelled")
    return t.yellow(f"{label}: exited rc={rc}")


def _drain_stdin(t: "Terminal", max_keys: int = 64) -> int:
    """Read-and-discard any buffered keystrokes on the TTY.

    Called after a launch round-trip returns and before the TUI re-enters
    fullscreen. blessed's ``t.cbreak()`` does not flush pending bytes on
    entry, so keys typed while tmux was running (or during the split
    second after ``_pause_on_launch_failure``) would otherwise be
    consumed by the next screen's ``t.inkey()`` — re-animating a stale
    cursor. Bounded at ``max_keys`` to dodge a pathological "stdin is a
    pipe of infinite bytes" scenario.

    Returns the number of keys drained (for testability / logging).
    """
    drained = 0
    try:
        with t.cbreak():
            while drained < max_keys:
                key = t.inkey(timeout=0)
                if not key:
                    break
                drained += 1
    except Exception:
        # Drain is best-effort. A broken tty must not crash the TUI.
        return drained
    return drained


def _pause_on_launch_failure(
    t: "Terminal",
    req: "LaunchRequest",
    rc: int,
    stage: str,
    wall_seconds: "float | None" = None,
) -> None:
    """Hold the terminal after a failed launch so the user can read stderr.

    Called after the fullscreen TUI context has exited and before we
    re-enter it. The failed subprocess's stderr is still on the physical
    terminal at this point; without a pause, re-entering fullscreen wipes
    it. We print a clear banner pointing at the output above and wait for
    a keypress. ``rc == 130`` (user Ctrl-C'd) is not treated as a failure.

    When ``wall_seconds`` is provided and the launch returned rc=0 in
    under :data:`FAST_EXIT_THRESHOLD_SEC`, also pause — a near-instant
    zero exit is almost always a silent launch failure (e.g. claude
    binary missing, bad tmux argv) and the user deserves to see any
    output that was printed before fullscreen wipes it.
    """
    from ccw_tui_widgets import dim as _dim_widget  # local to avoid hard dep

    fast_zero = (
        rc == 0
        and wall_seconds is not None
        and wall_seconds < FAST_EXIT_THRESHOLD_SEC
    )
    if rc == 130:
        return
    if rc == 0 and not fast_zero:
        return
    label = req.label or "launch"
    sys.stdout.write("\n")
    if fast_zero:
        sys.stdout.write(
            t.bold_yellow(
                f"ccw: {label} exited immediately (rc=0 in {wall_seconds:.2f}s, stage={stage})"
            )
            + "\n"
        )
    else:
        sys.stdout.write(t.bold_red(f"ccw: {label} failed (rc={rc}, stage={stage})") + "\n")
    if stage == "prelaunch":
        sys.stdout.write(
            _dim_widget(t, f"  command: {' '.join(list(req.prelaunch[0]) if req.prelaunch else [])}") + "\n"
        )
    else:
        sys.stdout.write(_dim_widget(t, f"  command: {' '.join(req.cmd)}") + "\n")
    sys.stdout.write(_dim_widget(t, "  see output above for details") + "\n")
    sys.stdout.write(t.bold("press any key to return to the ccw menu...") + "\n")
    sys.stdout.flush()
    with t.cbreak():
        t.inkey(timeout=None)

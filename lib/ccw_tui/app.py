"""Textual app shell for the ccw TUI.

:class:`CcwApp` is a thin shell that, on T5, mounts a placeholder main
screen. Subsequent tasks (T6/T7*) replace the placeholder with the
real :class:`MainScreen`.

The outer :func:`run` loop is the non-textual controller. It creates a
:class:`CcwApp`, waits for it to exit (either via quit binding or
:meth:`CcwApp.request_launch`), and — on launch intent — executes the
requested subprocess outside the textual render loop before creating a
fresh app instance. This is the ``exit()``-based TTY handoff pattern
described in the migration plan.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from textual.app import App
from textual.binding import Binding

from .context import CallbackError, LaunchRequest, TuiContext
from .events import _log_event
from .hints import TEXTUAL_MISSING_HINT
from .launch import _run_launch_request
from .screens.main import MainScreen


class CcwApp(App):
    """ccw interactive shell.

    Attributes set by bindings / screens and read by the outer loop:
      ``pending_launch`` — a :class:`LaunchRequest` when the app is
        exiting because a screen asked for a TTY handoff.
      ``quit_rc`` — integer exit code when the user quit the app.
      ``pending_status`` — error message from a prior round (typically
        ``on_refresh`` failure), displayed as a toast on mount.
    """

    CSS_PATH = "styles.tcss"

    # CcwApp has no per-app bindings — quit/help etc. live on the
    # MainScreen so its Footer displays them; delegating to screens
    # keeps the ``Footer`` widget single-source-of-truth (T18 drift
    # guard depends on this).
    BINDINGS = []

    def __init__(self, ctx: TuiContext, pending_status: str = "") -> None:
        super().__init__()
        self.ctx = ctx
        self.pending_launch: LaunchRequest | None = None
        self.quit_rc: int | None = None
        self.pending_status = pending_status

    def on_mount(self) -> None:
        # Push the main screen as the first and only base screen.
        self.push_screen(MainScreen(self.ctx))
        if self.pending_status:
            # T0a confirmed: a notify() raised on mount survives the app
            # re-create cycle when the outer loop stashes the message.
            self.notify(self.pending_status, severity="error", timeout=6)
        self.pending_status = ""

    # ── Public protocol: screens call this to hand off TTY ──────────

    def request_launch(self, req: LaunchRequest) -> None:
        """Schedule a launch. The outer loop picks up ``pending_launch``.

        Debounce double-scheduling — if a binding handler already called
        ``exit()`` on a prior frame, a second activation during the
        close-out window is a no-op.
        """
        if self.pending_launch is not None:
            return
        self.pop_until_main()
        self.pending_launch = req
        self.exit()

    def pop_until_main(self) -> None:
        """Dismiss every modal above the main screen.

        Modals are ``ModalScreen`` instances pushed onto the screen
        stack; we call ``pop_screen`` until only the base screen
        remains. Safe to call even when no modal is present.
        """
        while len(self.screen_stack) > 1:
            try:
                self.pop_screen()
            except Exception:  # pragma: no cover — belt-and-braces
                break


# ── Outer run loop ──────────────────────────────────────────────────


def run(ctx: TuiContext) -> int:
    """Run the interactive ccw TUI.

    Creates a :class:`CcwApp`, waits for it to exit, and on every
    launch-triggered exit runs the requested subprocess and re-creates
    the app with a refreshed context. On ``CallbackError`` from
    ``on_refresh`` the error is stashed in ``pending_status`` and
    surfaces as a toast when the next app instance mounts.
    """
    try:
        import textual  # noqa: F401 — presence check
    except ImportError:
        print(TEXTUAL_MISSING_HINT, file=sys.stderr)
        return 1

    caller_user = os.environ.get("SUDO_USER") or os.environ.get("USER", "")
    _log_event(
        "tui_start",
        caller_user=caller_user,
        launch_user=ctx.current_user,
        extra={"version": ctx.version, "has_sudo": ctx.has_sudo},
    )

    pending_status: str = ""
    while True:
        app = CcwApp(ctx, pending_status=pending_status)
        app.run()

        if app.quit_rc is not None:
            _log_event(
                "tui_quit",
                caller_user=caller_user,
                launch_user=ctx.current_user,
                outcome=f"rc={app.quit_rc}",
            )
            return app.quit_rc

        req = app.pending_launch
        if req is None:
            # Defensive: App exited without setting quit_rc or launch —
            # treat as a clean quit.
            return 0
        _log_event(
            "launch",
            caller_user=caller_user,
            launch_user=ctx.current_user,
            extra={"label": req.label, "cmd": list(req.cmd)[:2]},
        )
        sys.stdout.flush()
        rc, stage, wall_seconds = _run_launch_request(req)
        _log_event(
            "launch_completed",
            caller_user=caller_user,
            launch_user=ctx.current_user,
            outcome=f"rc={rc}",
            extra={"label": req.label, "stage": stage, "wall_seconds": round(wall_seconds, 3)},
        )
        _pause_on_launch_failure_plain(sys.stdout, req, rc, stage, wall_seconds)
        try:
            ctx = ctx.on_refresh()
            pending_status = ""
        except CallbackError as exc:
            pending_status = f"Refresh failed: {exc}"


def _pause_on_launch_failure_plain(
    stream: Any, req: LaunchRequest, rc: int, stage: str, wall_seconds: float
) -> None:
    """Plain-text variant of the blessed-era pause banner.

    Called after a launch round-trip when the TUI has exited. Prints to
    ``stream`` (normally ``sys.stdout``) without any blessed escape
    codes — textual is not active here. Stays silent on rc=0 unless the
    launch returned in under :data:`FAST_EXIT_THRESHOLD_SEC` (almost
    certainly a silent failure worth pausing to show stderr).
    """
    from .launch import FAST_EXIT_THRESHOLD_SEC

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

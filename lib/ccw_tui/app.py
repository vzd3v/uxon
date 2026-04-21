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
from textual.message import Message

from .context import CallbackError, LaunchRequest, TuiContext
from .events import _log_event
from .hints import TEXTUAL_MISSING_HINT
from .launch import _run_launch_request, pause_on_launch_failure
from .screens.agents_unavailable import AgentsUnavailableScreen
from .screens.main import MainScreen


class _AgentAvailabilityUpdated(Message):
    """Posted (app-level) when the background probe finishes or updates.

    ``bubble = False`` is critical: the app-level handler re-posts this
    message to the active top screen so open modals can refresh, but if
    the message bubbled back up from the screen to the app, the app
    would re-dispatch again, creating an infinite loop (observed as
    a visibly flashing agent list with the selection resetting each
    tick).
    """

    bubble = False


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
        # Latch: AgentsUnavailableScreen is pushed at most once per app
        # instance. ``run()``'s outer loop re-creates the app after every
        # launch, which is the right cadence to re-arm the popup. The
        # in-session ``r`` refresh does NOT re-run the probe (it only
        # rebuilds ctx), so it intentionally does not re-arm either —
        # the popup copy tells the user to quit and restart.
        self._agents_popup_shown: bool = False

    def on_mount(self) -> None:
        # Push the main screen as the first and only base screen.
        self.push_screen(MainScreen(self.ctx))
        if self.pending_status:
            # T0a confirmed: a notify() raised on mount survives the app
            # re-create cycle when the outer loop stashes the message.
            self.notify(self.pending_status, severity="error", timeout=6)
        self.pending_status = ""
        # Kick off background agent availability probe.
        if self.ctx.enabled_agents:
            self.run_worker(self._probe_agents_worker, thread=True, exclusive=True)

    def _probe_agents_worker(self) -> None:
        """Background thread: probe each enabled agent's binary --version."""
        import ccw_agents
        result = ccw_agents.probe_agents(
            list(self.ctx.enabled_agents),
            launch_user=self.ctx.launch_user or None,
        )
        for aid, avail in result.items():
            self.ctx.agent_availability[aid] = avail
        self.post_message(_AgentAvailabilityUpdated())

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

    def on__agent_availability_updated(self, event: _AgentAvailabilityUpdated) -> None:
        """Gate: on every probe update, decide whether to pop the install hint.

        Re-dispatches to the top screen so an active modal (e.g.
        LaunchOptionsScreen) can refresh its visible-agents list. We post
        a *fresh* message instance because textual marks the original as
        handled once this method returns; re-posting the same object is
        a silent no-op.
        """
        # Re-dispatch to the active top screen — app-level messages do
        # not bubble down by default. Skip when the top is already the
        # unavailable-agents popup (re-dispatching to ourselves is a
        # no-op) or missing; otherwise an active modal like
        # LaunchOptionsScreen gets a chance to refresh.
        top = self.screen_stack[-1] if self.screen_stack else None
        if (
            top is not None
            and top is not self
            and not isinstance(top, AgentsUnavailableScreen)
        ):
            top.post_message(_AgentAvailabilityUpdated())

        if self._agents_popup_shown:
            return
        enabled = self.ctx.enabled_agents
        if not enabled:
            return
        avail = self.ctx.agent_availability
        # Require every enabled agent to have a resolved status.
        resolved = all(
            aid in avail and getattr(avail[aid], "status", "pending") != "pending"
            for aid in enabled
        )
        if not resolved:
            return
        all_unusable = all(
            getattr(avail[aid], "status", None) in ("missing", "timeout")
            for aid in enabled
        )
        if not all_unusable:
            return
        self._agents_popup_shown = True
        self.push_screen(AgentsUnavailableScreen(tuple(enabled)))

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
        pause_on_launch_failure(sys.stdout, req, rc, stage, wall_seconds)
        try:
            ctx = ctx.on_refresh()
            pending_status = ""
        except CallbackError as exc:
            pending_status = f"Refresh failed: {exc}"



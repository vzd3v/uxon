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
from .state import should_show_agents_unavailable, should_start_agent_probe


class _AgentAvailabilityUpdated(Message):
    """Posted by the background probe worker when its dict update lands.

    Handled only at the app level (:meth:`CcwApp.on__agent_availability_updated`).
    Modals that need to refresh are invoked via ``call_later`` — no
    re-posting of this message. Re-posting to screens caused the message
    to bubble back up to the app and trigger a second dispatch, observed
    as an infinitely-flashing agent list with the selection resetting
    each tick.
    """

    bubble = False


class _LinkHealthUpdated(Message):
    """Posted by the background SSH-path probe worker when status changes."""

    bubble = False

    def __init__(self, status: Any) -> None:
        super().__init__()
        self.status = status


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
    BINDINGS = [
        Binding("1", "main_digit_jump(1)", "", show=False, priority=True),
        Binding("2", "main_digit_jump(2)", "", show=False, priority=True),
        Binding("3", "main_digit_jump(3)", "", show=False, priority=True),
        Binding("4", "main_digit_jump(4)", "", show=False, priority=True),
        Binding("5", "main_digit_jump(5)", "", show=False, priority=True),
        Binding("6", "main_digit_jump(6)", "", show=False, priority=True),
        Binding("7", "main_digit_jump(7)", "", show=False, priority=True),
        Binding("8", "main_digit_jump(8)", "", show=False, priority=True),
        Binding("9", "main_digit_jump(9)", "", show=False, priority=True),
    ]

    def __init__(
        self,
        ctx: TuiContext,
        pending_status: str = "",
        *,
        probe_agents: bool = True,
    ) -> None:
        super().__init__()
        self.ctx = ctx
        self.pending_launch: LaunchRequest | None = None
        self.quit_rc: int | None = None
        self.pending_status = pending_status
        self.probe_agents = probe_agents
        self._link_health_probe_running = False
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
        if should_start_agent_probe(
            probe_agents=self.probe_agents,
            enabled_agents=self.ctx.enabled_agents,
        ):
            self.run_worker(self._probe_agents_worker, thread=True, exclusive=True)
        self.set_timer(2.0, self._kick_link_health_probe)
        self.set_interval(
            max(15.0, self.ctx.tui_refresh_interval_seconds * 5),
            self._kick_link_health_probe,
        )

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

    def _kick_link_health_probe(self) -> None:
        if self._link_health_probe_running:
            return
        self._link_health_probe_running = True
        self.run_worker(self._probe_link_health_worker, thread=True, exclusive=False)

    def _probe_link_health_worker(self) -> None:
        import ccw_tui

        try:
            probe = getattr(self.ctx, "on_probe_link_health", None)
            status = probe() if callable(probe) else None
        except Exception as exc:  # pragma: no cover — defensive
            status = ccw_tui.LinkHealthStatus(
                state="error",
                summary=str(exc).strip() or exc.__class__.__name__,
            )
        finally:
            self._link_health_probe_running = False
        if status is None:
            status = ccw_tui.LinkHealthStatus()
        self.post_message(_LinkHealthUpdated(status))

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

        Also directly kicks the active modal (if it's the one that reads
        availability state — :class:`LaunchOptionsScreen`) so its
        ``(checking…)`` labels resolve. We intentionally do NOT post a
        second message at the screen here: re-posting caused the message
        to bubble back up to the app and trigger a second dispatch,
        flashing the list.
        """
        from .screens.launch_options import LaunchOptionsScreen

        top = self.screen_stack[-1] if self.screen_stack else None
        if isinstance(top, LaunchOptionsScreen):
            # call_later schedules the coroutine on the event loop and
            # does not go through the message-pump / bubbling path.
            self.call_later(top._rebuild_agent_list)

        if not should_show_agents_unavailable(
            enabled_agents=self.ctx.enabled_agents,
            availability=self.ctx.agent_availability,
            already_shown=self._agents_popup_shown,
        ):
            return
        self._agents_popup_shown = True
        self.push_screen(AgentsUnavailableScreen(tuple(self.ctx.enabled_agents)))

    def on__link_health_updated(self, event: _LinkHealthUpdated) -> None:
        self.ctx.link_health_status = event.status
        top = self.screen_stack[-1] if self.screen_stack else None
        if isinstance(top, MainScreen):
            top.ctx.link_health_status = event.status
            self.call_later(top._update_status_line)

    def action_main_digit_jump(self, n: int) -> None:
        top = self.screen_stack[-1] if self.screen_stack else None
        if isinstance(top, MainScreen):
            top.action_digit_jump(n)

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
        if sys.stdout.isatty():
            sys.stdout.write(
                "\rCcwApp | New session in current folder | Create new project | Open existing project\r"
            )
            sys.stdout.flush()
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

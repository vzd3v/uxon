"""Textual app shell for the uxon TUI.

:class:`UxonApp` is a thin shell that, on T5, mounts a placeholder main
screen. Subsequent tasks (T6/T7*) replace the placeholder with the
real :class:`MainScreen`.

The outer :func:`run` loop is the non-textual controller. It creates a
:class:`UxonApp`, waits for it to exit (either via quit binding or
:meth:`UxonApp.request_launch`), and — on launch intent — executes the
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
from .state import (
    compute_all_missing,
    should_push_agents_unavailable,
    should_start_agent_probe,
)


class _AgentAvailabilityUpdated(Message):
    """Posted by the background probe worker when its dict update lands.

    Handled only at the app level (:meth:`UxonApp.on__agent_availability_updated`).
    Modals that need to refresh are invoked via ``call_later`` — no
    re-posting of this message. Re-posting to screens caused the message
    to bubble back up to the app and trigger a second dispatch, observed
    as an infinitely-flashing agent list with the selection resetting
    each tick.

    Kept for backward compatibility with existing tests that synthesise
    this message; the worker now posts :class:`_HostReportUpdated` and
    derives the same dispatch from there.
    """

    bubble = False


class _HostReportUpdated(Message):
    """Posted by ``_probe_host_worker`` once a fresh :class:`HostReport` lands.

    Carries no payload — the worker mutates ``ctx.agent_availability`` and
    ``ctx.detected_agents`` in place before posting (mirroring the
    pre-existing pattern for ``_AgentAvailabilityUpdated``).
    """

    bubble = False


class _LinkHealthUpdated(Message):
    """Posted by the background SSH-path probe worker when status changes."""

    bubble = False

    def __init__(self, status: Any) -> None:
        super().__init__()
        self.status = status


class _CwdWritableUpdated(Message):
    """Posted by the cwd-write probe worker when the result lands."""

    bubble = False

    def __init__(self, writable: bool) -> None:
        super().__init__()
        self.writable = writable


class _MainCtxLoaded(Message):
    """Posted by the initial-load worker once on_refresh() returns the real ctx.

    Applied via :meth:`MainScreen.apply_loaded_ctx`. The skeleton ctx flips
    to ``loading=True``; this message hands in the loaded ctx (loading=False)
    and the screen patches itself in place or swaps for a fresh MainScreen
    when the layout changed.
    """

    bubble = False

    def __init__(self, ctx: TuiContext | None, error: str = "") -> None:
        super().__init__()
        self.ctx = ctx
        self.error = error


class UxonApp(App):
    """uxon interactive shell.

    Attributes set by bindings / screens and read by the outer loop:
      ``pending_launch`` — a :class:`LaunchRequest` when the app is
        exiting because a screen asked for a TTY handoff.
      ``quit_rc`` — integer exit code when the user quit the app.
      ``pending_status`` — error message from a prior round (typically
        ``on_refresh`` failure), displayed as a toast on mount.
    """

    CSS_PATH = "styles.tcss"

    # UxonApp has no per-app bindings — quit/help etc. live on the
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
        # Mirror of ``_link_health_probe_running`` for the host probe so
        # the periodic tick (and the manual ``r`` keybinding via
        # ``kick_refresh``) can re-run ``probe_host`` without piling up
        # concurrent workers.
        self._host_probe_running = False
        # Single in-flight latch for ctx refresh. Periodic timer, manual
        # ``r`` and the post-skeleton initial load all funnel through
        # :meth:`kick_refresh`; if a worker is still running we drop the
        # next request rather than queueing — workers are idempotent and
        # the next tick will catch up.
        self._refresh_in_flight = False
        # Transition gate: ``AgentsUnavailableScreen`` is pushed only on
        # the (False|None) → True transition of the "all enabled agents
        # are missing" predicate. ``None`` means we have not seen a probe
        # result yet. We deliberately do not auto-pop the modal when the
        # state recovers — see ``should_push_agents_unavailable`` in
        # ``state.py`` for the rationale.
        self._last_all_missing: bool | None = None

    def on_mount(self) -> None:
        # Push the main screen as the first and only base screen.
        self.push_screen(MainScreen(self.ctx))
        # MainScreen sits on the stack immediately after push_screen, so a
        # digit press received during mount is dispatched directly via
        # ``action_main_digit_jump`` — no pending-flush gymnastics needed.
        if self.pending_status:
            # T0a confirmed: a notify() raised on mount survives the app
            # re-create cycle when the outer loop stashes the message.
            self.notify(self.pending_status, severity="error", timeout=6)
        self.pending_status = ""
        # If the caller handed us a skeleton ctx, populate it asynchronously
        # — keeps the first frame fast and the event loop unblocked.
        if self.ctx.loading:
            self.kick_refresh()
        # Kick off background host probe (tmux + all known agents).
        if should_start_agent_probe(
            probe_agents=self.probe_agents,
            enabled_agents=self.ctx.enabled_agents,
        ):
            self._kick_host_probe()
        # Kick off cwd-write probe when the synchronous path didn't
        # already resolve it (cross-user case: uxon left cwd_writable=None
        # because the check would shell out via sudo).
        if self.ctx.cwd_writable is None:
            self.run_worker(self._probe_cwd_writable_worker, thread=True, exclusive=False)
        timers_enabled = not self.is_headless and "PYTEST_CURRENT_TEST" not in os.environ
        if timers_enabled:
            self.set_interval(
                self.ctx.tui_refresh_interval_seconds,
                self.kick_refresh,
            )
            self.set_timer(self.ctx.tui_ssh_refresh_interval_seconds, self._kick_link_health_probe)
            self.set_interval(
                self.ctx.tui_ssh_refresh_interval_seconds,
                self._kick_link_health_probe,
            )
            # Re-run the host probe on each ctx-refresh tick so freshly
            # installed tmux/agents surface without restarting uxon.
            # Same cadence as ``kick_refresh`` keeps the two streams in
            # lockstep — banner state and agent_availability advance
            # together.
            if should_start_agent_probe(
                probe_agents=self.probe_agents,
                enabled_agents=self.ctx.enabled_agents,
            ):
                self.set_interval(
                    self.ctx.tui_refresh_interval_seconds,
                    self._kick_host_probe,
                )

    def kick_refresh(self) -> None:
        """Schedule a worker that calls ``on_refresh`` and posts the result.

        Used by the periodic timer, the ``r`` keybinding and the initial
        load after a skeleton mount. No-op when a worker is already in
        flight — workers are idempotent and the next periodic tick will
        catch up. Result is delivered as a :class:`_MainCtxLoaded`
        message; :meth:`on__main_ctx_loaded` clears the flag and applies.
        """
        if self._refresh_in_flight:
            return
        self._refresh_in_flight = True
        self.run_worker(self._refresh_worker, thread=True, exclusive=False)

    def _refresh_worker(self) -> None:
        """Background thread: rebuild the TuiContext via ctx.on_refresh()."""
        try:
            new_ctx = self.ctx.on_refresh()
        except CallbackError as exc:
            self.post_message(_MainCtxLoaded(None, error=str(exc)))
            return
        except Exception as exc:  # pragma: no cover — defensive
            self.post_message(_MainCtxLoaded(None, error=str(exc) or exc.__class__.__name__))
            return
        self.post_message(_MainCtxLoaded(new_ctx))

    def on__main_ctx_loaded(self, event: _MainCtxLoaded) -> None:
        self._refresh_in_flight = False
        if event.error:
            self.notify(f"Refresh failed: {event.error}", severity="error", timeout=6)
            return
        if event.ctx is None:
            return
        top = self.screen_stack[-1] if self.screen_stack else None
        if isinstance(top, MainScreen):
            top.apply_loaded_ctx(event.ctx)

    def _kick_host_probe(self) -> None:
        """Schedule the host probe iff one isn't already in flight.

        Wired into ``on_mount`` (initial), the periodic refresh interval
        (so freshly-installed binaries surface without restart) and
        ``MainScreen.action_refresh`` (so the manual ``r`` keybinding
        does the same).
        """
        if self._host_probe_running:
            return
        self._host_probe_running = True
        self.run_worker(self._probe_host_worker, thread=True, exclusive=True)

    def _probe_host_worker(self) -> None:
        """Background thread: probe tmux + all known agent binaries.

        Uses ``probes.probe_host`` rather than the older per-agent
        ``probe_agents`` driver so that one ``sh -lc`` round-trip covers
        every binary, and so detected-but-not-enabled agents surface in
        ``ctx.detected_agents`` for the suggestion banner.

        ``BinaryStatus`` (path-based) is mapped to ``AgentAvailability``
        (status-based) for backward compatibility with existing
        consumers like ``LaunchOptionsScreen``.
        """
        from uxon import agents as uxon_agents
        from uxon import probes as uxon_probes

        # Build a minimal cfg-shaped object with what probe_host needs.
        class _Cfg:
            enabled_agents = self.ctx.enabled_agents

        # Default to the current OS user when ``launch_user`` is unset so
        # ``probe_host`` runs the local (no-sudo) code path. Falling back
        # to ``ctx.current_user`` would force a sudo wrap whenever the
        # caller fed us a placeholder value (test fixtures, skeleton
        # contexts), which is both slower and prone to false-negatives.
        target_user = self.ctx.launch_user or uxon_probes._current_user()

        try:
            report = uxon_probes.probe_host(_Cfg(), target_user)
        except Exception:  # pragma: no cover — defensive
            self._host_probe_running = False
            self.post_message(_HostReportUpdated())
            return

        # Map BinaryStatus → AgentAvailability for enabled agents.
        for aid, status in report.enabled.items():
            if status.path is not None:
                self.ctx.agent_availability[aid] = uxon_agents.AgentAvailability(
                    status="ok",
                    path=status.path,
                )
            else:
                self.ctx.agent_availability[aid] = uxon_agents.AgentAvailability(
                    status="missing",
                    error=f"{status.name} not found on PATH",
                )

        # Update detected agents (installed but not enabled).
        self.ctx.detected_agents.clear()
        self.ctx.detected_agents.update(report.detected)

        self._host_probe_running = False
        self.post_message(_HostReportUpdated())

    def _probe_cwd_writable_worker(self) -> None:
        """Background thread: probe write access and post the result."""
        try:
            writable = bool(self.ctx.on_probe_cwd_writable())
        except CallbackError:
            writable = False
        except Exception:  # pragma: no cover — defensive
            writable = False
        self.post_message(_CwdWritableUpdated(writable))

    def on__cwd_writable_updated(self, event: _CwdWritableUpdated) -> None:
        self.ctx.cwd_writable = event.writable
        top = self.screen_stack[-1] if self.screen_stack else None
        if isinstance(top, MainScreen):
            top.ctx.cwd_writable = event.writable
            self.call_later(top._refresh_cwd_row)

    def _kick_link_health_probe(self) -> None:
        if self._link_health_probe_running:
            return
        self._link_health_probe_running = True
        self.run_worker(self._probe_link_health_worker, thread=True, exclusive=False)

    def _probe_link_health_worker(self) -> None:
        from uxon import tui as uxon_tui

        try:
            probe = getattr(self.ctx, "on_probe_link_health", None)
            status = probe() if callable(probe) else None
        except Exception as exc:  # pragma: no cover — defensive
            status = uxon_tui.LinkHealthStatus(
                state="error",
                summary=str(exc).strip() or exc.__class__.__name__,
            )
        finally:
            self._link_health_probe_running = False
        if status is None:
            status = uxon_tui.LinkHealthStatus()
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

    def _dispatch_availability_change(self) -> None:
        """Common dispatch shared between ``_AgentAvailabilityUpdated`` and
        ``_HostReportUpdated``: refresh the active modal if it consumes
        availability, then run the transition-based gate for
        ``AgentsUnavailableScreen``.
        """
        from .screens.launch_options import LaunchOptionsScreen

        top = self.screen_stack[-1] if self.screen_stack else None
        if isinstance(top, LaunchOptionsScreen):
            # call_later schedules the coroutine on the event loop and
            # does not go through the message-pump / bubbling path.
            self.call_later(top._rebuild_agent_list)
        # Refresh the detected-agents banner on the main screen if we
        # have one — does nothing when the screen has not mounted yet.
        main = next((s for s in self.screen_stack if isinstance(s, MainScreen)), None)
        if main is not None:
            self.call_later(main._refresh_detected_banner)

        current_all_missing = compute_all_missing(
            enabled_agents=self.ctx.enabled_agents,
            availability=self.ctx.agent_availability,
        )
        modal_on_stack = any(isinstance(s, AgentsUnavailableScreen) for s in self.screen_stack)
        push = should_push_agents_unavailable(
            last_all_missing=self._last_all_missing,
            current_all_missing=current_all_missing,
            modal_already_on_stack=modal_on_stack,
            pending_launch=self.pending_launch is not None,
        )
        if push:
            self.push_screen(AgentsUnavailableScreen(tuple(self.ctx.enabled_agents)))
        # Update the transition tracker only after we have observed at
        # least one resolved availability set; ``compute_all_missing``
        # returns False both for "all ok" and for "still pending" — we
        # don't want a single pending tick to clear ``_last_all_missing``
        # back to False and re-arm a push when the next tick lands.
        if self._availability_resolved():
            self._last_all_missing = current_all_missing

    def _availability_resolved(self) -> bool:
        """True iff every enabled agent has a non-pending availability entry."""
        if not self.ctx.enabled_agents:
            return False
        return all(
            aid in self.ctx.agent_availability
            and getattr(self.ctx.agent_availability[aid], "status", "pending") != "pending"
            for aid in self.ctx.enabled_agents
        )

    def on__agent_availability_updated(self, event: _AgentAvailabilityUpdated) -> None:
        """Backward-compatible handler. Dispatches via the shared path."""
        self._dispatch_availability_change()

    def on__host_report_updated(self, event: _HostReportUpdated) -> None:
        """Handler for the new probe_host worker. Same dispatch path."""
        self._dispatch_availability_change()

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
    """Run the interactive uxon TUI.

    Creates a :class:`UxonApp`, waits for it to exit, and on every
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
                "\ruxon | New session in current folder | Create new project | Open existing project\r"
            )
            sys.stdout.flush()
        app = UxonApp(ctx, pending_status=pending_status)
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

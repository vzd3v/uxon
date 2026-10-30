"""Outer non-textual controller loop for the uxon TUI.

:func:`run` is the controller that lives *outside* the Textual render loop.
It creates a :class:`uxon.tui.app.UxonApp`, waits for it to exit (either via
the quit binding or :meth:`UxonApp.request_launch`), and — on a launch intent
— executes the requested subprocess outside the textual render loop before
creating a fresh app instance. This is the ``exit()``-based TTY handoff
runner boundary described by the package architecture
(``request_launch`` → ``exit()`` → runner handoff).

The ``import textual`` presence check reports repair guidance for incomplete
installations without loading the dependency in non-TUI commands.
"""

from __future__ import annotations

import os
import select
import sys
import threading
import time
from collections.abc import Callable
from functools import partial

from uxon.app.session_handoff import run_launch_request
from uxon.infra.events import debug as _debug
from uxon.infra.terminal_title import set_dashboard_title

from .context import CallbackError, TuiContext
from .hints import TEXTUAL_MISSING_HINT
from .launch import pause_on_launch_failure


class _TerminalDisconnectWatcher:
    """Notify the TUI when its controlling terminal disappears.

    Textual's Linux input driver currently keeps selecting a PTY after a
    hangup, which can leave its input thread spinning after an SSH session
    dies. The outer runner owns the terminal lifetime, so it watches the
    terminal descriptor independently and asks the app to exit on hangup.

    A wake pipe lets normal app shutdown stop the blocking ``poll`` without
    a timer or background scanning.
    """

    _DISCONNECT_EVENTS = select.POLLHUP | select.POLLERR | select.POLLNVAL

    def __init__(self, terminal_fd: int, on_disconnect: Callable[[], object]) -> None:
        self._terminal_fd = terminal_fd
        self._on_disconnect = on_disconnect
        self._wake_read_fd, self._wake_write_fd = os.pipe()
        self._thread = threading.Thread(
            target=self._watch,
            name="uxon-terminal-lifetime",
            daemon=True,
        )
        self._closed = False

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.write(self._wake_write_fd, b"\0")
        except OSError:
            pass
        self._thread.join()
        os.close(self._wake_read_fd)
        os.close(self._wake_write_fd)

    def _watch(self) -> None:
        poller = select.poll()
        poller.register(self._terminal_fd, self._DISCONNECT_EVENTS)
        poller.register(self._wake_read_fd, select.POLLIN)

        for fd, events in poller.poll():
            if fd == self._wake_read_fd:
                return
            if fd == self._terminal_fd and events & self._DISCONNECT_EVENTS:
                self._on_disconnect()
                return


def _run_app_with_terminal_lifetime(
    run_app: Callable[[], object],
    request_exit: Callable[[], object],
    terminal_fd: int,
) -> None:
    """Run one TUI instance and bind its lifetime to ``terminal_fd``."""
    watcher = _TerminalDisconnectWatcher(terminal_fd, request_exit)
    watcher.start()
    try:
        run_app()
    finally:
        watcher.close()


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
    except ImportError as error:
        print(f"{TEXTUAL_MISSING_HINT}\n  Import error: {error}", file=sys.stderr)
        return 1

    # Root invariant: from here on this process runs an event loop. Any
    # blocking subprocess on the loop thread (the keystroke-swallowing
    # bug class) now raises at the spawn site instead of degrading the
    # UI silently. Launch handoff (``run_launch_request``) runs between
    # app instances with no loop on the thread, so it is unaffected.
    from uxon.infra.loop_guard import install_subprocess_guard

    install_subprocess_guard()

    # Record stdin read categories, byte counts and timing without retaining
    # input contents. Enabled only under ``UXON_DEBUG=keys``.
    from .keylog import install_stdin_tap

    install_stdin_tap()

    from .app import UxonApp

    caller_user = os.environ.get("SUDO_USER") or os.environ.get("USER", "")
    from uxon.infra import audit as _audit

    _audit.audit("tui.open")

    pending_status: str = ""
    while True:
        set_dashboard_title(
            sys.stdout, enabled=ctx.tmux_set_terminal_title, term=os.environ.get("TERM", "")
        )
        if sys.stdout.isatty():
            sys.stdout.write("\ruxon | Open project | Create new project | Open in this folder\r")
            sys.stdout.flush()
        app = UxonApp(ctx, pending_status=pending_status)
        _run_app_with_terminal_lifetime(
            app.run,
            partial(app.call_later, app.exit),
            sys.stdin.fileno(),
        )

        if app.quit_rc is not None:
            _debug("tui", reason=f"rc={app.quit_rc}")
            return app.quit_rc

        req = app.pending_launch
        if req is None:
            # Defensive: App exited without setting quit_rc or launch —
            # treat as a clean quit.
            return 0
        from uxon.domain.launch_request import session_name_from_launch_label

        _session = session_name_from_launch_label(req.label)
        # Managed launch preparation emits ``session.new``. This optional
        # debug record describes the dashboard's launch selection.
        _debug(
            "launch",
            caller_user=caller_user,
            launch_user=ctx.current_user,
            label=req.label,
            session=_session,
            session_id=req.identity.session_id if req.identity else "",
            launch_nonce=req.identity.launch_nonce
            if req.identity
            else req.managed.record_nonce
            if req.managed
            else "",
            cmd=list(req.cmd)[:2],
        )
        sys.stdout.flush()
        _t0 = time.monotonic()
        try:
            result = run_launch_request(req)
            rc, stage, wall_seconds = result.rc, result.stage, result.wall_seconds
        except Exception as exc:
            # ``Exception`` (not ``BaseException``): a KeyboardInterrupt or
            # SystemExit propagating up here is a user-driven interruption,
            # not an error in the launched subprocess. The outcome alphabet
            # has no "cancelled" label, so leave those uncaught
            # rather than mislabel them as ``outcome="error"``.
            _audit.audit(
                "session.ended",
                outcome="error",
                session=_session,
                session_id=req.identity.session_id if req.identity else "",
                launch_nonce=req.identity.launch_nonce
                if req.identity
                else req.managed.record_nonce
                if req.managed
                else "",
                target_user=req.identity.user
                if req.identity
                else req.managed.launch_user
                if req.managed
                else "",
                execution_backend=req.identity.execution_backend
                if req.identity
                else req.managed.execution_backend
                if req.managed
                else "",
                **({"correlation_id": req.correlation_id} if req.correlation_id else {}),
                **({"peer_name": req.peer_name} if req.peer_name else {}),
                rc=-1,
                wall_seconds=round(time.monotonic() - _t0, 3),
                error=str(exc)[:256],
            )
            raise
        pause_on_launch_failure(sys.stdout, req, rc, stage, wall_seconds)
        try:
            ctx = ctx.on_refresh()
            pending_status = result.warning
        except CallbackError as exc:
            pending_status = "; ".join(filter(None, (result.warning, f"Refresh failed: {exc}")))

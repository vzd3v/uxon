"""Outer non-textual controller loop for the uxon TUI.

:func:`run` is the controller that lives *outside* the Textual render loop.
It creates a :class:`uxon.tui.app.UxonApp`, waits for it to exit (either via
the quit binding or :meth:`UxonApp.request_launch`), and — on a launch intent
— executes the requested subprocess outside the textual render loop before
creating a fresh app instance. This is the ``exit()``-based TTY handoff
pattern described in the migration plan
(``request_launch`` → ``exit()`` → runner handoff).

Extracted from ``app.py`` in Phase P7. The ``import textual`` presence check
stays here (it is the optional-textual UX guard, sanctioned).
"""

from __future__ import annotations

import os
import select
import sys
import threading
import time
from collections.abc import Callable
from functools import partial

from uxon.infra.events import debug as _debug

from .context import CallbackError, TuiContext
from .hints import TEXTUAL_MISSING_HINT
from .launch import _run_launch_request, pause_on_launch_failure


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
    except ImportError:
        print(TEXTUAL_MISSING_HINT, file=sys.stderr)
        return 1

    # Root invariant: from here on this process runs an event loop. Any
    # blocking subprocess on the loop thread (the keystroke-swallowing
    # bug class) now raises at the spawn site instead of degrading the
    # UI silently. Launch handoff (``_run_launch_request``) runs between
    # app instances with no loop on the thread, so it is unaffected.
    from uxon.infra.loop_guard import install_subprocess_guard

    install_subprocess_guard()

    # Lowest in-process keystroke witness: log every stdin read (payload,
    # byte count, gap since the previous read) so a swallowed key is a
    # provable gap in the log instead of unobservable silence. Installs
    # only under ``UXON_DEBUG=keys``; no-op (and zero cost) otherwise.
    from .keylog import install_stdin_tap

    install_stdin_tap()

    from .app import UxonApp

    caller_user = os.environ.get("SUDO_USER") or os.environ.get("USER", "")
    from uxon.infra import audit as _audit

    _audit.audit("tui.open")

    pending_status: str = ""
    while True:
        if sys.stdout.isatty():
            sys.stdout.write(
                "\ruxon | New session in current folder | Create new project | Open existing project\r"
            )
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
        # Audit-channel ``session.new`` is emitted by the per-callback
        # sites in ``cli.py::on_launch_*``; here we keep only the
        # developer-facing ``debug`` record (off by default,
        # ``UXON_DEBUG=tui`` opts in) so the dev-only fields (stage / cmd
        # head / label) survive the migration to journald.
        _debug(
            "launch",
            caller_user=caller_user,
            launch_user=ctx.current_user,
            label=req.label,
            cmd=list(req.cmd)[:2],
        )
        sys.stdout.flush()
        from uxon.domain.launch_request import session_name_from_launch_label

        _session = session_name_from_launch_label(req.label)
        _t0 = time.monotonic()
        try:
            rc, stage, wall_seconds = _run_launch_request(req)
        except Exception as exc:
            # ``Exception`` (not ``BaseException``): a KeyboardInterrupt or
            # SystemExit propagating up here is a user-driven interruption,
            # not an error in the launched subprocess.  Spec's outcome
            # alphabet has no "cancelled" label, so leave those uncaught
            # rather than mislabel them as ``outcome="error"``.
            _audit.audit(
                "session.ended",
                outcome="error",
                session=_session,
                rc=-1,
                wall_seconds=round(time.monotonic() - _t0, 3),
                error=str(exc)[:256],
            )
            raise
        _audit.audit(
            "session.ended",
            outcome="ok" if rc == 0 else "error",
            session=_session,
            rc=rc,
            wall_seconds=round(wall_seconds, 3),
        )
        pause_on_launch_failure(sys.stdout, req, rc, stage, wall_seconds)
        try:
            ctx = ctx.on_refresh()
            pending_status = ""
        except CallbackError as exc:
            pending_status = f"Refresh failed: {exc}"

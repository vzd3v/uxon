# SPDX-License-Identifier: MIT
"""Root invariant: blocking work must never run on the TUI event-loop thread.

The whole class of "swallowed keystrokes / frozen modals / hung launch"
bugs has a single cause: a synchronous, slow call (``subprocess`` →
``tmux`` / ``git`` / ``ssh`` / ``sudo``) executed on the asyncio thread
that Textual uses to read the keyboard. While that call runs, the loop
cannot service input, and Textual's escape-sequence parser starves —
arrow keys (multi-byte ``ESC [ A`` sequences) mis-parse or drop, and a
bare ``ESC`` needs several presses to register.

Rather than chase each offending call site (whack-a-mole — a new one
reappears with the next feature), this module makes the *class*
impossible to ship silently. It is the same pattern Django uses to ban
blocking ORM calls from async contexts
(``SynchronousOnlyOperation``): a guard installed once at the spawn
boundary that turns the bug into a loud, located failure.

How the discriminator works
---------------------------
``asyncio.get_running_loop()`` succeeds **only** on a thread that is
currently running an event loop. The Textual app runs its loop on the
main thread; every worker spawned via ``run_worker(thread=True)`` runs
on a plain thread with *no* running loop; a non-TUI CLI invocation has
no loop at all. So:

  - call from the event-loop thread  → loop found → **raise**
  - call from a worker thread        → no loop    → allowed
  - call from a plain CLI process    → no loop    → allowed

That is exactly the line we want to enforce: blocking is fine in a
worker, forbidden on the loop.

Scope & escape hatch
--------------------
:func:`install_subprocess_guard` patches ``subprocess.Popen.__init__``
for the current process only (the TUI process installs it at startup;
``subprocess.run`` / ``call`` / ``check_output`` all funnel through
``Popen``, so one install point covers every external command,
including code added later and blocking calls inside third-party
libraries). Set ``UXON_DISABLE_LOOP_GUARD=1`` to disable in an
emergency. The guard is idempotent — installing twice is a no-op.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading

__all__ = [
    "EventLoopBlockedError",
    "assert_off_event_loop",
    "install_subprocess_guard",
    "guard_installed",
]


class EventLoopBlockedError(RuntimeError):
    """Raised when a blocking call is attempted on the event-loop thread.

    A bug, never an expected condition: the offending call must move
    into a worker (``run_worker(thread=True)`` /
    ``WorkerCoordinator.run_off_loop``). The message names the call so
    the stack trace points straight at the site.
    """


_install_lock = threading.Lock()
_GUARD_MARKER = "__uxon_loop_guard__"


def _guard_disabled() -> bool:
    return bool(os.environ.get("UXON_DISABLE_LOOP_GUARD"))


def assert_off_event_loop(what: str = "blocking call") -> None:
    """Raise :class:`EventLoopBlockedError` if called on the loop thread.

    No-op when there is no running event loop on the current thread
    (worker threads, plain CLI) or when the guard is disabled via
    ``UXON_DISABLE_LOOP_GUARD``. Cost is one ``get_running_loop`` call —
    negligible next to anything worth guarding.
    """
    if _guard_disabled():
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise EventLoopBlockedError(
        f"{what} attempted on the asyncio event-loop thread. "
        "Blocking work (subprocess/ssh/sudo/git) must run in a worker — "
        "see WorkerCoordinator.run_off_loop / run_worker(thread=True). "
        "(Set UXON_DISABLE_LOOP_GUARD=1 to bypass in an emergency.)"
    )


def guard_installed() -> bool:
    """True iff the subprocess guard is currently installed."""
    return getattr(subprocess.Popen.__init__, _GUARD_MARKER, False)


def install_subprocess_guard() -> None:
    """Make every ``subprocess`` spawn assert it is off the event loop.

    Idempotent and process-wide. Wraps ``subprocess.Popen.__init__`` so
    every ``subprocess.run`` / ``call`` / ``check_output`` / ``Popen``
    routes through :func:`assert_off_event_loop` before the child is
    spawned. Call once at TUI startup.
    """
    with _install_lock:
        if guard_installed():
            return
        original_init = subprocess.Popen.__init__

        def guarded_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            assert_off_event_loop("subprocess spawn")
            original_init(self, *args, **kwargs)

        setattr(guarded_init, _GUARD_MARKER, True)
        guarded_init.__wrapped__ = original_init  # introspection / unwrap
        subprocess.Popen.__init__ = guarded_init  # type: ignore[method-assign]

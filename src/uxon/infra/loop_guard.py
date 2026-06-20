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

Two assertions, no mutation
---------------------------
The guard is a **pure detector** — it observes a spawn and may raise, but
never rewrites its kwargs. At each ``Popen.__init__`` it checks:

1. **Off the event loop** (:func:`assert_off_event_loop`) — blocking work must
   not run on the loop thread; raises :class:`EventLoopBlockedError` there.
2. **Sanctioned spawn** (:func:`_check_spawn_sanctioned`) — every background
   spawn must carry the in-frame sanction marker set by
   :func:`uxon.infra.run.run_query` (Lane A) or :func:`handoff_spawn` (Lane B).
   A marker-absent raw ``subprocess`` is a child that could inherit, and flush,
   the TUI's controlling terminal. It logs once and — only under
   ``UXON_SPAWN_GUARD_STRICT`` — raises :class:`UnsanctionedSpawnError`. By
   default it never raises: the hard first-party enforcer is the AC8 grep test,
   so migrating a tree mid-suite (real-subprocess integration tests carry no
   marker) does not break.

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
import contextlib
import contextvars
import os
import subprocess
import threading
from collections.abc import Iterator

__all__ = [
    "EventLoopBlockedError",
    "UnsanctionedSpawnError",
    "assert_off_event_loop",
    "handoff_spawn",
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


class UnsanctionedSpawnError(RuntimeError):
    """A subprocess was spawned without the sanction marker (strict mode only).

    Raised at the spawn site only when ``UXON_SPAWN_GUARD_STRICT`` is set: a
    background command bypassed :func:`uxon.infra.run.run_query` (Lane A) and the
    interactive handoff bypassed :func:`handoff_spawn` (Lane B) — a raw
    ``subprocess`` that could inherit, and flush, the TUI's controlling terminal.

    Distinct from :class:`EventLoopBlockedError`: that one is about the *thread*
    the spawn runs on (the on-loop block); this one is about *how* the spawn was
    created (raw vs. sanctioned). Off by default — the hard first-party enforcer
    is the AC8 grep test, not a runtime raise that would trip every real-
    subprocess integration test in the suite.
    """


# ── Sanction marker: which spawns may bypass the raw-spawn check ─────────────
#
# A module-private ``ContextVar`` set by exactly two context managers, each of
# which marks *and spawns in the same frame on the same thread*:
#
#   * :func:`sanctioned_spawn` — Lane A, used **only** by ``run_query``;
#   * :func:`handoff_spawn`    — Lane B, used **only** at the post-``exit()``
#     launch handoff (``tui/launch.py``).
#
# There is deliberately no bare "set the marker" function. Because both entry
# points are context managers that reset on exit, you cannot set the marker on
# one thread and hand a ``Popen`` to another — the cross-thread footgun is
# structurally unwriteable, not merely discouraged. A ``ContextVar`` is not
# copied into ``run_worker(thread=True)`` pool threads, but it need not be: set
# and read are co-located by construction.
_spawn_sanctioned: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "uxon_spawn_sanctioned", default=False
)

# Env var that turns the marker-absent case from log-once into a hard raise.
# Unset by default (including in ``tests/conftest.py``) so migrating mid-suite
# never breaks the many real-subprocess tests.
_STRICT_ENV = "UXON_SPAWN_GUARD_STRICT"

# Log the first unsanctioned spawn only — a future raw probe should leave one
# breadcrumb, not one line per spawn for the rest of the process's life.
_unsanctioned_logged = False
_unsanctioned_lock = threading.Lock()


@contextlib.contextmanager
def _sanction() -> Iterator[None]:
    """Mark every subprocess spawned in this ``with`` block as sanctioned."""
    token = _spawn_sanctioned.set(True)
    try:
        yield
    finally:
        _spawn_sanctioned.reset(token)


def sanctioned_spawn() -> contextlib.AbstractContextManager[None]:
    """Lane A marker — **internal**; used only by ``uxon.infra.run.run_query``.

    Every other background spawn must route through ``run_query``, never this
    directly. Marks the spawns inside the block so the guard's raw-spawn check
    passes. Not part of the casual public surface (absent from ``__all__``).
    """
    return _sanction()


def handoff_spawn() -> contextlib.AbstractContextManager[None]:
    """Lane B marker — explicit controlling-terminal handoff to an interactive child.

    Used only at the launch/attach handoff (``tui/launch.py``), which runs after
    ``app.exit()`` with the Textual input-reader thread already joined, so no
    concurrent reader exists while the child holds the real terminal. The child
    *keeps* the controlling tty (that is the Lane-B requirement); the marker just
    makes that explicitness legible to the guard so it is not flagged as a raw
    bypass.
    """
    return _sanction()


def _first_token(popen_args: object) -> object:
    """The program name from a ``Popen`` ``args`` (argv[0], or the string form).

    Only the program, never the full argv: argv can carry secrets and these are
    breadcrumbs/diagnostics, not audit records (``vz-general-rules`` § Safety).
    """
    try:
        if isinstance(popen_args, (list, tuple)) and popen_args:
            return popen_args[0]
        return popen_args
    except Exception:
        return "<unknown>"


def _note_unsanctioned_spawn(popen_args: object) -> None:
    """Record the first marker-absent spawn once per process (debug channel)."""
    global _unsanctioned_logged
    with _unsanctioned_lock:
        if _unsanctioned_logged:
            return
        _unsanctioned_logged = True
    from uxon.infra import events

    events.debug(
        "loop_guard",
        event="unsanctioned_spawn",
        cmd=str(_first_token(popen_args)),
        hint="background spawns must go through uxon.infra.run.run_query",
    )


def _check_spawn_sanctioned(args: tuple, kwargs: dict) -> None:
    """Flag a raw spawn that bypassed the sanctioned seams. Never mutates kwargs.

    A sanctioned spawn (``run_query`` Lane A / ``handoff_spawn`` Lane B) sets the
    marker in the same frame, so it passes silently. A marker-absent spawn logs
    exactly once and — only under ``UXON_SPAWN_GUARD_STRICT`` — additionally
    raises :class:`UnsanctionedSpawnError`. The guard only ever *observes*; it
    does not rewrite ``start_new_session``/``stdin`` (that was the reverted
    heuristic). The hard first-party enforcer is the AC8 grep test.
    """
    if _spawn_sanctioned.get():
        return
    popen_args = args[0] if args else kwargs.get("args")
    _note_unsanctioned_spawn(popen_args)
    if os.environ.get(_STRICT_ENV):
        raise UnsanctionedSpawnError(
            f"raw subprocess spawn ({_first_token(popen_args)!r}) bypassed the "
            "sanctioned seam. Background commands must use "
            "uxon.infra.run.run_query (Lane A); the interactive launch handoff "
            f"uses loop_guard.handoff_spawn (Lane B). Unset {_STRICT_ENV} to "
            "downgrade this to a one-time log."
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
            _check_spawn_sanctioned(args, kwargs)
            original_init(self, *args, **kwargs)

        setattr(guarded_init, _GUARD_MARKER, True)
        guarded_init.__wrapped__ = original_init  # introspection / unwrap
        subprocess.Popen.__init__ = guarded_init  # type: ignore[method-assign]

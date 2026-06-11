"""Textual message envelopes posted by the TUI's background workers.

Pure-data carriers: each class is a thin :class:`textual.message.Message`
subclass holding the payload a worker thread hands back to the event loop.
No behaviour lives here — the on-loop handlers (``UxonApp.on__*``) and the
reducers in :mod:`uxon.tui.source_dispatch` interpret these. Extracted from
``app.py`` in Phase P7 so the worker payload schema is a single, importable,
behaviour-free surface that tests can synthesise without touching the App.

``Message`` is the only Textual symbol imported — unavoidable, and fine: this
is a tui module, not a pure-domain one.
"""

from __future__ import annotations

from typing import Any

from textual.message import Message

from .context import TuiContext


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

    Carries the locally-built availability dict; the on-loop handler
    folds the payload into the slot store via :func:`slot_state.apply`.
    On failure ``error`` is non-empty and the dict may be ``None``;
    the handler skips the slot apply but still triggers the
    availability-dispatch path so the UI re-renders with whatever
    state currently holds.

    ``availability`` defaulting to ``None`` is the "skip the slot
    apply" signal used by tests that mutate the slot directly and
    post a bare message to wake the handler.
    """

    bubble = False

    def __init__(
        self,
        availability: dict | None = None,
        error: str = "",
        elapsed_ms: int = 0,
    ) -> None:
        super().__init__()
        self.availability = availability
        self.error = error
        self.elapsed_ms = elapsed_ms


class _LinkHealthUpdated(Message):
    """Posted by the background SSH-path probe worker when status changes."""

    bubble = False

    def __init__(self, status: Any) -> None:
        super().__init__()
        self.status = status


class _CwdWritableUpdated(Message):
    """Posted by the cwd-write probe worker when the result lands.

    Carries ``cwd_at_start`` — the cwd value captured at probe
    launch time. The on-loop handler drops results whose
    ``cwd_at_start`` does not match the current ``state.main.cwd``,
    so an in-flight probe started against ``cwd_old`` is not
    attributed to ``cwd_new`` after a directory change.
    """

    bubble = False

    def __init__(self, writable: bool, *, cwd_at_start: str = "") -> None:
        super().__init__()
        self.writable = writable
        self.cwd_at_start = cwd_at_start


class _WorktreesProbed(Message):
    """Posted by the one-shot worktree probe worker (launch-screen open).

    Carries the resolved launchability flag + the probed ``workspaces``
    list + an optional ``error`` message plus the ``on_done`` callback the
    launch flow handed in. The on-loop handler invokes
    ``on_done(launchable, workspaces, error)`` so it can gate / push
    :class:`LaunchOptionsScreen` safely off the worker thread (§4.2 — both
    the launchability ``sudo`` probe and the git worktree probe run in the
    thread; the screen push runs on the loop). ``launchable`` is ``None``
    when the caller pre-resolved it (cwd's reactive slot) and asked for no
    probe. ``error`` is a non-empty string only when the git probe raised
    (e.g. ``git worktree list`` failed on a real repo) — it drives the
    WORKSPACE error row, distinct from the empty-list "not a git repo" hint.
    The callable travels on the message because it is an in-process closure
    on the screen, not serialised state.
    """

    bubble = False

    def __init__(
        self,
        launchable: bool | None,
        workspaces: list,
        on_done: Any,
        error: str | None = None,
    ) -> None:
        super().__init__()
        self.launchable = launchable
        self.workspaces = workspaces
        self.on_done = on_done
        self.error = error


class _OffLoopCallbackDone(Message):
    """Posted when a blocking interactive callback finishes in a worker.

    Carries the result (or the exception) of a callback that
    :meth:`WorkerCoordinator.run_off_loop` ran on a worker thread, plus
    the two on-loop continuations the caller supplied. The on-loop
    handler invokes ``on_success(value)`` or ``on_error(exc)`` — both run
    back on the event loop so they may safely ``notify`` / ``push_screen``
    / ``request_launch`` / ``action_refresh``.

    This is the generic carrier for the interactive blocking class
    (attach / kill / kill-all / remote-kill / existing-session probe):
    the blocking ``tmux`` / ``ssh`` / ``sudo`` call runs in the worker;
    only the cheap UI continuation runs on the loop. ``instance_epoch``
    gates stale results from a previous app instance, exactly like
    :class:`_RefreshSourceLanded`.
    """

    bubble = False

    def __init__(
        self,
        value: object,
        error: BaseException | None,
        on_success: Any,
        on_error: Any,
        *,
        instance_epoch: int = -1,
    ) -> None:
        super().__init__()
        self.value = value
        self.error = error
        self.on_success = on_success
        self.on_error = on_error
        self.instance_epoch = instance_epoch


class _MainCtxLoaded(Message):
    """Posted when the ``main_ctx_rebuild`` source returns a fresh ctx.

    Applied via :meth:`MainScreen.apply_loaded_ctx`. The screen patches
    itself in place or swaps for a fresh MainScreen when the layout
    changed. Dispatched from :class:`_RefreshSourceLanded` for the
    ``main_ctx_rebuild`` source.
    """

    bubble = False

    def __init__(self, ctx: TuiContext | None, error: str = "") -> None:
        super().__init__()
        self.ctx = ctx
        self.error = error


class _RefreshSourceLanded(Message):
    """Posted by every registered refresh source when its worker finishes.

    The handler dispatches on :attr:`name` to the per-source apply logic.
    Sources are fail-soft: ``error`` may be set and ``value`` may be
    ``None`` — the handler logs via ``UXON_DEBUG=refresh`` and otherwise
    leaves state untouched, so a transient source failure does not
    corrupt good data.

    ``instance_epoch`` carries the spawning :class:`UxonApp`'s
    monotonically-increasing epoch. The dispatcher drops events whose
    epoch does not match the current app's epoch, catching the race
    where a worker thread spawned by instance-N posts its result after
    the outer ``run()`` loop has already created instance-N+1 (e.g.
    after a TTY handoff). The default ``-1`` is a sentinel meaning
    "unstamped" — the dispatcher skips the epoch gate then, so tests
    that synthesise this message directly without an epoch keep working.
    """

    bubble = False

    def __init__(
        self,
        name: str,
        value: object,
        error: str = "",
        elapsed_ms: int = 0,
        *,
        instance_epoch: int = -1,
    ) -> None:
        super().__init__()
        self.name = name
        self.value = value
        self.error = error
        self.elapsed_ms = elapsed_ms
        self.instance_epoch = instance_epoch

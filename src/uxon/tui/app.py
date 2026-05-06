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
import time
from collections.abc import Callable
from typing import Any, ClassVar

from textual.app import App
from textual.binding import Binding
from textual.message import Message
from textual.reactive import reactive
from textual.worker import Worker, WorkerState

from .config import TuiConfig
from .context import CallbackError, LaunchRequest, TuiContext
from .events import debug as _debug
from .events import metrics_record
from .hints import TEXTUAL_MISSING_HINT
from .launch import _run_launch_request, pause_on_launch_failure
from .screens.agents_unavailable import AgentsUnavailableScreen
from .screens.main import MainScreen
from .state import (
    compute_all_missing,
    should_push_agents_unavailable,
)
from .tui_state import TuiState

_ACTIVE_STATES = (WorkerState.PENDING, WorkerState.RUNNING)


def _worker_active(w: Worker | None) -> bool:
    """True iff ``w`` is queued or running.

    Used as the in-flight gate for periodic kick-X helpers — derives
    from worker state rather than a separate bool, so a cancelled or
    crashed worker frees its slot automatically.
    """
    return w is not None and w.state in _ACTIVE_STATES


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

    Stage 8 commit 5b: carries the locally-built availability and
    detected dicts. The worker no longer mutates ``ctx.<field>`` from
    the thread; the on-loop handler folds the payload into the slot
    store via :func:`slot_state.apply`. Pre-5b semantics (no payload,
    in-place worker mutation) are gone — the message must always
    carry the dicts (or ``error`` when the probe failed).

    On failure ``error`` is non-empty and the dicts may be empty; the
    handler skips the slot apply but still triggers the
    availability-dispatch path so the UI re-renders with whatever
    state currently holds.
    """

    bubble = False

    def __init__(
        self,
        availability: dict | None = None,
        detected: dict | None = None,
        error: str = "",
        elapsed_ms: int = 0,
    ) -> None:
        super().__init__()
        # ``None`` (the default) marks a legacy bare-post pattern
        # used by tests that mutated ``ctx.agent_availability`` /
        # ``ctx.detected_agents`` directly and then posted to wake
        # the handler. The on-loop handler treats this case as
        # "skip the slot apply" (the slot already reflects the
        # mutation through the shim's read-through path) and falls
        # through to ``_dispatch_availability_change`` so the UI
        # still re-renders.
        self.availability = availability
        self.detected = detected
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

    Stage 8 commit 6: carries ``cwd_at_start`` — the cwd value
    captured at probe launch time. The on-loop handler drops
    results whose ``cwd_at_start`` does not match the current
    ``state.main.cwd`` (or, pre-commit-7, ``ctx.cwd``); this
    prevents an in-flight probe started against ``cwd_old`` from
    being attributed to ``cwd_new`` after a directory change.

    SlotResult-shaped fields stay inside the handler; the message
    envelope only carries the validated payload + the gate token.
    """

    bubble = False

    def __init__(self, writable: bool, *, cwd_at_start: str = "") -> None:
        super().__init__()
        self.writable = writable
        self.cwd_at_start = cwd_at_start


class _MainCtxLoaded(Message):
    """Posted when the ``main_ctx_rebuild`` source returns a fresh ctx.

    Applied via :meth:`MainScreen.apply_loaded_ctx`. The skeleton ctx flips
    to ``loading=True``; this message hands in the loaded ctx (loading=False)
    and the screen patches itself in place or swaps for a fresh MainScreen
    when the layout changed.

    Driven from :class:`_RefreshSourceLanded` for the ``main_ctx_rebuild``
    source, so the dispatch path matches every other registry source.
    Kept as a separate message for backward compatibility with tests that
    synthesise it directly.
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
    ``None`` — the handler is responsible for that case (typically: log
    via ``UXON_DEBUG=refresh`` and otherwise leave state untouched, so
    a transient source failure does not corrupt previously-good data).

    The ``instance_epoch`` field carries the spawning :class:`UxonApp`'s
    monotonically-increasing epoch (set in ``__init__``). The dispatcher
    drops events whose epoch does not match the current app's epoch —
    this catches the rare race where a worker thread spawned by an
    instance-N app posts its result after the outer ``run()`` loop has
    already created instance-N+1 (e.g. after a TTY handoff).

    The default ``-1`` is a sentinel meaning "unstamped" — the dispatcher
    skips the epoch gate when it sees the sentinel. This keeps existing
    tests that synthesise this message directly without specifying an
    epoch working unchanged. Production stamping is done in
    :meth:`UxonApp._source_worker`, which always passes the real
    ``self._instance_epoch``.
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

    # Stage 8 commit 11: writable reactive carrying the flattened
    # multi-host row tuple. The coalescer (``_mark_remote_rows_dirty``
    # / ``_drain_remote_rows``) collapses N back-to-back per-host
    # slot writes within one event-loop cycle into a single
    # ``select_remote_rows`` invocation. Plain assignment only — NO
    # ``compute_remote_rows`` method (textual/reactive.py:330-333
    # marks the descriptor read-only when ``hasattr(obj,
    # compute_name)`` holds, and any subsequent assignment raises).
    # The introspection regression test in
    # tests/test_uxon_tui_remote_table.py guards against this trap.
    remote_rows: reactive[tuple] = reactive(())

    # Process-wide monotonic counter feeding ``self._instance_epoch``.
    # Each ``UxonApp.__init__`` snapshots-and-increments this so a
    # worker spawned by instance N can be distinguished from one
    # belonging to instance N+1 after the outer ``run()`` loop
    # re-creates the app following a TTY handoff. Spec § Worker
    # lifetime: "every result carries a monotonically increasing
    # ``instance_epoch`` matched against the App's own epoch".
    _next_epoch: ClassVar[int] = 0

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
        # Snapshot the immutable side of ``ctx`` once at construction.
        # ``cfg`` is shared across rebuild ticks — ``on_refresh()``
        # produces a fresh ctx with new sessions / server_status, but
        # the callbacks, cadence knobs, remote-hosts registry and
        # refresh-source list are stable for the App's lifetime.
        # Screens / modals migrate to reading from ``self.cfg`` over
        # subsequent commits; for this commit ``cfg`` is duplicated
        # state populated alongside the live ctx.
        self.cfg: TuiConfig = TuiConfig.from_context(ctx)
        # Stage 8 commit 3: introduce the async-side state container.
        # Empty on construction — no slot is canonical yet (commits
        # 4–6b flip canonicality field-by-field). ``ctx._state`` is
        # linked here so ``ctx.refresh_tick`` already round-trips
        # through ``state.refresh_tick``; the canonical owner of the
        # counter still lives in the legacy
        # ``MainScreen.apply_loaded_ctx`` increment until commit 6b.
        self.state: TuiState = TuiState()
        # Stage 8 commit 5a: hoist the cli-built initial dicts into
        # state slots so the slot is canonical from this point
        # forward. The legacy ctx kwarg-stored dicts remain accessible
        # via the shim's fallback path for unit tests that build a
        # bare ctx without an App. ``dataclasses.replace`` produces a
        # new (frozen) :class:`SlotState` carrying the same dict
        # reference — worker-thread in-place mutations through
        # ``ctx.agent_availability[aid] = …`` will land on this dict.
        from dataclasses import replace as _replace

        self.state.agent_availability = _replace(
            self.state.agent_availability,
            value=dict(ctx.agent_availability),
        )
        self.state.detected_agents = _replace(
            self.state.detected_agents,
            value=dict(ctx.detected_agents),
        )
        self.ctx._state = self.state
        self.pending_launch: LaunchRequest | None = None
        self.quit_rc: int | None = None
        self.pending_status = pending_status
        self.probe_agents = probe_agents
        # Snapshot the process-wide counter, then bump it. Production
        # ``_source_worker`` stamps the live epoch on every result.
        # ``_RefreshSourceLanded.instance_epoch`` defaults to the
        # sentinel ``-1`` ("unstamped — skip the gate"); the dispatcher
        # treats a sentinel-tagged event as always-current so synthetic
        # test posts (which omit the kwarg) bypass the cross-instance
        # drop. Don't trust value alignment between the two: the gate
        # is the sentinel branch, not the integer compare.
        self._instance_epoch: int = UxonApp._next_epoch
        UxonApp._next_epoch += 1
        # Worker-handle in-flight gates (see :func:`_worker_active`).
        # Each kick also pins its worker to a dedicated group so an
        # ``exclusive=True`` call cancels only siblings, never workers
        # from another stream.
        #
        # Registry sources gate per-source: ``self._source_handles[name]``
        # holds the in-flight worker for that source so a slow source
        # never blocks a faster sibling's next tick.
        self._source_handles: dict[str, Worker | None] = {}
        self._host_probe_handle: Worker | None = None
        self._link_health_handle: Worker | None = None
        # Stage 10a — ``UXON_DEBUG=startup`` channel: latch to fire
        # ``first_data_landed`` exactly once per app instance. Subsequent
        # ``main_ctx_rebuild`` results are steady-state and not
        # interesting for time-to-first-paint diagnosis.
        self._first_data_landed_logged: bool = False
        # Source-landing dispatch registries (id → handler). Built
        # once per instance so unit tests can inspect them without
        # spinning a Pilot. See :meth:`_build_source_dispatch`.
        (
            self._source_dispatch_exact,
            self._source_dispatch_prefix,
        ) = self._build_source_dispatch()
        # Transition gate: ``AgentsUnavailableScreen`` is pushed only on
        # the (False|None) → True transition of the "all enabled agents
        # are missing" predicate. ``None`` means we have not seen a probe
        # result yet. We deliberately do not auto-pop the modal when the
        # state recovers — see ``should_push_agents_unavailable`` in
        # ``state.py`` for the rationale.
        self._last_all_missing: bool | None = None
        # Stage 8 commit 11: dirty-flag for the remote-rows coalescer.
        # Multiple per-host slot writes within the same event-loop
        # cycle flip the flag once; the drainer (scheduled via
        # ``call_after_refresh``) runs ``select_remote_rows`` and
        # assigns ``self.remote_rows`` exactly once per refresh
        # cycle. Independent of Textual's internal callable
        # deduplication for ``call_after_refresh``: the dirty flag
        # is the coalescing mechanism.
        self._remote_rows_dirty: bool = False
        # Stage 8 commit 11 followup: reset module-level selector
        # caches at every App-instance birth. The TTY-handoff loop
        # (``run()`` outside this class) creates a fresh App after
        # each launch; module-level caches in
        # :mod:`uxon.tui.state` would otherwise leak prior-instance
        # data into the new one. The keys typically differ, but the
        # reset makes the contract explicit and shifts any cache
        # invalidation cost to the cold path.
        from .state import _HOST_HEALTH_BADGE_CACHE, _REMOTE_ROWS_CACHE

        _REMOTE_ROWS_CACHE["key"] = None
        _REMOTE_ROWS_CACHE["value"] = ()
        _HOST_HEALTH_BADGE_CACHE.clear()

    def on_mount(self) -> None:
        # Stage 10a — ``UXON_DEBUG=startup`` channel: log mount entry
        # with a monotonic timestamp so the operator can compute
        # ``mount_started → first_paint`` (target ≤ 50 ms per spec)
        # and ``mount_started → first_data_landed`` for end-to-end
        # cold-start latency. ``time.monotonic()`` is the right clock
        # for diffs; wall-clock would jitter under NTP corrections.
        _debug("startup", at="mount_started", ts=time.monotonic())
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
        # If the caller handed us a skeleton ctx, populate it
        # asynchronously — keeps the first frame fast and the event
        # loop unblocked. ``_kick_initial_sources`` honours
        # ``SourceSpec.kick_on_mount`` so future one-shot or interval-only
        # sources can opt out of the initial fan-out.
        if self.ctx.loading:
            self._kick_initial_sources()
        # Kick off background host probe (tmux + all known agents).
        # Probe even with an empty ``enabled_agents`` so the
        # detected-agents banner can surface CATALOG agents that the
        # user has never enabled (e.g., fresh install with the default
        # config). The per-tick gate inside ``_kick_host_probe``
        # honours ``self.probe_agents`` (the test-mode kill switch).
        if self.probe_agents:
            self._kick_host_probe()
        # Kick off cwd-write probe when the synchronous path didn't
        # already resolve it (cross-user case: uxon left cwd_writable=None
        # because the check would shell out via sudo).
        if self.ctx.cwd_writable is None:
            cwd_at_start = self.ctx.cwd
            self.run_worker(
                lambda cwd=cwd_at_start: self._probe_cwd_writable_worker(cwd),
                thread=True,
                exclusive=False,
                group="cwd_writable",
            )
        timers_enabled = not self.is_headless and "PYTEST_CURRENT_TEST" not in os.environ
        if timers_enabled:
            # Per-source periodic timers, driven from the registry.
            # Each source advances independently so a slow source can't
            # stall the others — e.g. a future remote-host source over
            # SSH won't block the local-sessions stream.
            for spec in self.ctx.refresh_sources or ():
                # Precedence: explicit ``spec.cadence_seconds`` first
                # (per-source override, e.g. per-host
                # ``[[remote_hosts]].interval``). Fall back to the
                # named ctx attribute only when no explicit value is
                # supplied. Both ``None`` means "no periodic timer".
                cadence: float | int | None = spec.cadence_seconds
                if cadence is None:
                    cadence_attr = spec.cadence_seconds_attr
                    if cadence_attr is None:
                        continue
                    cadence = getattr(self.ctx, cadence_attr, None)
                if not isinstance(cadence, (int, float)) or cadence <= 0:
                    continue
                self.set_interval(
                    float(cadence),
                    lambda spec=spec: self._kick_source(spec),
                )
            self.set_timer(self.ctx.tui_ssh_refresh_interval_seconds, self._kick_link_health_probe)
            self.set_interval(
                self.ctx.tui_ssh_refresh_interval_seconds,
                self._kick_link_health_probe,
            )
            # Re-run the host probe on each ctx-refresh tick so freshly
            # installed tmux/agents surface without restarting uxon.
            # Same cadence as the local-sessions source keeps the two
            # streams in lockstep — banner state and agent_availability
            # advance together. Gated only on ``self.probe_agents`` so
            # the detection path keeps working when the user enables
            # their first agent at runtime via Settings.
            if self.probe_agents:
                self.set_interval(
                    self.ctx.tui_refresh_interval_seconds,
                    self._kick_host_probe,
                )

    def _kick_initial_sources(self) -> None:
        """Kick every refresh source whose ``kick_on_mount`` is True.

        Called once from :meth:`on_mount` when the ctx is a skeleton.
        Distinct from :meth:`kick_refresh` (which fans out
        unconditionally for the manual-refresh / steady-state path)
        so a future "lazy" source can opt out of the startup wave
        without affecting the user-visible ``r`` keybinding.
        """
        for spec in self.ctx.refresh_sources or ():
            if spec.kick_on_mount:
                self._kick_source(spec)

    def kick_refresh(self) -> None:
        """Fan out: kick every registered refresh source.

        Used by the ``r`` keybinding, the initial load after a skeleton
        mount, and any other "manual refresh" path. Each source is
        gated independently — a source whose worker is still in flight
        is skipped without affecting siblings. Per-source periodic
        timers (set up in :meth:`on_mount`) drive the steady-state
        cadence; this method is the synchronous fan-out for explicit
        refreshes.
        """
        sources = self.ctx.refresh_sources or ()
        if not sources:
            _debug("refresh", at="kick_refresh", action="skip", reason="no_sources")
            return
        _debug("refresh", at="kick_refresh", action="fanout", count=len(sources))
        for spec in sources:
            self._kick_source(spec)

    def _kick_source(self, spec: Any) -> None:
        """Schedule one source's fetcher in a worker if not already running.

        Worker group is namespaced ``refresh:<name>`` so an ``exclusive``
        call from one source can never cancel another source's worker
        (the same per-stream isolation that the 3.2.2 fix established).
        """
        handle = self._source_handles.get(spec.name)
        if _worker_active(handle):
            _debug("refresh", at="kick_source", source=spec.name, action="skip", reason="in_flight")
            return
        _debug("refresh", at="kick_source", source=spec.name, action="spawn")
        self._source_handles[spec.name] = self.run_worker(
            lambda spec=spec: self._source_worker(spec),
            thread=True,
            exclusive=False,
            group=f"refresh:{spec.name}",
        )

    def _source_worker(self, spec: Any) -> None:
        """Background thread: run a source's fetcher with fail-soft semantics.

        ``run_source`` never raises — exceptions are captured into the
        result. We post the outcome as :class:`_RefreshSourceLanded`;
        the handler dispatches on ``name``.
        """
        from .refresh import run_source

        result = run_source(spec)
        _debug(
            "refresh",
            at="source_worker",
            source=result.name,
            action="done" if result.error is None else "error",
            error=result.error or "",
            elapsed_ms=result.elapsed_ms,
        )
        # Stage 10b — opt-in ``UXON_METRICS=1`` JSONL. ``from_cache`` is
        # available on ``RemoteSnapshot`` (the remote-host path) but not
        # on ``TuiContext`` (the local rebuild path), so we read it
        # defensively. ``error or None`` normalises the empty-string
        # convention used inside ``SourceResult``.
        from_cache = bool(getattr(result.value, "from_cache", False))
        metrics_record(
            result.name,
            elapsed_ms=result.elapsed_ms,
            error=result.error or None,
            from_cache=from_cache,
        )
        self.post_message(
            _RefreshSourceLanded(
                name=result.name,
                value=result.value,
                error=result.error or "",
                elapsed_ms=result.elapsed_ms,
                instance_epoch=self._instance_epoch,
            )
        )

    # ── Source landing dispatch ─────────────────────────────────────
    #
    # Per stage 8 of the multi-host design spec, the result-landing
    # dispatch is data-driven: a registry maps source-id → handler so
    # adding a new asynchronous stream is a registry entry rather than
    # an ``if/elif`` ladder edit. Two registries are inspected in this
    # order:
    #
    # 1. ``_EXACT_HANDLERS``: ``dict[str, handler]`` — exact name match
    #    (``"main_ctx_rebuild"`` etc.). Most sources land here.
    # 2. ``_PREFIX_HANDLERS``: ordered ``list[(prefix, handler)]`` —
    #    fallback for families like ``"remote:<host>"`` where the
    #    handler peels the prefix off and routes by suffix.
    #
    # An unknown name falls through to a debug-log drop, exactly as
    # the legacy ladder did. The handlers are bound methods on
    # :class:`UxonApp` so they have access to ``self`` (state, screens,
    # the message pump). The registry is built once per instance in
    # :meth:`__init__` so it can be inspected by tests without going
    # through the Pilot harness.

    def _handle_main_ctx_rebuild(self, event: _RefreshSourceLanded) -> None:
        """Dispatch ``main_ctx_rebuild`` into the legacy :class:`_MainCtxLoaded`
        message so :meth:`on__main_ctx_loaded` stays the single render
        entry point. Many tests synthesise ``_MainCtxLoaded`` directly,
        so the legacy message must keep its semantics.

        Stage 8 commit 6b: ``state.refresh_tick`` is canonical. The
        rebuild-source dispatcher is the *only* writer; previously
        ``MainScreen.apply_loaded_ctx`` did
        ``new_ctx.refresh_tick = self.ctx.refresh_tick + 1`` via the
        shim, but that path is gone. Selectors that memoise on
        ``state.refresh_tick`` will cache-miss every tick by design
        (the counter advances always); the contract is that
        selectors key on the specific subfield they consume, not on
        whole-state identity.
        """
        ctx = event.value if isinstance(event.value, TuiContext) else None
        # Stage 10a — ``UXON_DEBUG=startup``: latch fires once per app
        # instance (per ``self._first_data_landed_logged`` reset in
        # ``__init__``). Subsequent rebuilds are steady-state ticks,
        # not interesting for cold-start latency diagnosis.
        if not self._first_data_landed_logged:
            self._first_data_landed_logged = True
            _debug(
                "startup",
                at="first_data_landed",
                source=event.name,
                ts=time.monotonic(),
            )
        # Advance the canonical tick on every rebuild landing. The
        # increment happens before the screen sees the ctx so any
        # selector or watcher that fires off ``apply_loaded_ctx``
        # reads the post-increment value.
        if event.error == "" and ctx is not None:
            from .main_data import MainData

            self.state.refresh_tick += 1
            # Stage 8 commit 7: ``state.main`` is now canonical. The
            # rebuild-source dispatcher is the single writer; we
            # snapshot the rebuild-derived fields off the incoming
            # ctx into a frozen :class:`MainData` and assign. The ctx
            # stays in the message envelope for screens not yet
            # ported (commit 8 flips MainScreen to read
            # ``state.main`` directly).
            self.state.main = MainData.from_context(ctx)
            # Drive the ``loading`` reactive on the main screen if
            # one is mounted. Plain reassignment — the reactive has
            # no compute method (verified by introspection in
            # tests), so ``__set__`` triggers ``_check_watchers``
            # without raising. ``state.main is None`` is the
            # structural loading sentinel; on first landing it
            # flips to False and the ``#sessions-note`` reactively
            # repaints.
            screen = next((s for s in self.screen_stack if isinstance(s, MainScreen)), None)
            if screen is not None and hasattr(screen, "_id"):
                screen.loading = self.state.main is None
        self.post_message(_MainCtxLoaded(ctx, error=event.error))

    def _handle_remote_snapshot(self, event: _RefreshSourceLanded) -> None:
        """Apply a remote-host snapshot via the slot store.

        Source name is ``remote:<host>``. The fetcher always returns a
        :class:`RemoteSnapshot` (the collector is fail-soft); we wrap
        it in a :class:`SlotResult` and fold it into
        ``state.remote[host]`` via the pure :func:`slot_state.apply`.

        Stage 8 commit 4: ``state.remote`` is now the canonical store
        for per-host snapshots. The shim ``ctx.remote_snapshots``
        property reads through ``state.remote`` so screens that have
        not yet been ported keep working unchanged. The fetcher
        already includes its own elapsed timing in
        :attr:`_RefreshSourceLanded.elapsed_ms`; we surface it on the
        slot's ring so the latency-p50 tooltip has data.
        """
        host_name = event.name[len("remote:") :]
        from uxon.remote_collector import RemoteSnapshot

        from .slot_state import SlotResult, SlotState
        from .slot_state import apply as apply_slot

        # Resolve attempted_at from the source result's timing
        # signature: ``time.time()`` at landing is close enough — the
        # fetcher itself doesn't emit a timestamp, and the wall-clock
        # at dispatch is what staleness logic compares against.
        attempted_at = time.time()

        snap = event.value if isinstance(event.value, RemoteSnapshot) else None
        if snap is None and not event.error:
            # Defensive: a remote source landed with neither a
            # RemoteSnapshot nor an error — drop without mutating.
            return

        from_cache = bool(getattr(snap, "from_cache", False)) if snap is not None else False
        result: SlotResult[RemoteSnapshot] = SlotResult(
            value=snap,
            error=event.error or None,
            elapsed_ms=event.elapsed_ms,
            attempted_at=attempted_at,
            from_cache=from_cache,
        )
        prev = self.state.remote.get(host_name) or SlotState[RemoteSnapshot]()
        self.state.remote[host_name] = apply_slot(prev, result)

        # Stage 8 commit 11: mark the remote-rows reactive dirty.
        # The drainer collapses multiple same-cycle slot writes into
        # a single selector invocation + a single per-host diff
        # against the previous tuple. The single-host section
        # header still refreshes imperatively (it depends on the
        # newly-landed scope flag, not on the row tuple itself).
        self._mark_remote_rows_dirty()
        top = self.screen_stack[-1] if self.screen_stack else None
        if top is not None and isinstance(top, MainScreen) and len(self.cfg.remote_hosts) == 1:
            top._refresh_remote_section_header(host_name)

    # ── Remote-rows coalescer ────────────────────────────────────────
    #
    # Stage 8 commit 11. Multiple per-host slot writes within the same
    # event-loop cycle would otherwise trigger N back-to-back
    # ``select_remote_rows`` invocations and N table-update fanouts.
    # The coalescer collapses them: the dirty flag flips on the first
    # write, ``call_after_refresh`` schedules a single drain at the
    # end of the cycle, the drain runs the selector and dispatches
    # the row tuple to the table once.

    def _mark_remote_rows_dirty(self) -> None:
        if self._remote_rows_dirty:
            return
        self._remote_rows_dirty = True
        # ``call_after_refresh`` runs the callback after the next
        # render cycle — i.e. after every per-host slot write that
        # happens in the current cycle has landed in ``state.remote``.
        # If the App is mid-mount and ``call_after_refresh`` is not
        # yet wired, fall back to ``call_later`` which still defers
        # to the event loop.
        try:
            self.call_after_refresh(self._drain_remote_rows)
        except Exception:  # pragma: no cover — defensive
            self.call_later(self._drain_remote_rows)

    def _drain_remote_rows(self) -> None:
        """Run :func:`select_remote_rows` once and dispatch the result.

        Reads ``self.state.remote`` and ``self.cfg.remote_hosts`` (both
        snapshot-frozen for the purposes of this drain), assigns the
        flattened tuple to ``self.remote_rows``, then dispatches a
        per-host diff to the active :class:`MainScreen`'s remote
        table. The dispatch (rather than data_bind) keeps the
        screen-side per-host ``update_host_rows`` optimisation
        introduced in commit 4 — only changed hosts trigger
        ``add_row`` / ``remove_row``.
        """
        if not self._remote_rows_dirty:
            return
        self._remote_rows_dirty = False
        from .state import select_remote_rows

        new_rows = select_remote_rows(self.state, self.cfg.remote_hosts)
        old_rows = self.remote_rows
        self.remote_rows = new_rows
        # Tuple equality short-circuits the assignment in Textual's
        # ``Reactive._set`` (current_value != value gate). When the
        # selector cache hits (id(slot.value) preserved by the
        # identity-stable apply), ``new_rows is old_rows`` so the
        # assignment is a no-op and we skip the screen dispatch too.
        if new_rows is old_rows:
            return
        screen = next((s for s in self.screen_stack if isinstance(s, MainScreen)), None)
        if screen is not None:
            screen._dispatch_remote_rows(old_rows, new_rows)

    def _build_source_dispatch(
        self,
    ) -> tuple[
        dict[str, Callable[[_RefreshSourceLanded], None]],
        list[tuple[str, Callable[[_RefreshSourceLanded], None]]],
    ]:
        """Construct the (exact, prefix) dispatch registries.

        Inspected by :meth:`on__refresh_source_landed` and by unit
        tests (no Pilot required). Adding a new source means adding
        an entry here in the same change as the source-spec
        registration.
        """
        exact: dict[str, Callable[[_RefreshSourceLanded], None]] = {
            "main_ctx_rebuild": self._handle_main_ctx_rebuild,
        }
        # Prefix matchers are scanned in order; the first match wins.
        prefix: list[tuple[str, Callable[[_RefreshSourceLanded], None]]] = [
            ("remote:", self._handle_remote_snapshot),
        ]
        return exact, prefix

    def on__refresh_source_landed(self, event: _RefreshSourceLanded) -> None:
        """Dispatch a source's result via the id → handler registry.

        Cross-instance gate: an event whose ``instance_epoch`` does not
        match ``self._instance_epoch`` is dropped — the worker that
        posted it belongs to a previous app instance whose result has
        no business mutating the current instance's state. Spec §
        Worker lifetime.

        Looks up ``event.name`` in ``_source_dispatch_exact`` first; on
        miss, scans ``_source_dispatch_prefix`` for the first prefix
        match. Unknown names are debug-logged and dropped — adding a
        new source means registering a handler in
        :meth:`_build_source_dispatch`.
        """
        # Sentinel ``-1`` = unstamped (synthetic test post). Production
        # workers always stamp ``self._instance_epoch``; a real event
        # with a different epoch indicates a worker spawned by a prior
        # app instance and is dropped.
        if event.instance_epoch != -1 and event.instance_epoch != self._instance_epoch:
            _debug(
                "refresh",
                at="source_landed",
                source=event.name,
                action="drop",
                reason="stale_instance_epoch",
                event_epoch=event.instance_epoch,
                app_epoch=self._instance_epoch,
            )
            return
        handler = self._source_dispatch_exact.get(event.name)
        if handler is not None:
            handler(event)
            return
        for prefix_str, prefix_handler in self._source_dispatch_prefix:
            if event.name.startswith(prefix_str):
                prefix_handler(event)
                return
        # Unknown source name — log and drop.
        _debug(
            "refresh",
            at="source_landed",
            source=event.name,
            action="drop",
            reason="no_handler",
        )

    def on__main_ctx_loaded(self, event: _MainCtxLoaded) -> None:
        top = self.screen_stack[-1] if self.screen_stack else None
        top_kind = type(top).__name__ if top else "None"
        _debug(
            "refresh",
            at="on_ctx_loaded",
            error=event.error or "",
            ctx_is_none=event.ctx is None,
            top=top_kind,
        )
        if event.error:
            self.notify(f"Refresh failed: {event.error}", severity="error", timeout=6)
            return
        if event.ctx is None:
            return
        if isinstance(top, MainScreen):
            top.apply_loaded_ctx(event.ctx)

    def _kick_host_probe(self) -> None:
        """Schedule the host probe iff one isn't already in flight.

        Wired into ``on_mount`` (initial), the periodic refresh interval
        (so freshly-installed binaries surface without restart) and
        ``MainScreen.action_refresh`` (so the manual ``r`` keybinding
        does the same). Honours ``self.probe_agents`` so tests that
        opt out of probing (Pilot tests with ``probe_agents=False``,
        the pty integration suite that stubs ``probes.probe_host``)
        do not start a real subprocess from the manual-refresh path.
        """
        if not self.probe_agents:
            return
        if _worker_active(self._host_probe_handle):
            return
        self._host_probe_handle = self.run_worker(
            self._probe_host_worker, thread=True, exclusive=True, group="host_probe"
        )

    def _probe_host_worker(self) -> None:
        """Background thread: probe tmux + all known agent binaries.

        Stage 8 commit 5b — race-free. The worker builds two local
        dicts (availability + detected) and posts them in a single
        :class:`_HostReportUpdated` message; the on-loop handler
        folds the payload into ``state.agent_availability`` /
        ``state.detected_agents`` via :func:`slot_state.apply`. No
        ``self.ctx.<field>`` is touched from the thread — the
        previous in-place mutation pattern was a latent data race
        between the worker and the event loop's selectors / screens.

        Uses ``probes.probe_host`` rather than the older per-agent
        ``probe_agents`` driver so one ``sh -lc`` round-trip covers
        every binary; detected-but-not-enabled agents surface in
        ``state.detected_agents.value`` for the suggestion banner.
        """
        import time as _time

        from uxon import agents as uxon_agents
        from uxon import probes as uxon_probes

        # Build a minimal cfg-shaped object with what probe_host needs.
        # Both fields are read off the frozen :class:`TuiConfig`
        # snapshot — no mutable ctx access from the worker thread.
        class _Cfg:
            enabled_agents = self.cfg.enabled_agents

        target_user = self.cfg.launch_user or uxon_probes._current_user()

        t0 = _time.monotonic()
        try:
            report = uxon_probes.probe_host(_Cfg(), target_user)
        except Exception as exc:  # pragma: no cover — defensive
            self.post_message(
                _HostReportUpdated(
                    error=str(exc) or exc.__class__.__name__,
                    elapsed_ms=int((_time.monotonic() - t0) * 1000),
                )
            )
            return

        availability: dict = {}
        for aid, status in report.enabled.items():
            if status.path is not None:
                availability[aid] = uxon_agents.AgentAvailability(
                    status="ok",
                    path=status.path,
                )
            else:
                availability[aid] = uxon_agents.AgentAvailability(
                    status="missing",
                    error=f"{status.name} not found on PATH",
                )
        detected = dict(report.detected)
        self.post_message(
            _HostReportUpdated(
                availability=availability,
                detected=detected,
                elapsed_ms=int((_time.monotonic() - t0) * 1000),
            )
        )

    def _probe_cwd_writable_worker(self, cwd_at_start: str = "") -> None:
        """Background thread: probe write access and post the result.

        Stage 8 commit 6: ``cwd_at_start`` is captured at the kick
        site (``on_mount`` / ``MainScreen``-driven re-probes) and
        threaded through the message envelope so the on-loop
        handler can gate against an in-flight probe whose result
        no longer applies to the current cwd.
        """
        try:
            writable = bool(self.cfg.on_probe_cwd_writable())
        except CallbackError:
            writable = False
        except Exception:  # pragma: no cover — defensive
            writable = False
        self.post_message(_CwdWritableUpdated(writable, cwd_at_start=cwd_at_start))

    def on__cwd_writable_updated(self, event: _CwdWritableUpdated) -> None:
        """Apply a cwd-write probe result to ``state.cwd_writable``.

        Stage 8 commit 6: drops results whose ``cwd_at_start`` no
        longer matches the live ``ctx.cwd``. Without this gate, an
        in-flight probe started against ``cwd_old`` could land
        after the user changed directory and surface as the answer
        for ``cwd_new`` — confusing and wrong. The mismatch case
        is logged via ``UXON_DEBUG=refresh`` and silently dropped
        so the next probe starts fresh.
        """
        from .slot_state import SlotResult
        from .slot_state import apply as apply_slot

        live_cwd = self.ctx.cwd
        if event.cwd_at_start and event.cwd_at_start != live_cwd:
            _debug(
                "refresh",
                at="cwd_writable_drop",
                reason="cwd_changed",
                cwd_at_start=event.cwd_at_start,
                live_cwd=live_cwd,
            )
            return
        result: SlotResult[bool | None] = SlotResult(
            value=bool(event.writable),
            error=None,
            elapsed_ms=0,
            attempted_at=time.time(),
        )
        self.state.cwd_writable = apply_slot(self.state.cwd_writable, result)
        top = self.screen_stack[-1] if self.screen_stack else None
        if isinstance(top, MainScreen):
            self.call_later(top._refresh_cwd_row)

    def _kick_link_health_probe(self) -> None:
        if _worker_active(self._link_health_handle):
            return
        self._link_health_handle = self.run_worker(
            self._probe_link_health_worker, thread=True, exclusive=False, group="link_health"
        )

    def _probe_link_health_worker(self) -> None:
        """Background thread: probe SSH-path health and post the result.

        Stage 8 commit 6 followup: reads ``on_probe_link_health`` off
        the frozen :class:`TuiConfig` (no mutable ctx access from the
        thread). ``apply_loaded_ctx`` may replace ``self.app.ctx``
        concurrently on the event loop, so reading ``self.ctx`` from
        the worker is a data race; the cfg snapshot is immutable for
        the App's lifetime.
        """
        from uxon import tui as uxon_tui

        try:
            probe = self.cfg.on_probe_link_health
            status = probe() if callable(probe) else None
        except Exception as exc:  # pragma: no cover — defensive
            status = uxon_tui.LinkHealthStatus(
                state="error",
                summary=str(exc).strip() or exc.__class__.__name__,
            )
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

        # Stage 8 commit 5d: read availability through the canonical
        # store directly. The shim ``ctx.agent_availability`` resolves
        # to the same value (state.<slot>.value) but going through it
        # forces an extra getattr layer; in the dispatch hot path
        # (every probe tick) the direct read is cheaper and pre-stages
        # the shim deletion in commit 10.
        availability = self.state.agent_availability.value or {}
        enabled = self.cfg.enabled_agents
        current_all_missing = compute_all_missing(
            enabled_agents=enabled,
            availability=availability,
        )
        modal_on_stack = any(isinstance(s, AgentsUnavailableScreen) for s in self.screen_stack)
        push = should_push_agents_unavailable(
            last_all_missing=self._last_all_missing,
            current_all_missing=current_all_missing,
            modal_already_on_stack=modal_on_stack,
            pending_launch=self.pending_launch is not None,
        )
        if push:
            self.push_screen(AgentsUnavailableScreen(tuple(enabled)))
        # Update the transition tracker only after we have observed at
        # least one resolved availability set; ``compute_all_missing``
        # returns False both for "all ok" and for "still pending" — we
        # don't want a single pending tick to clear ``_last_all_missing``
        # back to False and re-arm a push when the next tick lands.
        if self._availability_resolved():
            self._last_all_missing = current_all_missing

    def _availability_resolved(self) -> bool:
        """True iff every enabled agent has a non-pending availability entry.

        Stage 8 commit 5d: reads ``self.state.agent_availability.value``
        directly rather than going through the shim — same data, one
        less layer of indirection on a path that runs on every probe
        tick.
        """
        enabled = self.cfg.enabled_agents
        if not enabled:
            return False
        availability = self.state.agent_availability.value or {}
        return all(
            aid in availability and getattr(availability[aid], "status", "pending") != "pending"
            for aid in enabled
        )

    def on__agent_availability_updated(self, event: _AgentAvailabilityUpdated) -> None:
        """Backward-compatible handler. Dispatches via the shared path."""
        self._dispatch_availability_change()

    def on__host_report_updated(self, event: _HostReportUpdated) -> None:
        """Handler for the new probe_host worker.

        Stage 8 commit 5b: folds the worker's locally-built dicts
        into ``state.agent_availability`` / ``state.detected_agents``
        via :func:`slot_state.apply`. The dispatcher is the *only*
        on-loop site that mutates these slots, so any later observer
        (selectors, screens, the dispatch path below) sees a
        consistent fresh dict on each tick. The shim
        ``ctx.agent_availability`` returns ``state.<slot>.value``,
        i.e. the freshly-allocated dict — by-reference snapshots
        captured at modal-construction time go stale, which is
        why ``LaunchOptionsScreen`` is ported off snapshotting in
        commit 5c.
        """
        from .slot_state import SlotResult
        from .slot_state import apply as apply_slot

        # Apply the slot updates transactionally — either both slots
        # fold in or neither does. The two fields are coupled (same
        # probe produces both) and must not desync silently.
        # ``availability is None and detected is None`` is the legacy
        # bare-post pattern: tests mutate ``ctx.agent_availability``
        # directly and post to wake the handler. An asymmetric
        # payload (one None, the other not) is a programming error;
        # log and drop without partial mutation rather than silently
        # advancing one slot.
        avail_present = event.availability is not None
        detect_present = event.detected is not None
        if not event.error and avail_present and detect_present:
            attempted_at = time.time()
            avail_result: SlotResult[dict] = SlotResult(
                value=event.availability,
                error=None,
                elapsed_ms=event.elapsed_ms,
                attempted_at=attempted_at,
            )
            self.state.agent_availability = apply_slot(self.state.agent_availability, avail_result)
            detect_result: SlotResult[dict] = SlotResult(
                value=event.detected,
                error=None,
                elapsed_ms=event.elapsed_ms,
                attempted_at=attempted_at,
            )
            self.state.detected_agents = apply_slot(self.state.detected_agents, detect_result)
        elif avail_present != detect_present:
            _debug(
                "refresh",
                at="host_report_partial",
                action="drop",
                avail_present=avail_present,
                detect_present=detect_present,
            )
        self._dispatch_availability_change()

    def on__link_health_updated(self, event: _LinkHealthUpdated) -> None:
        """Apply a link-health probe result to ``state.link_health``.

        Stage 8 commit 6: ``link_health`` is now a slot. The handler
        runs on the event loop (the worker only posts the message),
        so no thread-race concerns — the migration is a data-shape
        rename so the carry-list can disappear.
        """
        from .slot_state import SlotResult
        from .slot_state import apply as apply_slot

        result: SlotResult = SlotResult(
            value=event.status,
            error=None,
            elapsed_ms=0,
            attempted_at=time.time(),
        )
        self.state.link_health = apply_slot(self.state.link_health, result)
        top = self.screen_stack[-1] if self.screen_stack else None
        if isinstance(top, MainScreen):
            self.call_later(top._update_status_line)

    def action_main_digit_jump(self, n: int) -> None:
        top = self.screen_stack[-1] if self.screen_stack else None
        if isinstance(top, MainScreen):
            top.action_digit_jump(n)

    # ── Worker drain on teardown ────────────────────────────────────

    def _drain_workers(self, *, grace_seconds: float = 0.1) -> None:
        """Cancel every tracked worker and wait briefly for completion.

        Spec § Worker lifetime: "Before App.run() returns, every
        in-flight worker is awaited (or hard-cancelled with a 100 ms
        grace). No worker thread survives the App instance that
        spawned it."

        Cancel-then-poll-until-grace is the simplest portable
        implementation: textual's :meth:`Worker.cancel` flips the
        state to ``CANCELLED`` for thread workers more or less
        immediately; for any straggler we sleep in 10 ms slices up
        to ``grace_seconds`` total so a slow shutdown doesn't make
        teardown synchronous on the worker.

        Called from :meth:`on_unmount`. Safe to call multiple times.
        """
        import time as _time

        # Collect every tracked handle into one flat list. ``run_worker``
        # may have produced a ``Worker`` for any of these slots.
        candidates: list[Worker] = []
        for w in self._source_handles.values():
            if w is not None:
                candidates.append(w)
        if self._host_probe_handle is not None:
            candidates.append(self._host_probe_handle)
        if self._link_health_handle is not None:
            candidates.append(self._link_health_handle)

        # cwd_writable workers were spawned without a handle; reach
        # into the worker manager group instead. ``self.workers``
        # exposes a :class:`WorkerManager` whose ``__iter__`` yields
        # every worker; filtering by group avoids touching workers
        # already covered above.
        try:
            for w in list(self.workers):
                if w.group == "cwd_writable":
                    candidates.append(w)
        except Exception:  # pragma: no cover — defensive
            pass

        cancelled = 0
        already_done = 0
        for w in candidates:
            if w.state in _ACTIVE_STATES:
                try:
                    w.cancel()
                    cancelled += 1
                except Exception:  # pragma: no cover — defensive
                    pass
            else:
                already_done += 1

        # Bounded polling — never block teardown more than the grace
        # window. 10 ms slices keep the wakeup cost negligible.
        deadline = _time.monotonic() + max(grace_seconds, 0.0)
        while _time.monotonic() < deadline:
            if not any(w.state in _ACTIVE_STATES for w in candidates):
                break
            _time.sleep(0.01)

        _debug(
            "refresh",
            at="drain",
            cancelled=cancelled,
            already_done=already_done,
            total=len(candidates),
            instance_epoch=self._instance_epoch,
        )

    def on_unmount(self) -> None:
        """Drain in-flight workers before the app loop returns.

        Textual fires ``Unmount`` from :meth:`App._shutdown` after
        ``_close_all`` / ``_close_messages``, so by the time we get
        here the message pump is already winding down — cancelling
        workers from this hook is exactly the "before App.run()
        returns" point the spec calls for.
        """
        self._drain_workers()

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
    from uxon import audit as _audit

    _audit.audit("tui.open")

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
        rc, stage, wall_seconds = _run_launch_request(req)
        _audit.audit(
            "session.ended",
            outcome="ok" if rc == 0 else "error",
            session=req.label,
            rc=rc,
            wall_seconds=round(wall_seconds, 3),
        )
        pause_on_launch_failure(sys.stdout, req, rc, stage, wall_seconds)
        try:
            ctx = ctx.on_refresh()
            pending_status = ""
        except CallbackError as exc:
            pending_status = f"Refresh failed: {exc}"

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

from textual import events as _events
from textual.app import App
from textual.binding import Binding
from textual.message import Message
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
    BINDINGS: ClassVar[list[Binding]] = []

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
        self.state: TuiState = TuiState()
        # Hoist the cli-built initial dicts into state slots so the
        # slot is canonical from construction. ``dataclasses.replace``
        # produces a new (frozen) :class:`SlotState` carrying the same
        # dict reference — worker-thread in-place mutations through
        # ``ctx.agent_availability[aid] = …`` land on this dict.
        from dataclasses import replace as _replace

        self.state.agent_availability = _replace(
            self.state.agent_availability,
            value=dict(ctx.agent_availability),
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
        # Latch so ``UXON_DEBUG=startup`` fires ``first_data_landed``
        # exactly once per app instance.
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
        # True once the host probe has produced *any* result (success
        # or error). Auto-mode uses this to gate the "no agents
        # installed" modal — an empty availability dict before the
        # probe lands is not "all missing", it is "not yet probed".
        # An errored probe still flips the flag so the modal can
        # surface the diagnostic instead of leaving the user staring
        # at a silently-empty agent list.
        self._host_probe_landed: bool = False
        # Last probe error (e.g. sudo failure). Empty on success.
        # Carried into :class:`AgentsUnavailableScreen` so the user
        # sees *why* nothing was probed rather than a generic "no
        # agents" message.
        self._host_probe_error: str = ""
        # Latest TuiContext from a successful ``main_ctx_rebuild`` landing.
        # The render scheduler reads it when firing a "main_ctx" dirty
        # batch into ``MainScreen.apply_loaded_ctx``. Stays ``None`` until
        # the first non-error rebuild lands.
        self._latest_ctx: TuiContext | None = None
        # Single locus for render-cadence decisions. All paths that
        # want a redraw call ``self._render.request(kind)``; the
        # scheduler coalesces and dispatches via ``_render_dirty``.
        from .render_scheduler import RenderScheduler

        self._render = RenderScheduler(
            self,
            debounce_ms=self.cfg.tui_render_debounce_ms,
            max_latency_ms=self.cfg.tui_render_max_latency_ms,
            render=self._render_dirty,
        )
        # Recompose-safe transient UI state for ``MainScreen``. Lives
        # here (not on the screen) because ``apply_loaded_ctx`` builds
        # a fresh screen on layout-signature flips, and three pieces
        # of state used to die with it: dashboard view/filter, host
        # tab index, and the tab-focus-restore flag. See
        # :class:`MainScreenUiState` for the rationale.
        from .dashboard.ui_state import DashboardUiState, MainScreenUiState

        self.main_ui = MainScreenUiState(
            ui=DashboardUiState(view_mode=ctx.tui_table_default_view),
        )

    def on_key(self, event: _events.Key) -> None:
        """Diagnostic log for keys that fall through unhandled.

        ``UXON_DEBUG=keys`` writes one record per key event that
        bubbles all the way up to the App without being consumed by a
        widget binding or ``event.stop()`` along the chain. Combined
        with the ``keys`` log entries on widget-side actions
        (ActionRow cycle/leave, SessionDashboardTable cursor up/down,
        ``MainScreen._refresh_dashboard`` entry/elapsed) this gives a
        timeline of "key arrived → who handled it (or didn't) → was a
        refresh in flight". Off by default; the call site costs one
        ``frozenset`` truthiness check when disabled.
        """
        focused = self.focused
        focused_id = getattr(focused, "id", None) if focused is not None else None
        focused_kind = type(focused).__name__ if focused is not None else None
        screen = self.screen
        active_workers = sum(1 for h in self._source_handles.values() if _worker_active(h))
        _debug(
            "keys",
            at="app_unhandled",
            key=getattr(event, "key", ""),
            screen=type(screen).__name__ if screen is not None else None,
            focused_id=focused_id,
            focused_kind=focused_kind,
            workers=active_workers,
            ts=time.monotonic(),
        )

    def on_mount(self) -> None:
        # ``time.monotonic()`` for diffs only — wall-clock jitters
        # under NTP corrections.
        _debug("startup", at="mount_started", ts=time.monotonic())
        self.push_screen(MainScreen(self.ctx))
        if self.pending_status:
            # A notify() raised on mount survives the app re-create
            # cycle when the outer loop stashes the message.
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
        # Probes every CATALOG agent regardless of cfg.enabled_agents
        # so auto-mode (empty enabled list) sees what is installed for
        # ``launch_user``.
        if self.probe_agents:
            self._kick_host_probe()
        # Cross-user case: the synchronous path leaves ``cwd_writable``
        # as None because the check would shell out via sudo.
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
            # Per-source periodic timers. Each source advances
            # independently so a slow source can't stall the others.
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

    def _kick_initial_sources(self) -> None:
        """Kick every refresh source whose ``kick_on_mount`` is True.

        Distinct from :meth:`kick_refresh` (which fans out
        unconditionally) so a "lazy" source can opt out of the startup
        wave without affecting the user-visible ``r`` keybinding.
        """
        for spec in self.ctx.refresh_sources or ():
            if spec.kick_on_mount:
                self._kick_source(spec)

    def kick_refresh(self) -> None:
        """Fan out: kick every registered refresh source.

        Each source is gated independently — a source whose worker is
        still in flight is skipped without affecting siblings.
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
        result.
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
        # ``from_cache`` is available on ``RemoteSnapshot`` (the
        # remote-host path) but not on ``TuiContext`` (the local
        # rebuild path), so we read it defensively.
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
    # Result-landing dispatch is data-driven: two registries map
    # source-id → handler so adding an asynchronous stream is a
    # registry entry rather than an if/elif ladder. Inspected in order:
    #
    # 1. ``_EXACT_HANDLERS``: ``dict[str, handler]`` — exact name match
    #    (``"main_ctx_rebuild"`` etc.). Most sources land here.
    # 2. ``_PREFIX_HANDLERS``: ordered ``list[(prefix, handler)]`` —
    #    fallback for families like ``"remote:<host>"`` where the
    #    handler peels the prefix off and routes by suffix.
    #
    # An unknown name falls through to a debug-log drop. Handlers are
    # bound methods on :class:`UxonApp`. The registry is built once
    # per instance in :meth:`__init__` so tests can inspect it without
    # going through the Pilot harness.

    def _handle_main_ctx_rebuild(self, event: _RefreshSourceLanded) -> None:
        """Apply a ``main_ctx_rebuild`` landing into canonical state.

        Sole writer of ``state.refresh_tick`` and ``state.main``.
        Requests a render via the scheduler; the scheduler decides
        when ``apply_loaded_ctx`` actually fires.
        """
        if event.error:
            self.notify(f"Refresh failed: {event.error}", severity="error", timeout=6)
            return
        ctx = event.value if isinstance(event.value, TuiContext) else None
        if ctx is None:
            return
        if not self._first_data_landed_logged:
            self._first_data_landed_logged = True
            _debug(
                "startup",
                at="first_data_landed",
                source=event.name,
                ts=time.monotonic(),
            )
        from .main_data import MainData

        self.state.refresh_tick += 1
        self.state.main = MainData.from_context(ctx)
        self._latest_ctx = ctx
        screen = next((s for s in self.screen_stack if isinstance(s, MainScreen)), None)
        if screen is not None and hasattr(screen, "_id"):
            screen.loading = self.state.main is None
        self._render.request("main_ctx")

    def _handle_remote_snapshot(self, event: _RefreshSourceLanded) -> None:
        """Apply a remote-host snapshot via the slot store.

        Source name is ``remote:<host>``. The fetcher always returns a
        :class:`RemoteSnapshot` (the collector is fail-soft); we wrap
        it in a :class:`SlotResult` and fold it into
        ``state.remote[host]`` via the pure :func:`slot_state.apply`.
        ``elapsed_ms`` from the fetcher is surfaced on the slot's
        ring so the latency-p50 tooltip has data.
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
        self._render.request("remote")

    def _render_dirty(self, kinds: frozenset[str]) -> bool:
        """Single render-dispatch entry. Called by :class:`RenderScheduler`.

        Returns True when a render actually happened. Returns False
        when ``MainScreen`` is not on top (e.g. a modal is up); the
        scheduler preserves the dirty state and re-fires on the next
        :meth:`RenderScheduler.request`.

        ``main_ctx`` rebuilds run the full :meth:`apply_loaded_ctx`
        path so structural fields (server status, banners, layout
        signature) re-evaluate. A ``remote``-only batch is a hot-path
        update of dashboard rows; :meth:`_refresh_dashboard` pulls
        from ``state.remote`` directly and is enough.
        """
        screen = next((s for s in self.screen_stack if isinstance(s, MainScreen)), None)
        if screen is None:
            return False
        top = self.screen_stack[-1] if self.screen_stack else None
        if not isinstance(top, MainScreen):
            return False
        if "main_ctx" in kinds and self._latest_ctx is not None:
            screen.apply_loaded_ctx(self._latest_ctx)
            return True
        if "remote" in kinds:
            screen._refresh_dashboard()
            return True
        return False

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

        Wired into ``on_mount`` (initial) and ``MainScreen.action_refresh``
        (manual ``r`` keybinding). The probe is one-shot on mount; the
        user picks up freshly-installed binaries via ``r``. Honours
        ``self.probe_agents`` so tests that opt out of probing (Pilot
        tests with ``probe_agents=False``, the pty integration suite
        that stubs ``probes.probe_host``) do not start a real subprocess
        from the manual-refresh path.
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

        Race-free: the worker builds a local availability dict and
        posts it in a single :class:`_HostReportUpdated`; the on-loop
        handler folds the payload into ``state.agent_availability``
        via :func:`slot_state.apply`. No ``self.ctx.<field>`` access
        from the thread.

        Uses ``probes.probe_host`` so one ``sh -lc`` round-trip covers
        every CATALOG agent.
        """
        import time as _time

        from uxon import agents as uxon_agents
        from uxon import probes as uxon_probes

        target_user = self.cfg.launch_user or uxon_probes._current_user()

        t0 = _time.monotonic()
        try:
            report = uxon_probes.probe_host(target_user)
        except Exception as exc:  # pragma: no cover — defensive
            self.post_message(
                _HostReportUpdated(
                    error=str(exc) or exc.__class__.__name__,
                    elapsed_ms=int((_time.monotonic() - t0) * 1000),
                )
            )
            return

        # Strict-whitelist mode (``enabled_agents`` non-empty): surface
        # exactly the enabled ids, marking absent binaries as "missing"
        # so the unavailable-modal can fire. Auto-mode (empty config):
        # surface only what is actually installed; un-installed
        # CATALOG ids stay out of the availability map entirely.
        configured = self.cfg.enabled_agents
        availability: dict = {}
        if configured:
            for aid in configured:
                status = report.agents.get(aid)
                if status is not None and status.path is not None:
                    availability[aid] = uxon_agents.AgentAvailability(
                        status="ok",
                        path=status.path,
                    )
                else:
                    binary = uxon_agents.CATALOG[aid].binary if aid in uxon_agents.CATALOG else aid
                    availability[aid] = uxon_agents.AgentAvailability(
                        status="missing",
                        error=f"{binary} not found on PATH",
                    )
        else:
            for aid, status in report.agents.items():
                if status.path is not None:
                    availability[aid] = uxon_agents.AgentAvailability(
                        status="ok",
                        path=status.path,
                    )
        self.post_message(
            _HostReportUpdated(
                availability=availability,
                elapsed_ms=int((_time.monotonic() - t0) * 1000),
            )
        )

    def _probe_cwd_writable_worker(self, cwd_at_start: str = "") -> None:
        """Background thread: probe write access and post the result.

        ``cwd_at_start`` threads through the message envelope so the
        on-loop handler can gate against an in-flight probe whose
        result no longer applies to the current cwd.
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

        Drops results whose ``cwd_at_start`` no longer matches the
        live ``ctx.cwd`` so a probe started against ``cwd_old`` is
        not surfaced as the answer for ``cwd_new``.
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

        Reads ``on_probe_link_health`` off the frozen :class:`TuiConfig`
        (no mutable ctx access from the thread) — ``apply_loaded_ctx``
        may replace ``self.app.ctx`` concurrently on the event loop,
        so reading ``self.ctx`` from the worker is a data race.
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

        availability = self.state.agent_availability.value or {}
        configured = self.cfg.enabled_agents
        if configured:
            current_all_missing = compute_all_missing(
                enabled_agents=configured,
                availability=availability,
            )
            modal_arg: tuple[str, ...] = tuple(configured)
        else:
            # Auto-mode: "all missing" iff the probe landed and found
            # zero installed agents.
            current_all_missing = self._host_probe_landed and not availability
            modal_arg = ()
        # An errored probe is independently fatal — neither mode can
        # know what is installed, so surface the diagnostic via the
        # same modal rather than leaving the user with a silent empty
        # list. Overrides the per-mode predicates above.
        if self._host_probe_error:
            current_all_missing = True
        modal_on_stack = any(isinstance(s, AgentsUnavailableScreen) for s in self.screen_stack)
        push = should_push_agents_unavailable(
            last_all_missing=self._last_all_missing,
            current_all_missing=current_all_missing,
            modal_already_on_stack=modal_on_stack,
            pending_launch=self.pending_launch is not None,
        )
        if push:
            self.push_screen(
                AgentsUnavailableScreen(modal_arg, error=self._host_probe_error)
            )
        if self._availability_resolved():
            self._last_all_missing = current_all_missing

    def _availability_resolved(self) -> bool:
        """True iff the availability snapshot is settled.

        Strict mode: every enabled agent has a non-pending entry.
        Auto-mode: the host probe has landed at least once.
        """
        configured = self.cfg.enabled_agents
        if not configured:
            return self._host_probe_landed
        availability = self.state.agent_availability.value or {}
        return all(
            aid in availability and getattr(availability[aid], "status", "pending") != "pending"
            for aid in configured
        )

    def on__agent_availability_updated(self, event: _AgentAvailabilityUpdated) -> None:
        """Backward-compatible handler. Dispatches via the shared path."""
        self._dispatch_availability_change()

    def on__host_report_updated(self, event: _HostReportUpdated) -> None:
        """Handler for the probe_host worker.

        Folds the worker's availability dict into
        ``state.agent_availability`` via :func:`slot_state.apply`.
        The dispatcher is the *only* on-loop site that mutates this
        slot, so observers see a consistent fresh dict on each tick.
        ``ctx.agent_availability`` returns ``state.<slot>.value`` —
        the freshly-allocated dict — so by-reference snapshots
        captured at modal-construction time would go stale.
        """
        from .slot_state import SlotResult
        from .slot_state import apply as apply_slot

        # ``availability is None`` is the bare-post pattern used by
        # tests that mutate the slot directly and post a bare message
        # to wake the handler — no slot apply, no flag flip.
        if event.availability is not None:
            avail_result: SlotResult[dict] = SlotResult(
                value=event.availability,
                error=None,
                elapsed_ms=event.elapsed_ms,
                attempted_at=time.time(),
            )
            self.state.agent_availability = apply_slot(self.state.agent_availability, avail_result)
        # Any non-bare result lands the probe — success *and* error.
        # Errors leave ``availability`` empty (the worker only posts a
        # dict on the success path) but still flip the gate so the
        # auto-mode unavailable-modal surfaces the diagnostic instead
        # of silently waiting forever.
        if event.availability is not None or event.error:
            self._host_probe_landed = True
            self._host_probe_error = event.error
        self._dispatch_availability_change()

    def on__link_health_updated(self, event: _LinkHealthUpdated) -> None:
        """Apply a link-health probe result to ``state.link_health``."""
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
        self._render.shutdown()
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
        from uxon.tui.context import session_name_from_launch_label

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

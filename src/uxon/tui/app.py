"""Textual app shell for the uxon TUI.

:class:`UxonApp` is the Textual host. On mount it pushes the
:class:`MainScreen`, kicks the background fan-out (delegated to
:class:`uxon.tui.workers.WorkerCoordinator`), and routes worker results
through its ``on__*`` message handlers into canonical :class:`TuiState`.

The outer non-textual controller (``request_launch`` → ``exit()`` → re-create
loop / TTY handoff) lives in :func:`uxon.tui.runner.run`. The worker bodies
live in :mod:`uxon.tui.workers`; the landed-result reducers in
:mod:`uxon.tui.source_dispatch`; the message envelopes in
:mod:`uxon.tui.messages`. This module keeps the Textual message-routing
surface (every ``on__*`` handler is a method here) plus the
``push_screen``/``call_later`` decisions that must stay on the event loop.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import ClassVar

from textual import events as _events
from textual.app import App
from textual.binding import Binding

from uxon import __version__
from uxon.domain.launch_request import LaunchRequest
from uxon.infra.events import debug as _debug
from uxon.infra.events import is_enabled as _debug_enabled

from .config import TuiConfig
from .context import TuiContext
from .messages import (
    _AgentAvailabilityUpdated,
    _CwdWritableUpdated,
    _HostReportUpdated,
    _LinkHealthUpdated,
    _MainCtxLoaded,
    _OffLoopCallbackDone,
    _RefreshSourceLanded,
    _WorktreesProbed,
)
from .screens.agents_unavailable import AgentsUnavailableScreen
from .screens.main import MainScreen
from .source_dispatch import build_source_dispatch
from .state import (
    compute_all_missing,
    should_push_agents_unavailable,
)
from .tui_state import TuiState
from .workers import WorkerCoordinator

# Event-loop watchdog cadence (UXON_DEBUG=keys only). Poll fast enough
# to resolve a stall on the order of a single rendered frame; report any
# tick that lands ≥ _WATCHDOG_STALL_S late, the scale at which a blocked
# loop would visibly starve the input parser.
_WATCHDOG_INTERVAL_S: float = 0.02
_WATCHDOG_STALL_S: float = 0.04


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
        # Terminal/window title — "uxon <version>" rather than Textual's
        # default class-name ("UxonApp"). Version read live from the
        # single source of truth so it never drifts.
        self.title = f"uxon {__version__}"
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
        # Seed the agent-availability slot from the cli-built initial
        # dict so the slot is canonical from construction.
        # ``dataclasses.replace`` produces a new (frozen)
        # :class:`SlotState` carrying a fresh copy of the seed dict;
        # the live value is read off ``state.agent_availability`` by
        # every consumer thereafter (the old read-through proxy on
        # ``ctx`` is gone).
        from dataclasses import replace as _replace

        self.state.agent_availability = _replace(
            self.state.agent_availability,
            value=dict(ctx.agent_availability),
        )
        self.pending_launch: LaunchRequest | None = None
        self.quit_rc: int | None = None
        self.pending_status = pending_status
        self.probe_agents = probe_agents
        # ── Key-drop diagnostics (UXON_DEBUG=keys) ──────────────────
        # Resolved once at construction so the hot ``on_event`` tap and
        # the loop watchdog cost a single bool check in production. The
        # tap records EVERY key at the instant Textual first sees it
        # (before bindings/forwarding); the existing ``on_key`` only
        # sees keys that bubble up UNHANDLED — by definition never the
        # ones that vanish.
        self._key_trace: bool = _debug_enabled("keys")
        # Monotonic timestamp of the previous watchdog tick; the gap to
        # the next tick beyond its scheduled interval is event-loop lag
        # (a blocked loop delays our own timer — the standard asyncio
        # stall probe). ``None`` until the first tick.
        self._wd_prev: float | None = None
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
        # Background-worker coordinator: owns the per-source/per-probe
        # in-flight gates, the worker bodies, and the teardown drain.
        # Constructed after ``cfg``/``state``/``_instance_epoch`` so it
        # can capture them; reads ``_instance_epoch`` live at post time
        # (the cross-instance drop gate).
        self._worker_coord = WorkerCoordinator(self)
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
        # Seed the cross-user accumulator from the initial ctx so the
        # very first ``MainScreen.__init__`` mounts with the right
        # column set when the launching ctx already carries multi-user
        # data (the synchronous build path in ``tui.context_builder.build_tui_context``
        # populates ``other_sessions`` before the TUI runs at all).
        # Remote landings feed the same set lazily in
        # :func:`source_dispatch.handle_remote_snapshot`; local rebuilds in
        # :meth:`MainScreen.apply_loaded_ctx`.
        for s in ctx.sessions:
            if s.user:
                self.main_ui.seen_users.add(s.user)
        for s in ctx.other_sessions:
            if s.user:
                self.main_ui.seen_users.add(s.user)

    async def on_event(self, event: _events.Event) -> None:
        """Total key tap: record EVERY key the instant Textual sees it.

        ``App.on_event`` is the one seam every non-forwarded input event
        crosses before bindings or forwarding (Textual 8.x). A real
        key, typed at the terminal, that reached the app's message pump
        at all appears here exactly once (``is_forwarded`` False). Diff
        this stream against the external stdin oracle
        (``script --log-in``) and any byte the oracle saw but this tap
        did NOT is a key lost *upstream* of the pump — in the terminal
        driver / escape-sequence parser, the half no prior in-process
        repro could observe (they injected synthetic ``Key`` messages
        straight into the pump, downstream of the parser). A byte that
        DID appear here but produced no action is the routing/focus
        half. Off by default; one bool check when disabled.
        """
        if self._key_trace and isinstance(event, _events.Key) and not event.is_forwarded:
            focused = self.focused
            _debug(
                "keys",
                at="app_received",
                key=getattr(event, "key", ""),
                screen=type(self.screen).__name__ if self.screen is not None else None,
                focused_id=getattr(focused, "id", None) if focused is not None else None,
                focused_kind=type(focused).__name__ if focused is not None else None,
                workers=self._worker_coord.active_source_count(),
                pending_launch=self.pending_launch is not None,
                mono=time.monotonic(),
                wall=time.time(),
            )
        await super().on_event(event)

    def _loop_watchdog(self) -> None:
        """Record event-loop stalls that could starve the input parser.

        Scheduled on the loop via ``set_interval``; a blocked loop
        delays this very callback, so the gap beyond the scheduled
        interval IS the stall duration. Correlate a logged stall window
        against a key the oracle saw vanish to confirm-or-refute the
        ``run all blocking callbacks off the loop`` theory (35387a2) in
        real usage rather than by assertion.
        """
        now = time.monotonic()
        prev = self._wd_prev
        self._wd_prev = now
        if prev is None:
            return
        lag = (now - prev) - _WATCHDOG_INTERVAL_S
        if lag >= _WATCHDOG_STALL_S:
            _debug(
                "keys",
                at="loop_stall",
                stall_ms=round(lag * 1000, 1),
                mono=now,
                wall=time.time(),
            )

    def on_key(self, event: _events.Key) -> None:
        """Diagnostic log for keys that fall through unhandled.

        ``UXON_DEBUG=keys`` writes one record per key event that
        bubbles all the way up to the App without being consumed by a
        widget binding or ``event.stop()`` along the chain. Combined
        with the ``keys`` log entries on widget-side actions
        (ActionRow cycle/leave, SessionListView cursor up/down,
        ``MainScreen._refresh_dashboard`` entry/elapsed) this gives a
        timeline of "key arrived → who handled it (or didn't) → was a
        refresh in flight". Off by default; the call site costs one
        ``frozenset`` truthiness check when disabled.
        """
        focused = self.focused
        focused_id = getattr(focused, "id", None) if focused is not None else None
        focused_kind = type(focused).__name__ if focused is not None else None
        screen = self.screen
        active_workers = self._worker_coord.active_source_count()
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
        if self._key_trace:
            # Arm the loop-stall watchdog only when key tracing is on.
            self.set_interval(_WATCHDOG_INTERVAL_S, self._loop_watchdog)
        self.push_screen(MainScreen(self.ctx, self.state))
        if self.pending_status:
            # A notify() raised on mount survives the app re-create
            # cycle when the outer loop stashes the message.
            self.notify(self.pending_status, severity="error", timeout=6)
        self.pending_status = ""
        # If the caller handed us a skeleton ctx, populate it
        # asynchronously — keeps the first frame fast and the event
        # loop unblocked. ``kick_initial_sources`` honours
        # ``SourceSpec.kick_on_mount`` so future one-shot or interval-only
        # sources can opt out of the initial fan-out.
        if self.ctx.loading:
            self._worker_coord.kick_initial_sources()
        # Kick off background host probe (tmux + all known agents).
        # Probes every CATALOG agent regardless of cfg.enabled_agents
        # so auto-mode (empty enabled list) sees what is installed for
        # ``launch_user``.
        if self.probe_agents:
            self._worker_coord.kick_host_probe()
        # Cross-user case: the synchronous path leaves ``cwd_writable``
        # as None because the check would shell out via sudo.
        if self.ctx.cwd_writable is None:
            self._worker_coord.kick_cwd_writable(self.ctx.cwd)
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
                    lambda spec=spec: self._worker_coord.kick_source(spec),
                )
            # ``set_interval`` already fires its first probe one interval out
            # — the same offset a one-shot ``set_timer(interval)`` would use,
            # so a separate one-shot only double-fires the first probe (RC6).
            self.set_interval(
                self.ctx.tui_ssh_refresh_interval_seconds,
                self._worker_coord.kick_link_health_probe,
            )

    # ── Background fan-out: thin delegators to the coordinator ───────
    #
    # ``MainScreen.action_refresh`` / ``launch_flow`` call these on the
    # App; the bodies live in :class:`WorkerCoordinator`. Kept as
    # methods so the production + test call surface (``app.kick_refresh``
    # etc.) is unchanged.

    def kick_refresh(self) -> None:
        self._worker_coord.kick_refresh()

    def run_off_loop(self, fn, *, on_success=None, on_error=None, label="action") -> None:
        """Run a blocking interactive callback off the loop (see coordinator).

        The single entry point screens/controllers use to keep tmux / ssh
        / sudo / git off the event-loop thread. Thin delegator to
        :meth:`WorkerCoordinator.run_off_loop`.
        """
        self._worker_coord.run_off_loop(fn, on_success=on_success, on_error=on_error, label=label)

    def _kick_initial_sources(self) -> None:
        self._worker_coord.kick_initial_sources()

    def _kick_host_probe(self) -> None:
        self._worker_coord.kick_host_probe()

    def _kick_link_health_probe(self) -> None:
        self._worker_coord.kick_link_health_probe()

    def probe_workspaces_then(
        self, cwd: str, on_done: object, *, probe_launchable: object = None
    ) -> None:
        self._worker_coord.probe_workspaces_then(cwd, on_done, probe_launchable=probe_launchable)

    # ── Source landing dispatch ─────────────────────────────────────
    #
    # Result-landing dispatch is data-driven: two registries map
    # source-id → handler so adding an asynchronous stream is a
    # registry entry rather than an if/elif ladder. Inspected in order:
    #
    # 1. ``_source_dispatch_exact``: ``dict[str, handler]`` — exact name
    #    match (``"main_ctx_rebuild"`` etc.). Most sources land here.
    # 2. ``_source_dispatch_prefix``: ordered ``list[(prefix, handler)]``
    #    — fallback for families like ``"remote:<host>"`` where the
    #    handler peels the prefix off and routes by suffix.
    #
    # An unknown name falls through to a debug-log drop. The reducer
    # bodies live in :mod:`uxon.tui.source_dispatch`; the registry is
    # built once per instance in :meth:`__init__` so tests can inspect
    # it without going through the Pilot harness.

    def _build_source_dispatch(
        self,
    ) -> tuple[
        dict[str, Callable[[_RefreshSourceLanded], None]],
        list[tuple[str, Callable[[_RefreshSourceLanded], None]]],
    ]:
        """Construct the (exact, prefix) dispatch registries (shell).

        Delegates to :func:`source_dispatch.build_source_dispatch`, which
        returns reducers bound to this App. Inspected by
        :meth:`on__refresh_source_landed` and by unit tests.
        """
        return build_source_dispatch(self)

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

        Both branches' widget mutations run inside one
        :meth:`App.batch_update` so a landing yields at most one composite
        (AC6). Nesting with the status writer's own ``batch_update`` is
        safe — ``_batch_count`` is counter-based — and the batch wraps
        synchronous widget writes only (no awaits).
        """
        screen = next((s for s in self.screen_stack if isinstance(s, MainScreen)), None)
        if screen is None:
            return False
        top = self.screen_stack[-1] if self.screen_stack else None
        if not isinstance(top, MainScreen):
            return False
        with self.batch_update():
            if "main_ctx" in kinds and self._latest_ctx is not None:
                screen.apply_loaded_ctx(self._latest_ctx)
                return True
            if "remote" in kinds:
                screen._refresh_dashboard()
                return True
        return False

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
        :func:`source_dispatch.build_source_dispatch`.
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

    def on__worktrees_probed(self, event: _WorktreesProbed) -> None:
        """Invoke the launch-flow callback with launchability + workspaces + error."""
        event.on_done(event.launchable, event.workspaces, event.error)

    def on__off_loop_callback_done(self, event: _OffLoopCallbackDone) -> None:
        """Run the on-loop continuation for a blocking callback finished off-loop.

        Cross-instance gate mirrors :meth:`on__refresh_source_landed`: a
        result stamped with a prior app instance's epoch is dropped (the
        worker belongs to an app that no longer exists, e.g. after a TTY
        handoff recreated the instance). ``-1`` is the unstamped-test
        sentinel and skips the gate.
        """
        if event.instance_epoch != -1 and event.instance_epoch != self._instance_epoch:
            return
        if event.error is not None:
            if event.on_error is not None:
                event.on_error(event.error)
            return
        if event.on_success is not None:
            event.on_success(event.value)

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
                AgentsUnavailableScreen(
                    modal_arg, agents=self.cfg.agents, error=self._host_probe_error
                )
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
        Consumers read ``state.agent_availability.value`` — the
        freshly-allocated dict — so by-reference snapshots captured at
        modal-construction time would go stale.
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

    def on_unmount(self) -> None:
        """Drain in-flight workers before the app loop returns.

        Textual fires ``Unmount`` from :meth:`App._shutdown` after
        ``_close_all`` / ``_close_messages``, so by the time we get
        here the message pump is already winding down — cancelling
        workers from this hook is exactly the "before App.run()
        returns" point the spec calls for.
        """
        self._render.shutdown()
        self._worker_coord.drain()

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

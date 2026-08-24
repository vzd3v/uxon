"""Background-worker coordination for :class:`uxon.tui.app.UxonApp`.

:class:`WorkerCoordinator` owns the App's background fan-out: the per-source
refresh workers, the host / link-health / cwd-writable probes, the one-shot
worktree probe, the per-source/per-probe in-flight gating, and the teardown
drain. Extracted from ``app.py`` in Phase P7.

The Textual primitives the coordinator needs are **injected** at construction
(captured from the App), not reached through a hidden back-reference:

  - ``run_worker`` / ``post_message`` — bound App methods captured once;
  - ``instance_epoch`` — a **callable** read at *post time* so every
    ``_RefreshSourceLanded`` carries the CURRENT App's epoch. This is the
    cross-instance drop gate: a worker spawned by instance-N must stamp
    instance-N's epoch even though the outer ``run()`` loop may have created
    instance-N+1 by the time the worker posts. Capturing the integer at
    construction would defeat the gate, so we keep the read live.

Mutable App state read live by the workers (``ctx``, ``workers``) is reached
through the retained App reference, because those are replaced on the event
loop (``apply_loaded_ctx`` swaps ``app.ctx``) and must be read fresh. ``cfg``
is stable for the App's lifetime and captured directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from textual.worker import Worker, WorkerState

from uxon.infra.events import debug as _debug
from uxon.infra.events import metrics_record

from .context import CallbackError
from .messages import (
    _CwdWritableUpdated,
    _HostReportUpdated,
    _LinkHealthUpdated,
    _OffLoopCallbackDone,
    _RefreshSourceLanded,
    _WorktreesProbed,
)

if TYPE_CHECKING:
    from .app import UxonApp

_ACTIVE_STATES = (WorkerState.PENDING, WorkerState.RUNNING)


def _worker_active(w: Worker | None) -> bool:
    """True iff ``w`` is queued or running.

    Used as the in-flight gate for periodic kick-X helpers — derives
    from worker state rather than a separate bool, so a cancelled or
    crashed worker frees its slot automatically.
    """
    return w is not None and w.state in _ACTIVE_STATES


class WorkerCoordinator:
    """Owns the App's background workers + their in-flight gates + drain."""

    def __init__(self, app: UxonApp) -> None:
        # The App is the single injection point. ``run_worker`` /
        # ``post_message`` are read LIVE off it (see the properties
        # below) rather than captured here, so a test that patches
        # ``app.run_worker`` / ``app.post_message`` after construction
        # is honoured — exactly the pre-P7 ``self.run_worker`` /
        # ``self.post_message`` semantics. ``_instance_epoch`` is read
        # live at post time (the cross-instance drop gate). ``cfg`` is
        # stable for the App's lifetime so it is captured directly;
        # ``ctx`` / ``workers`` / ``probe_agents`` are read live off the
        # App because they change on the event loop.
        self._app = app
        self._cfg = app.cfg

        # Per-source in-flight gates: ``self._source_handles[name]`` holds
        # the in-flight worker for that source so a slow source never
        # blocks a faster sibling's next tick.
        self._source_handles: dict[str, Worker | None] = {}
        self._host_probe_handle: Worker | None = None
        self._link_health_handle: Worker | None = None

    # ── Injected Textual primitives (read live off the App) ──────────

    def _run_worker(self, *args: Any, **kwargs: Any) -> Worker:
        return self._app.run_worker(*args, **kwargs)

    def _post_message(self, message: Any) -> bool:
        return self._app.post_message(message)

    def _instance_epoch(self) -> int:
        """Read the CURRENT App's epoch — at *post time*, never snapshotted.

        Stamped onto every ``_RefreshSourceLanded``. A worker spawned by
        instance-N must carry instance-N's epoch even if the outer
        ``run()`` loop has created instance-N+1 by the time it posts;
        the dispatcher drops a result whose epoch != the live App epoch.
        """
        return self._app._instance_epoch

    # ── In-flight introspection ─────────────────────────────────────

    def active_source_count(self) -> int:
        """Number of refresh sources whose worker is currently in flight."""
        return sum(1 for h in self._source_handles.values() if _worker_active(h))

    # ── Refresh-source fan-out ──────────────────────────────────────

    def kick_initial_sources(self) -> None:
        """Kick every refresh source whose ``kick_on_mount`` is True.

        Distinct from :meth:`kick_refresh` (which fans out
        unconditionally) so a "lazy" source can opt out of the startup
        wave without affecting the user-visible ``r`` keybinding.
        """
        for spec in self._app.ctx.refresh_sources or ():
            if spec.kick_on_mount:
                self.kick_source(spec)

    def kick_refresh(self) -> None:
        """Fan out: kick every registered refresh source.

        Each source is gated independently — a source whose worker is
        still in flight is skipped without affecting siblings.
        """
        sources = self._app.ctx.refresh_sources or ()
        if not sources:
            _debug("refresh", at="kick_refresh", action="skip", reason="no_sources")
            return
        _debug("refresh", at="kick_refresh", action="fanout", count=len(sources))
        for spec in sources:
            self.kick_source(spec)

    def kick_source(self, spec: Any) -> None:
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
        self._source_handles[spec.name] = self._run_worker(
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
        self._post_message(
            _RefreshSourceLanded(
                name=result.name,
                value=result.value,
                error=result.error or "",
                elapsed_ms=result.elapsed_ms,
                instance_epoch=self._instance_epoch(),
            )
        )

    # ── Host probe (tmux + agent binaries) ──────────────────────────

    def kick_host_probe(self) -> None:
        """Schedule the host probe iff one isn't already in flight.

        Wired into ``on_mount`` (initial) and ``MainScreen.action_refresh``
        (manual ``r`` keybinding). The probe is one-shot on mount; the
        user picks up freshly-installed binaries via ``r``. Honours
        ``app.probe_agents`` so tests that opt out of probing (Pilot
        tests with ``probe_agents=False``, the pty integration suite
        that stubs ``probes.probe_host``) do not start a real subprocess
        from the manual-refresh path.
        """
        if not self._app.probe_agents:
            return
        if _worker_active(self._host_probe_handle):
            return
        self._host_probe_handle = self._run_worker(
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
        every catalogued agent (``self._cfg.agents``).
        """
        import time as _time

        from uxon.infra import agents as uxon_agents
        from uxon.infra import probes as uxon_probes

        t0 = _time.monotonic()
        try:
            reports = {}
            profiles = self._cfg.launch_profiles
            if profiles:
                by_user: dict[str, set[str]] = {}
                for profile in profiles.values():
                    agents = by_user.setdefault(profile.launch_user, set())
                    if not profile.container_profile:
                        agents.add(profile.agent)
                for user, agent_ids in by_user.items():
                    catalog = {
                        aid: self._cfg.agents[aid]
                        for aid in sorted(agent_ids)
                        if aid in self._cfg.agents
                    }
                    reports[user] = uxon_probes.probe_host(user, catalog)
            else:
                # Compatibility path for old tests/fixtures that construct a
                # TuiConfig without launch_profiles.
                target_user = self._cfg.launch_user or uxon_probes._current_user()
                reports[target_user] = uxon_probes.probe_host(target_user, self._cfg.agents)
        except Exception as exc:  # pragma: no cover — defensive
            self._post_message(
                _HostReportUpdated(
                    error=str(exc) or exc.__class__.__name__,
                    elapsed_ms=int((_time.monotonic() - t0) * 1000),
                )
            )
            return

        # Strict profile mode: surface exactly the enabled profile ids,
        # marking absent host binaries as missing. Auto-mode: surface only
        # profiles that are actually launchable.
        configured = self._cfg.enabled_profiles
        availability: dict = {}
        if self._cfg.launch_profiles:
            for pid in configured:
                profile = self._cfg.launch_profiles.get(pid)
                if profile is None:
                    if not self._cfg.launch_auto_mode:
                        availability[pid] = uxon_agents.AgentAvailability(
                            status="missing", error="unknown launch profile"
                        )
                    continue
                report = reports.get(profile.launch_user)
                if report is None or report.tmux.path is None:
                    if not self._cfg.launch_auto_mode:
                        availability[pid] = uxon_agents.AgentAvailability(
                            status="missing",
                            error="tmux not found on PATH",
                        )
                    continue
                if profile.container_profile:
                    availability[pid] = uxon_agents.AgentAvailability(
                        status="ok",
                        path=f"container:{profile.container_profile}",
                    )
                    continue
                status = report.agents.get(profile.agent)
                if status is not None and status.path is not None:
                    availability[pid] = uxon_agents.AgentAvailability(status="ok", path=status.path)
                elif not self._cfg.launch_auto_mode:
                    spec = self._cfg.agents.get(profile.agent)
                    binary = spec.binary if spec is not None else profile.agent
                    availability[pid] = uxon_agents.AgentAvailability(
                        status="missing", error=f"{binary} not found on PATH"
                    )
        else:
            # No launch profiles populated (operator removed every
            # built-in and defined none): fall back to reporting every
            # agent the host probe discovered so auto-mode still surfaces
            # a usable choice.
            report = next(iter(reports.values()))
            for aid, status in report.agents.items():
                if status.path is not None:
                    availability[aid] = uxon_agents.AgentAvailability(status="ok", path=status.path)
        self._post_message(
            _HostReportUpdated(
                availability=availability,
                elapsed_ms=int((_time.monotonic() - t0) * 1000),
            )
        )

    # ── cwd-writable probe ──────────────────────────────────────────

    def kick_cwd_writable(self, cwd_at_start: str) -> None:
        """Spawn the cwd-write probe for ``cwd_at_start`` (cross-user path).

        Spawned without a stored handle; :meth:`drain` reaches into the
        ``cwd_writable`` worker group to cancel it on teardown.
        """
        self._run_worker(
            lambda cwd=cwd_at_start: self._probe_cwd_writable_worker(cwd),
            thread=True,
            exclusive=False,
            group="cwd_writable",
        )

    def _probe_cwd_writable_worker(self, cwd_at_start: str = "") -> None:
        """Background thread: probe write access and post the result.

        ``cwd_at_start`` threads through the message envelope so the
        on-loop handler can gate against an in-flight probe whose
        result no longer applies to the current cwd.
        """
        try:
            writable = bool(self._cfg.on_probe_cwd_writable())
        except CallbackError:
            writable = False
        except Exception:  # pragma: no cover — defensive
            writable = False
        self._post_message(_CwdWritableUpdated(writable, cwd_at_start=cwd_at_start))

    # ── SSH link-health probe ───────────────────────────────────────

    def kick_link_health_probe(self) -> None:
        if _worker_active(self._link_health_handle):
            return
        self._link_health_handle = self._run_worker(
            self._probe_link_health_worker, thread=True, exclusive=False, group="link_health"
        )

    def _probe_link_health_worker(self) -> None:
        """Background thread: probe SSH-path health and post the result.

        Reads ``on_probe_link_health`` off the frozen :class:`TuiConfig`
        (no mutable ctx access from the thread) — ``apply_loaded_ctx``
        may replace ``self.app.ctx`` concurrently on the event loop,
        so reading ``self.ctx`` from the worker is a data race.
        """
        from uxon.domain.status import LinkHealthStatus

        try:
            probe = self._cfg.on_probe_link_health
            status = probe() if callable(probe) else None
        except Exception as exc:  # pragma: no cover — defensive
            status = LinkHealthStatus(
                state="error",
                summary=str(exc).strip() or exc.__class__.__name__,
            )
        if status is None:
            status = LinkHealthStatus()
        self._post_message(_LinkHealthUpdated(status))

    # ── One-shot worktree probe (launch-screen open) ────────────────

    def probe_workspaces_then(
        self, cwd: str, on_done: Any, *, probe_launchable: Any = None
    ) -> None:
        """Probe ``cwd`` off the event loop; call ``on_done(launchable, list, error)``.

        Off-event-loop git probe (under the non-interactive sudo prefix on
        the CLI side, §4.2). Runs ONCE per launch-flow open, before the
        launch screen is pushed. The worker reads ``on_probe_worktrees``
        off the frozen :class:`TuiConfig` (no mutable-ctx access from the
        thread, same rule as :meth:`_probe_link_health_worker`), then posts
        a :class:`_WorktreesProbed`; ``on_done`` runs on the message loop so
        it can gate / push :class:`LaunchOptionsScreen` safely.

        ``probe_launchable`` (optional) is a launchability predicate run in
        the SAME worker so its cross-user ``sudo`` call never blocks the
        event loop — passed when the caller has no pre-resolved value (cwd
        pre-resolves via its reactive slot and omits it → ``launchable`` is
        ``None``). Any *launchability* probe error degrades to "not
        launchable" (False), mirroring :func:`probe_cwd_writable`'s yes/no
        contract. A False result skips the worktree probe — the gate aborts
        anyway. A *worktree* probe that raises (e.g. ``git worktree list``
        failed on a real repo) is NOT swallowed: its message rides ``error``
        so the WORKSPACE column can show an error row.
        """

        def _worker() -> None:
            launchable: bool | None = None
            if probe_launchable is not None:
                try:
                    launchable = bool(probe_launchable())
                except Exception:  # pragma: no cover — defensive; any error = no
                    launchable = False
            workspaces: list = []
            error: str | None = None
            if launchable is not False:
                try:
                    probe = self._cfg.on_probe_worktrees
                    workspaces = list(probe(cwd)) if callable(probe) else []
                except Exception as exc:
                    # A git probe that raised is a real failure (e.g. the repo
                    # exists but ``git worktree list`` errored) — surface it as
                    # an error row rather than silently hiding the column or
                    # mislabelling it as "not a git repo".
                    error = str(exc).strip() or exc.__class__.__name__
            self._post_message(_WorktreesProbed(launchable, workspaces, on_done, error))

        self._run_worker(_worker, thread=True, exclusive=False, group="worktree_probe")

    # ── Interactive blocking callbacks (run off the event loop) ──────

    def run_off_loop(
        self,
        fn: Any,
        *,
        on_success: Any = None,
        on_error: Any = None,
        label: str = "action",
    ) -> None:
        """Run a blocking interactive callback in a worker, continue on loop.

        ``fn`` is a zero-arg callable that performs blocking work
        (``tmux`` / ``ssh`` / ``sudo`` / ``git`` via subprocess) — it MUST
        NOT touch Textual or the DOM. The worker runs it off the event
        loop, then posts a :class:`_OffLoopCallbackDone` so the App's
        on-loop handler invokes ``on_success(result)`` (or
        ``on_error(exc)`` if ``fn`` raised). Both continuations run back on
        the event loop and may safely ``notify`` / ``push_screen`` /
        ``request_launch`` / ``action_refresh``.

        This is the single mechanism that keeps the interactive blocking
        class off the loop. Every error is captured (the worker never dies
        silently): :class:`CallbackError` is the expected shape, but any
        other ``Exception`` is delivered to ``on_error`` too so a
        misbehaving callback surfaces as a toast rather than a dropped
        action. ``BaseException`` (KeyboardInterrupt / SystemExit /
        CancelledError) propagates — worker cancellation must stay
        effective.
        """

        def _worker() -> None:
            try:
                value: object = fn()
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:  # noqa: BLE001 — delivered to on_error
                self._post_message(
                    _OffLoopCallbackDone(
                        None,
                        exc,
                        on_success,
                        on_error,
                        instance_epoch=self._instance_epoch(),
                    )
                )
                return
            self._post_message(
                _OffLoopCallbackDone(
                    value,
                    None,
                    on_success,
                    on_error,
                    instance_epoch=self._instance_epoch(),
                )
            )

        self._run_worker(_worker, thread=True, exclusive=False, group=f"action:{label}")

    # ── Teardown drain ──────────────────────────────────────────────

    def drain(self, *, grace_seconds: float = 0.1) -> None:
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

        Called from :meth:`UxonApp.on_unmount`. Safe to call multiple times.
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
        # into the worker manager group instead. ``app.workers``
        # exposes a :class:`WorkerManager` whose ``__iter__`` yields
        # every worker; filtering by group avoids touching workers
        # already covered above.
        try:
            for w in list(self._app.workers):
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
            instance_epoch=self._instance_epoch(),
        )

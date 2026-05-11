"""Tests for the pluggable refresh-source registry.

PR1 introduces ``uxon.tui.refresh`` as the seam future asynchronous
data streams (e.g. multi-host SSH collectors) plug into. The contract
this file pins down:

- ``run_source`` is fail-soft: any ``Exception`` raised by a fetcher
  is captured into ``SourceResult.error`` instead of escaping.
- ``KeyboardInterrupt`` and ``SystemExit`` propagate so a user Ctrl-C
  or a process termination is never silently swallowed.
- ``SourceResult`` carries the source name, the value (or None), the
  error (or None), and elapsed wall time in milliseconds.
- A failing source produces a result with ``value is None`` — so a
  per-source dispatch handler that checks ``error`` first and ignores
  ``None`` values cannot be misled into clobbering known-good state
  with empty data.

These tests are pure: no Textual, no event loop. Behaviour wiring
through the app lives in ``test_uxon_tui_screens.WorkerGateTests``.
"""

from __future__ import annotations

import unittest

from uxon.tui.refresh import SourceResult, SourceSpec, run_source


class RunSourceTests(unittest.TestCase):
    def test_returns_value_on_success(self) -> None:
        spec = SourceSpec(name="ok", fetch=lambda: {"hello": 1})
        result = run_source(spec)
        self.assertEqual(result.name, "ok")
        self.assertEqual(result.value, {"hello": 1})
        self.assertIsNone(result.error)
        self.assertGreaterEqual(result.elapsed_ms, 0)

    def test_captures_exception_into_error(self) -> None:
        def boom() -> object:
            raise RuntimeError("disk on fire")

        spec = SourceSpec(name="hot", fetch=boom)
        result = run_source(spec)
        self.assertEqual(result.name, "hot")
        self.assertIsNone(result.value)
        self.assertIsNotNone(result.error)
        assert result.error is not None  # for the type checker
        self.assertIn("disk on fire", result.error)

    def test_captures_typeerror_with_class_name_when_message_empty(self) -> None:
        def boom() -> object:
            raise ValueError()

        result = run_source(SourceSpec(name="bare", fetch=boom))
        # Empty exception message → falls back to class name so the
        # debug log row never carries an empty error= field.
        self.assertEqual(result.error, "ValueError")

    def test_keyboard_interrupt_propagates(self) -> None:
        def boom() -> object:
            raise KeyboardInterrupt

        spec = SourceSpec(name="ctrl_c", fetch=boom)
        with self.assertRaises(KeyboardInterrupt):
            run_source(spec)

    def test_system_exit_propagates(self) -> None:
        def boom() -> object:
            raise SystemExit(2)

        with self.assertRaises(SystemExit) as cm:
            run_source(SourceSpec(name="sysexit", fetch=boom))
        self.assertEqual(cm.exception.code, 2)

    def test_one_sources_failure_does_not_pollute_anothers_result(self) -> None:
        """The headline isolation property the registry exists to guarantee.

        Running a failing source and a healthy source independently
        must produce results that do not influence each other — a
        slow/broken source can never corrupt a sibling's snapshot.
        This is the unit-level proof of the property the multi-host
        feature relies on.
        """

        def boom() -> object:
            raise OSError("ssh: connect timeout")

        slow_failing = SourceSpec(name="remote:gpu-vps", fetch=boom)
        healthy = SourceSpec(name="local_sessions", fetch=lambda: ["one", "two"])

        r_failing = run_source(slow_failing)
        r_healthy = run_source(healthy)

        self.assertIsNone(r_failing.value)
        self.assertEqual(r_healthy.value, ["one", "two"])
        self.assertIsNotNone(r_failing.error)
        self.assertIsNone(r_healthy.error)


class SourceResultTests(unittest.TestCase):
    def test_dataclass_fields(self) -> None:
        # Pin the field set: app.py builds ``_RefreshSourceLanded``
        # from these names; renaming any of them is a cross-file
        # contract change that should fail this test loudly.
        r = SourceResult(name="x", value=None, error=None, elapsed_ms=0)
        self.assertEqual(r.name, "x")
        self.assertIsNone(r.value)
        self.assertIsNone(r.error)
        self.assertEqual(r.elapsed_ms, 0)


class SourceSpecTests(unittest.TestCase):
    def test_default_cadence_attr_matches_legacy_refresh(self) -> None:
        # Sources default to the legacy ``tui_refresh_interval_seconds``
        # cadence so existing callers register without touching the
        # field. Multi-host sources will override to
        # ``remote_refresh_interval_seconds`` (PR3).
        spec = SourceSpec(name="legacy", fetch=lambda: None)
        self.assertEqual(spec.cadence_seconds_attr, "tui_refresh_interval_seconds")
        self.assertTrue(spec.kick_on_mount)

    def test_one_shot_source_via_none_cadence(self) -> None:
        # A source with cadence_seconds_attr=None is run once on mount
        # and never on a periodic timer — pattern reserved for things
        # like the cwd-writable probe (when migrated).
        spec = SourceSpec(name="oneshot", fetch=lambda: None, cadence_seconds_attr=None)
        self.assertIsNone(spec.cadence_seconds_attr)


class DispatchRegistryTests(unittest.TestCase):
    """Stage 8: ``UxonApp`` builds an id → handler dispatch registry
    inspected by :meth:`on__refresh_source_landed`. The legacy
    ``main_ctx_rebuild`` and ``remote:<host>`` paths must keep working
    bit-identical, but they now live in the registry rather than an
    ``if/elif`` ladder. These tests poke at the registry directly so a
    future regression that swaps the lookup back into bespoke control
    flow is caught without a Pilot.
    """

    def _make_app(self) -> object:
        # Lazy import: importing ``uxon.tui.app`` requires textual.
        try:
            from uxon.tui.app import UxonApp
        except ImportError:
            self.skipTest("textual not available")
        from uxon.tui.context import LaunchRequest, TuiContext

        # Build a minimal ``TuiContext`` mirroring the screens-test
        # factory. We don't need a Pilot — only the app constructor,
        # which builds the dispatch registry in ``__init__``.
        ctx = TuiContext(
            sessions=[],
            total_cpu="0",
            total_ram="0",
            version="0.0",
            cwd="/tmp",
            cwd_short="tmp",
            new_project_root="/tmp",
            existing_projects=[],
            cwd_writable=True,
            current_user="u",
            on_launch_cwd=lambda agent_id, mode_id: LaunchRequest(cmd=("/bin/true",), label="cwd"),
            on_launch_new=lambda n, agent_id, mode_id, g: LaunchRequest(
                cmd=("/bin/true",), label="new"
            ),
            on_launch_existing=lambda n, agent_id, mode_id: LaunchRequest(
                cmd=("/bin/true",), label="existing"
            ),
        )
        return UxonApp(ctx, probe_agents=False)

    def test_main_ctx_rebuild_in_exact_registry(self) -> None:
        app = self._make_app()
        self.assertIn("main_ctx_rebuild", app._source_dispatch_exact)  # type: ignore[attr-defined]

    def test_remote_prefix_in_prefix_registry(self) -> None:
        app = self._make_app()
        prefixes = [p for p, _ in app._source_dispatch_prefix]  # type: ignore[attr-defined]
        self.assertIn("remote:", prefixes)

    def test_main_ctx_handler_requests_render(self) -> None:
        from uxon.tui.app import _RefreshSourceLanded

        app = self._make_app()
        ctx = app.ctx  # type: ignore[attr-defined]
        requests: list[str] = []
        app._render.request = requests.append  # type: ignore[attr-defined,method-assign]

        handler = app._source_dispatch_exact["main_ctx_rebuild"]  # type: ignore[attr-defined]
        handler(_RefreshSourceLanded(name="main_ctx_rebuild", value=ctx))

        self.assertEqual(requests, ["main_ctx"])
        self.assertIs(app._latest_ctx, ctx)  # type: ignore[attr-defined]

    def test_event_default_epoch_is_unstamped_sentinel(self) -> None:
        """Default ``instance_epoch=-1`` means "synthetic / unstamped"
        — the dispatcher skips the epoch gate so legacy tests keep
        working unchanged.
        """
        from uxon.tui.app import _RefreshSourceLanded

        ev = _RefreshSourceLanded(name="x", value=None)
        self.assertEqual(ev.instance_epoch, -1)

    def test_stale_epoch_is_dropped_at_dispatcher(self) -> None:
        """A real (non-sentinel) epoch that doesn't match the app's
        current epoch must be dropped — no handler invocation, no
        message posted. This protects against a worker spawned by a
        previous app instance landing after a TTY-handoff app
        re-creation.
        """
        from uxon.tui.app import _RefreshSourceLanded

        app = self._make_app()
        posted: list[object] = []
        app.post_message = posted.append  # type: ignore[method-assign]

        # Pick an epoch deliberately != app's. Using ``app_epoch + 1``
        # (any int other than the app's value works); using
        # ``-1`` would hit the unstamped-sentinel skip path.
        app_epoch = app._instance_epoch  # type: ignore[attr-defined]
        stale = _RefreshSourceLanded(
            name="main_ctx_rebuild",
            value=app.ctx,  # type: ignore[attr-defined]
            instance_epoch=app_epoch + 100,
        )
        app.on__refresh_source_landed(stale)  # type: ignore[attr-defined]
        self.assertEqual(posted, [])

    def test_matching_epoch_is_dispatched(self) -> None:
        """A real event with epoch matching the app's epoch must be
        dispatched normally — the gate's job is to drop *stale*
        events, not all stamped events.
        """
        from uxon.tui.app import _RefreshSourceLanded

        app = self._make_app()
        requests: list[str] = []
        app._render.request = requests.append  # type: ignore[attr-defined,method-assign]

        ev = _RefreshSourceLanded(
            name="main_ctx_rebuild",
            value=app.ctx,  # type: ignore[attr-defined]
            instance_epoch=app._instance_epoch,  # type: ignore[attr-defined]
        )
        app.on__refresh_source_landed(ev)  # type: ignore[attr-defined]
        self.assertEqual(requests, ["main_ctx"])

    def test_unknown_name_falls_through_to_drop(self) -> None:
        from uxon.tui.app import _RefreshSourceLanded

        app = self._make_app()
        # No-op post_message; an unknown source name must NOT post
        # anything (the legacy ladder logged and dropped).
        posted: list[object] = []
        app.post_message = posted.append  # type: ignore[method-assign]
        # Drive the dispatcher directly; it should not raise and not
        # post any message.
        app.on__refresh_source_landed(  # type: ignore[attr-defined]
            _RefreshSourceLanded(name="totally_unknown", value=None)
        )
        self.assertEqual(posted, [])


class WorkerDrainTests(unittest.TestCase):
    """Stage 8 § Worker lifetime: ``UxonApp._drain_workers`` cancels
    every tracked handle and waits a bounded grace before returning,
    so no worker thread survives the app instance that spawned it.
    """

    def _make_app(self) -> object:
        try:
            from uxon.tui.app import UxonApp
        except ImportError:
            self.skipTest("textual not available")
        from uxon.tui.context import LaunchRequest, TuiContext

        ctx = TuiContext(
            sessions=[],
            total_cpu="0",
            total_ram="0",
            version="0.0",
            cwd="/tmp",
            cwd_short="tmp",
            new_project_root="/tmp",
            existing_projects=[],
            cwd_writable=True,
            current_user="u",
            on_launch_cwd=lambda agent_id, mode_id: LaunchRequest(cmd=("/bin/true",), label="cwd"),
            on_launch_new=lambda n, agent_id, mode_id, g: LaunchRequest(
                cmd=("/bin/true",), label="new"
            ),
            on_launch_existing=lambda n, agent_id, mode_id: LaunchRequest(
                cmd=("/bin/true",), label="existing"
            ),
        )
        return UxonApp(ctx, probe_agents=False)

    def _fake_worker(self, *, active: bool):
        """Build a stand-in for ``textual.worker.Worker`` exposing only
        the surface :meth:`_drain_workers` actually touches.
        """
        from textual.worker import WorkerState

        class _FakeWorker:
            def __init__(self, active: bool) -> None:
                self.state = WorkerState.RUNNING if active else WorkerState.SUCCESS
                self.group = "test"
                self.cancel_called = 0

            def cancel(self) -> None:
                self.cancel_called += 1
                # Mimic textual: cancellation flips state out of RUNNING
                # so the bounded poll-loop in ``_drain_workers`` exits
                # immediately rather than burning the full grace window.
                from textual.worker import WorkerState as _WS

                self.state = _WS.CANCELLED

        return _FakeWorker(active)

    def test_drain_cancels_active_source_handles(self) -> None:
        app = self._make_app()
        w_a = self._fake_worker(active=True)
        w_b = self._fake_worker(active=True)
        app._source_handles = {"main_ctx_rebuild": w_a, "remote:foo": w_b}  # type: ignore[attr-defined,assignment]
        # Wipe the host_probe / link_health slots so they don't try
        # to reach into a real WorkerManager.
        app._host_probe_handle = None  # type: ignore[attr-defined]
        app._link_health_handle = None  # type: ignore[attr-defined]
        app._drain_workers(grace_seconds=0.05)  # type: ignore[attr-defined]
        self.assertEqual(w_a.cancel_called, 1)
        self.assertEqual(w_b.cancel_called, 1)

    def test_drain_skips_already_completed_workers(self) -> None:
        app = self._make_app()
        done = self._fake_worker(active=False)
        app._source_handles = {"main_ctx_rebuild": done}  # type: ignore[attr-defined,assignment]
        app._host_probe_handle = None  # type: ignore[attr-defined]
        app._link_health_handle = None  # type: ignore[attr-defined]
        app._drain_workers(grace_seconds=0.0)  # type: ignore[attr-defined]
        # Already-done worker must not be cancelled — there's nothing
        # to cancel and ``Worker.cancel`` would warn in textual.
        self.assertEqual(done.cancel_called, 0)

    def test_drain_handles_empty_state(self) -> None:
        app = self._make_app()
        app._source_handles = {}  # type: ignore[attr-defined,assignment]
        app._host_probe_handle = None  # type: ignore[attr-defined]
        app._link_health_handle = None  # type: ignore[attr-defined]
        # Must not raise.
        app._drain_workers(grace_seconds=0.0)  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()

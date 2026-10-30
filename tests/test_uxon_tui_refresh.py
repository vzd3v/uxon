"""Refresh dispatch and actual source scheduling contracts."""

from __future__ import annotations

import os
import unittest
from dataclasses import replace
from unittest.mock import PropertyMock, patch

from helpers import make_dashboard_context as _mk_ctx
from helpers import make_dashboard_session as _session
from helpers import make_dashboard_snapshot as _snapshot
from helpers import visible_dashboard_text as _visible

from uxon.tui.app import UxonApp
from uxon.tui.messages import _RefreshSourceLanded
from uxon.tui.refresh import SourceSpec, run_source
from uxon.tui.widgets.session_list_view import SessionListView


class SourceAndDrainTests(unittest.TestCase):
    def test_failure_success_and_empty_error_have_independent_diagnostic_results(self):
        def failure():
            raise OSError("peer connection failed")

        failed = run_source(SourceSpec("remote:peer", failure))
        healthy = run_source(SourceSpec("local", lambda: ["one", "two"]))

        def empty():
            raise ValueError()

        bare = run_source(SourceSpec("bare", empty))
        self.assertEqual(
            (failed.name, failed.value, failed.error),
            ("remote:peer", None, "peer connection failed"),
        )
        self.assertEqual(
            (healthy.name, healthy.value, healthy.error), ("local", ["one", "two"], None)
        )
        self.assertEqual((bare.name, bare.value, bare.error), ("bare", None, "ValueError"))
        self.assertTrue(all(result.elapsed_ms >= 0 for result in (failed, healthy, bare)))

    def test_control_exceptions_propagate_unchanged(self):
        import asyncio

        for error in (KeyboardInterrupt(), SystemExit(2), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):

                def interrupted(error=error):
                    raise error

                with self.assertRaises(type(error)) as caught:
                    run_source(SourceSpec("interrupted", interrupted))
                self.assertIs(caught.exception, error)

    def test_drain_cancels_every_active_stream_leaves_done_and_is_idempotent(self):
        from textual.worker import WorkerState

        class Handle:
            def __init__(self, state=WorkerState.RUNNING):
                self.state, self.cancelled = state, 0
                self.group = "test"

            def cancel(self):
                self.cancelled += 1
                self.state = WorkerState.CANCELLED

        app = UxonApp(_mk_ctx(), probe_agents=False)
        active = [Handle() for _ in range(5)]
        active[0].state = WorkerState.PENDING
        active[4].group = "cwd_writable"
        done = Handle(WorkerState.SUCCESS)
        coordinator = app._worker_coord
        coordinator._source_handles = {
            "main_ctx_rebuild": active[0],
            "remote:peer": active[1],
            "done": done,
        }
        coordinator._host_probe_handle, coordinator._link_health_handle = active[2:4]

        class Manager:
            def __iter__(self):
                return iter([active[4], done])

        # Textual owns cwd/action handles through its WorkerManager.
        with patch.object(UxonApp, "workers", new_callable=PropertyMock, return_value=Manager()):
            coordinator.drain(grace_seconds=0.05)
            coordinator.drain(grace_seconds=0.05)
        self.assertEqual([handle.cancelled for handle in active], [1] * 5)
        self.assertEqual(done.cancelled, 0)


class DispatchAndSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_and_unknown_preserve_canonical_state_valid_events_change_it(self):
        ctx = _mk_ctx()
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            table = app.screen.query_one("#sessions-dashboard", SessionListView)
            main = app.state.main
            remote = dict(app.state.remote)
            tick = app.state.refresh_tick
            before = _visible(table)
            fresh = replace(ctx, sessions=[_session(cpu="27.0")])
            for name, value in (("main_ctx_rebuild", fresh), ("remote:peer", _snapshot())):
                app.on__refresh_source_landed(
                    _RefreshSourceLanded(name, value, instance_epoch=app._instance_epoch + 1)
                )
            app.on__refresh_source_landed(_RefreshSourceLanded("unknown", fresh))
            await pilot.pause()
            self.assertIs(app.state.main, main)
            self.assertEqual(app.state.remote, remote)
            self.assertEqual(app.state.refresh_tick, tick)
            self.assertEqual(_visible(table), before)
            app.on__refresh_source_landed(
                _RefreshSourceLanded("main_ctx_rebuild", fresh, instance_epoch=app._instance_epoch)
            )
            app.on__refresh_source_landed(
                _RefreshSourceLanded("remote:peer", _snapshot(), instance_epoch=app._instance_epoch)
            )
            await pilot.pause()
            self.assertEqual(app.state.refresh_tick, tick + 1)
            self.assertEqual(app.state.main.sessions, tuple(fresh.sessions))
            self.assertIsNot(app.state.main, main)
            self.assertIn("27.0", _visible(table))
            self.assertEqual(app.state.remote["peer"].value.sessions, _snapshot().sessions)
            self.assertIsNotNone(app.state.remote["peer"].last_attempt_at)
            remote_slot = app.state.remote["peer"]
            availability = app.state.agent_availability
            second = replace(
                fresh,
                sessions=[_session(cpu="39.0")],
                total_cpu="39",
                total_ram="5G",
                cwd="/srv/second",
                cwd_short="second",
            )
            app.on__refresh_source_landed(
                _RefreshSourceLanded("main_ctx_rebuild", second, instance_epoch=app._instance_epoch)
            )
            await pilot.pause()
            self.assertEqual(app.state.refresh_tick, tick + 2)
            self.assertEqual(app.state.main.sessions, tuple(second.sessions))
            self.assertEqual(
                (
                    app.state.main.cwd,
                    app.state.main.cwd_short,
                    app.state.main.total_cpu,
                    app.state.main.total_ram,
                ),
                ("/srv/second", "second", "39", "5G"),
            )
            self.assertIn("39.0", _visible(table))
            valid = app.state.main
            for value, error in ((None, None), (fresh, "probe failed")):
                app.on__refresh_source_landed(
                    _RefreshSourceLanded(
                        "main_ctx_rebuild", value, error=error, instance_epoch=app._instance_epoch
                    )
                )
            await pilot.pause()
            self.assertIs(app.state.main, valid)
            self.assertEqual(app.state.refresh_tick, tick + 2)
            self.assertIs(app.state.remote["peer"], remote_slot)
            self.assertIs(app.state.agent_availability, availability)
            self.assertIn("39.0", _visible(table))

    async def test_mount_initial_kicks_interval_precedence_and_no_periodic_sources(self):
        calls = []

        def source(name, **kwargs):
            return SourceSpec(name, lambda: calls.append(name), **kwargs)

        specs = [
            source("legacy"),
            source("explicit", cadence_seconds=0.75),
            source("oneshot", cadence_seconds_attr=None),
            source("interval-only", cadence_seconds=0.5, kick_on_mount=False),
            source("invalid", cadence_seconds=-1),
            source("missing", cadence_seconds_attr="absent"),
        ]
        ctx = _mk_ctx(
            refresh_sources=specs,
            tui_refresh_interval_seconds=2.5,
            tui_ssh_refresh_interval_seconds=11.0,
        )
        app = UxonApp(ctx, probe_agents=False)
        timers = []
        async with app.run_test(size=(100, 30)) as pilot:
            self.assertEqual(calls, [], "loaded contexts must not kick on mount")
            ctx.loading = True
            with (
                patch.object(UxonApp, "is_headless", new_callable=PropertyMock, return_value=False),
                patch.object(app, "push_screen"),
                patch.object(
                    app,
                    "set_interval",
                    side_effect=lambda seconds, callback: timers.append((seconds, callback)),
                ),
                patch.dict(os.environ),
            ):
                os.environ.pop("PYTEST_CURRENT_TEST", None)
                app.on_mount()
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertCountEqual(calls, ["legacy", "explicit", "oneshot", "invalid", "missing"])
            self.assertEqual([interval for interval, _ in timers], [2.5, 0.75, 0.5, 11.0])
            calls.clear()
            for _, callback in timers[:-1]:
                callback()
            await app.workers.wait_for_complete()
            self.assertCountEqual(calls, ["legacy", "explicit", "interval-only"])
            calls.clear()
            for _, callback in timers[:-1]:
                callback()
            await app.workers.wait_for_complete()
            self.assertCountEqual(calls, ["legacy", "explicit", "interval-only"])

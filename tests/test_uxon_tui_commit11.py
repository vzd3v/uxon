"""Stage 8 commit 11 — dirty-flag coalescer + dashboard refresh.

Pinned contracts:

* ``MainScreen.loading`` is writable; no ``compute_loading`` method
  (which would mark the descriptor read-only per
  textual/reactive.py:330-333).
* Multiple per-host slot writes within one event-loop cycle collapse
  into a single dashboard refresh via the dirty-flag coalescer.
"""

from __future__ import annotations

import unittest
from unittest import mock


def _textual_available() -> bool:
    try:
        import textual  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(_textual_available(), "textual not installed")
class ReactiveReadOnlyTrapTests(unittest.TestCase):
    """``MainScreen.loading`` may not have a corresponding
    ``compute_loading`` method. Such a method marks the descriptor
    read-only and any later ``__set__`` raises AttributeError. The
    plan-mandated plain-assignment dispatcher pattern relies on this
    guarantee.
    """

    def test_main_screen_loading_has_no_compute(self) -> None:
        from uxon.tui.screens.main import MainScreen

        self.assertFalse(
            hasattr(MainScreen, "compute_loading"),
            "MainScreen.compute_loading must not exist — would make loading read-only.",
        )


@unittest.skipUnless(_textual_available(), "textual not installed")
class CoalescerTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_dirty_marks_collapse_to_one_drain(self) -> None:
        """The dirty-flag coalescer collapses N synchronous calls to
        ``_mark_remote_rows_dirty`` within one event-loop cycle into a
        single ``_drain_remote_rows`` invocation. Calling the
        coalescer directly (rather than relying on post_message
        ordering) keeps the assertion deterministic across xdist
        parallelism.
        """
        from uxon.remote_hosts import RemoteHost
        from uxon.tui.app import UxonApp
        from uxon.tui.context import TuiContext

        ctx = TuiContext(
            sessions=[],
            total_cpu="",
            total_ram="",
            version="",
            cwd="",
            cwd_short="",
            new_project_root="",
            existing_projects=[],
            remote_hosts=[
                RemoteHost(name="a", ssh_alias="a", description="", remote_uxon="uxon"),
                RemoteHost(name="b", ssh_alias="b", description="", remote_uxon="uxon"),
            ],
        )
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(80, 24)) as pilot:
            with mock.patch.object(UxonApp, "_drain_remote_rows", autospec=True) as drain_spy:
                # Two synchronous mark-dirty calls before any
                # event-loop tick advances.
                app._mark_remote_rows_dirty()
                app._mark_remote_rows_dirty()
                # The dirty flag flipped once, scheduling exactly
                # one drain via call_after_refresh.
                self.assertTrue(app._remote_rows_dirty)
                # Advance the refresh cycle so the drain runs.
                await pilot.pause()
                # Drain ran exactly once.
                self.assertEqual(drain_spy.call_count, 1)


@unittest.skipUnless(_textual_available(), "textual not installed")
class DashboardPerHostRepaintTests(unittest.TestCase):
    """``select_dashboard_model`` + ``diff`` preserve the per-host
    repaint invariant: a snapshot replacement on one host produces
    diff ops only for that host's rows; rows belonging to unchanged
    hosts produce zero ops.
    """

    def _state_with(self, snaps_by_host):
        from uxon.tui.slot_state import SlotState
        from uxon.tui.tui_state import TuiState

        state = TuiState()
        for name, snap in snaps_by_host.items():
            state.remote[name] = SlotState(value=snap, last_attempt_at=1.0)
        return state

    def _snap(self, host, sessions):
        from uxon.remote_collector import RemoteSnapshot

        return RemoteSnapshot(
            host_name=host,
            fetched_at_epoch=1.0,
            from_cache=False,
            error=None,
            sessions=sessions,
            cached_at_epoch=1.0,
        )

    def test_only_changed_host_produces_ops(self) -> None:
        from types import SimpleNamespace

        from uxon.remote_hosts import RemoteHost
        from uxon.tui.dashboard import model as dashboard_model
        from uxon.tui.dashboard.layout import LayoutFlags, build_active_columns
        from uxon.tui.dashboard.model import select_dashboard_model
        from uxon.tui.dashboard.reconcile import diff
        from uxon.tui.dashboard.ui_state import DashboardUiState
        from uxon.tui.slot_state import SlotState

        # Reset the model selector cache so this test stands alone.
        dashboard_model._LAST_OUTPUT = ()

        host_a = RemoteHost(name="a", ssh_alias="a", description="", remote_uxon="uxon")
        host_b = RemoteHost(name="b", ssh_alias="b", description="", remote_uxon="uxon")
        cfg = SimpleNamespace(remote_hosts=[host_a, host_b], current_user="u1")
        ui = DashboardUiState(sort_by="cpu")
        cols = build_active_columns(
            cfg_columns=None,
            flags=LayoutFlags(multi_host=True, cross_user=False),
        )

        state = self._state_with(
            {
                "a": self._snap("a", [{"user": "u1", "name": "a1", "short_id": "a1"}]),
                "b": self._snap("b", [{"user": "u1", "name": "b1", "short_id": "b1"}]),
            }
        )
        first = select_dashboard_model(state, cfg, ui)  # type: ignore[arg-type]

        # Replace only host A's slot with a different snapshot.
        state.remote["a"] = SlotState(
            value=self._snap("a", [{"user": "u1", "name": "a1-new", "short_id": "a1-new"}]),
            last_attempt_at=2.0,
        )
        second = select_dashboard_model(state, cfg, ui)  # type: ignore[arg-type]
        self.assertIsNot(first, second)

        ops = diff(first, second, cols).ops
        # Inspect ops: row_keys touched must all begin with "a/" —
        # rows belonging to host B (prefix "b/") are not touched.
        # ``_row_key`` formats as "<host>/<user>/<name>".
        self.assertGreater(len(ops), 0, "expected ops for the changed host")
        for op in ops:
            self.assertTrue(
                op.row_key.startswith("a/"),
                f"op {op!r} touched a non-host-a row",
            )


if __name__ == "__main__":
    unittest.main()

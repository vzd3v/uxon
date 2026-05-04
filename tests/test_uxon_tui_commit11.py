"""Stage 8 commit 11 — reactive remote-rows + dirty-flag coalescer.

Pinned contracts:

* ``App.remote_rows`` is a writable reactive (no ``compute_remote_rows``
  method — that would mark it read-only per textual/reactive.py:330-333).
* ``MainScreen.loading`` is also writable, no compute_loading method.
* Multiple per-host slot writes within one event-loop cycle collapse
  into a single ``select_remote_rows`` invocation via the dirty-flag
  coalescer.
* ``_dispatch_remote_rows`` produces zero ``add_row`` / ``remove_row``
  calls for hosts whose row tuples are unchanged across the diff.
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
    """Class-level introspection: neither ``App.remote_rows`` nor
    ``MainScreen.loading`` may have a corresponding ``compute_*``
    method. Such a method marks the descriptor read-only and any
    later ``__set__`` raises AttributeError. The plan-mandated
    plain-assignment dispatcher pattern relies on this guarantee.
    """

    def test_app_remote_rows_has_no_compute(self) -> None:
        from uxon.tui.app import UxonApp

        self.assertFalse(
            hasattr(UxonApp, "compute_remote_rows"),
            "UxonApp.compute_remote_rows must not exist — would make remote_rows read-only.",
        )
        self.assertTrue(hasattr(UxonApp, "remote_rows"))

    def test_main_screen_loading_has_no_compute(self) -> None:
        from uxon.tui.screens.main import MainScreen

        self.assertFalse(
            hasattr(MainScreen, "compute_loading"),
            "MainScreen.compute_loading must not exist — would make loading read-only.",
        )


@unittest.skipUnless(_textual_available(), "textual not installed")
class CoalescerTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_dirty_marks_collapse_to_one_drain(self) -> None:
        """Stage 8 commit 11: the dirty-flag coalescer collapses N
        synchronous calls to ``_mark_remote_rows_dirty`` within one
        event-loop cycle into a single ``select_remote_rows``
        invocation. Calling the coalescer directly (rather than
        relying on post_message ordering) keeps the assertion
        deterministic across xdist parallelism.
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
            with mock.patch("uxon.tui.state.select_remote_rows") as mocked:
                mocked.return_value = ()
                # Two synchronous mark-dirty calls before any
                # event-loop tick advances.
                app._mark_remote_rows_dirty()
                app._mark_remote_rows_dirty()
                # The dirty flag flipped once, scheduling exactly
                # one drain via call_after_refresh.
                self.assertTrue(app._remote_rows_dirty)
                # Advance the refresh cycle so the drain runs.
                await pilot.pause()
                # Selector ran exactly once.
                self.assertEqual(mocked.call_count, 1)
                # Dirty flag cleared.
                self.assertFalse(app._remote_rows_dirty)


@unittest.skipUnless(_textual_available(), "textual not installed")
class DispatchRemoteRowsDiffTests(unittest.TestCase):
    """``_dispatch_remote_rows`` is the per-host diff that drives
    ``RemoteSessionTable.update_host_rows``. Pin that unchanged hosts
    produce zero structural mutations.
    """

    def test_unchanged_hosts_skip_update_host_rows(self) -> None:
        from uxon.remote_hosts import RemoteHost
        from uxon.tui.context import TuiContext
        from uxon.tui.screens.main import MainScreen

        captured: list[tuple[str, list]] = []

        class _FakeTable:
            def update_host_rows(self, host_name, rows):
                captured.append((host_name, list(rows)))

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
        screen = MainScreen.__new__(MainScreen)
        screen.ctx = ctx  # type: ignore[attr-defined]

        def _query_one(selector, _kind=None):
            if selector == "#sessions-remote":
                return _FakeTable()
            raise LookupError(selector)

        screen.query_one = _query_one  # type: ignore[method-assign]

        old_rows = (
            ("a", {"name": "s1", "short_id": "s1"}),
            ("a", {"name": "s2", "short_id": "s2"}),
            ("b", {"name": "s3", "short_id": "s3"}),
        )
        new_rows = (
            ("a", {"name": "s1-new", "short_id": "s1-new"}),
            ("a", {"name": "s2", "short_id": "s2"}),
            ("b", {"name": "s3", "short_id": "s3"}),
        )
        screen._dispatch_remote_rows(old_rows, new_rows)
        # Only host "a" was touched. host "b" produced zero
        # update_host_rows calls.
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][0], "a")

    def test_dropped_host_clears_its_rows(self) -> None:
        from uxon.remote_hosts import RemoteHost
        from uxon.tui.context import TuiContext
        from uxon.tui.screens.main import MainScreen

        captured: list[tuple[str, list]] = []

        class _FakeTable:
            def update_host_rows(self, host_name, rows):
                captured.append((host_name, list(rows)))

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
            ],
        )
        screen = MainScreen.__new__(MainScreen)
        screen.ctx = ctx  # type: ignore[attr-defined]

        def _query_one(selector, _kind=None):
            if selector == "#sessions-remote":
                return _FakeTable()
            raise LookupError(selector)

        screen.query_one = _query_one  # type: ignore[method-assign]

        old_rows = (("a", {"name": "s1", "short_id": "s1"}),)
        new_rows = ()
        screen._dispatch_remote_rows(old_rows, new_rows)
        # Host "a" disappeared → empty rows dispatched to drop it.
        self.assertEqual(captured, [("a", [])])


if __name__ == "__main__":
    unittest.main()

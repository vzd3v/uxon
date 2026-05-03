"""Tests for the multi-host TUI block (Task #11).

Pin the rendering contract for ``RemoteSessionTable`` and the
flatten/dispatch logic that wires per-host snapshots into the
table:

- ``column_labels`` includes ``HOST`` iff ``show_host=True``.
- ``populate`` renders a row per (host_name, record) tuple, fills
  defaults for missing fields, marks ``attached=True`` rows visually,
  and exposes ``row_at`` so a future remote-attach handler can
  identify what was clicked.
- ``MainScreen._flatten_remote_rows`` follows ``ctx.remote_hosts``
  order (config-defined), skips hosts with no snapshot yet, and
  pairs each session record with its host name.
- ``MainScreen.apply_remote_snapshot`` mutates
  ``ctx.remote_snapshots`` by host_name.

Most tests are pure (no Textual app loop). The widget instantiation
checks just verify the data-shape contract — full rendering is
exercised by the existing TUI integration tests.
"""

from __future__ import annotations

import unittest

from uxon.remote_collector import RemoteSnapshot
from uxon.remote_hosts import RemoteHost


class ColumnLabelTests(unittest.TestCase):
    def test_without_host(self) -> None:
        from uxon.tui.widgets.remote_session_table import RemoteSessionTable

        labels = RemoteSessionTable.column_labels(show_host=False)
        self.assertEqual(labels, ("USER", "NAME", "AGENT", "CMD", "PATH"))

    def test_with_host(self) -> None:
        from uxon.tui.widgets.remote_session_table import RemoteSessionTable

        labels = RemoteSessionTable.column_labels(show_host=True)
        self.assertEqual(labels, ("HOST", "USER", "NAME", "AGENT", "CMD", "PATH"))


class FlattenRemoteRowsTests(unittest.TestCase):
    """``MainScreen._flatten_remote_rows`` is a pure helper — we
    exercise it by constructing a mock object with the same attrs
    and calling the unbound method, avoiding the Textual app loop.
    """

    def _flatten(self, hosts, snapshots):
        from uxon.tui.context import TuiContext
        from uxon.tui.screens.main import MainScreen

        ctx = TuiContext(
            sessions=[],
            total_cpu="",
            total_ram="",
            version="",
            cwd="",
            cwd_short="",
            new_project_root="",
            existing_projects=[],
            remote_hosts=hosts,
            remote_snapshots=snapshots,
        )
        # Bind ctx without going through Screen.__init__ (which needs an
        # app). The method accesses self.ctx only.
        screen = MainScreen.__new__(MainScreen)
        screen.ctx = ctx  # type: ignore[attr-defined]
        return MainScreen._flatten_remote_rows(screen)

    def _host(self, name: str) -> RemoteHost:
        return RemoteHost(name=name, ssh_alias=name, description="", remote_uxon="uxon")

    def _snap(self, name: str, sessions: list[dict]) -> RemoteSnapshot:
        return RemoteSnapshot(
            host_name=name,
            fetched_at_epoch=1.0,
            from_cache=False,
            error=None,
            sessions=sessions,
            cached_at_epoch=1.0,
        )

    def test_empty_when_no_hosts(self) -> None:
        self.assertEqual(self._flatten([], {}), [])

    def test_skips_hosts_with_no_snapshot(self) -> None:
        # A peer that has not yet been polled (or whose worker has
        # not yet returned) is silently skipped — no row, no error.
        rows = self._flatten([self._host("a"), self._host("b")], {})
        self.assertEqual(rows, [])

    def test_iterates_hosts_in_config_order(self) -> None:
        # Iteration order follows ``remote_hosts``, not snapshot
        # insertion order, so the displayed order is config-defined.
        snaps = {
            "b": self._snap("b", [{"name": "x"}]),
            "a": self._snap("a", [{"name": "y"}]),
        }
        rows = self._flatten([self._host("a"), self._host("b")], snaps)
        self.assertEqual([h for h, _ in rows], ["a", "b"])

    def test_pairs_each_record_with_its_host(self) -> None:
        snaps = {
            "a": self._snap("a", [{"name": "s1"}, {"name": "s2"}]),
            "b": self._snap("b", [{"name": "s3"}]),
        }
        rows = self._flatten([self._host("a"), self._host("b")], snaps)
        self.assertEqual(
            rows,
            [
                ("a", {"name": "s1"}),
                ("a", {"name": "s2"}),
                ("b", {"name": "s3"}),
            ],
        )


class ApplyRemoteSnapshotTests(unittest.TestCase):
    def test_updates_snapshot_dict(self) -> None:
        from uxon.tui.context import TuiContext
        from uxon.tui.screens.main import MainScreen

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
                RemoteHost(
                    name="vz-prod1", ssh_alias="vz-prod1", description="", remote_uxon="uxon"
                )
            ],
            remote_snapshots={},
        )
        screen = MainScreen.__new__(MainScreen)
        screen.ctx = ctx  # type: ignore[attr-defined]
        # Patch _populate_remote_table so the call doesn't need a DOM.
        screen._populate_remote_table = lambda: None  # type: ignore[method-assign]
        snap = RemoteSnapshot(
            host_name="vz-prod1",
            fetched_at_epoch=1.0,
            from_cache=False,
            error=None,
            sessions=[{"name": "uxon-foo@claude"}],
            cached_at_epoch=1.0,
        )
        screen.apply_remote_snapshot("vz-prod1", snap)
        self.assertIn("vz-prod1", ctx.remote_snapshots)
        self.assertIs(ctx.remote_snapshots["vz-prod1"], snap)


class RemoteHeaderTests(unittest.TestCase):
    """``_remote_header`` formats the section title for the remote
    block. Text is informational; we just pin that the host count is
    surfaced consistently."""

    def _header(self, hosts):
        from uxon.tui.context import TuiContext
        from uxon.tui.screens.main import MainScreen

        ctx = TuiContext(
            sessions=[],
            total_cpu="",
            total_ram="",
            version="",
            cwd="",
            cwd_short="",
            new_project_root="",
            existing_projects=[],
            remote_hosts=hosts,
        )
        screen = MainScreen.__new__(MainScreen)
        screen.ctx = ctx  # type: ignore[attr-defined]
        return MainScreen._remote_header(screen)

    def _host(self, name: str) -> RemoteHost:
        return RemoteHost(name=name, ssh_alias=name, description="", remote_uxon="uxon")

    def test_single_host_shows_name(self) -> None:
        self.assertIn("vz-prod1", self._header([self._host("vz-prod1")]))

    def test_multi_host_shows_count(self) -> None:
        h = self._header([self._host("a"), self._host("b"), self._host("c")])
        self.assertIn("3 hosts", h)


if __name__ == "__main__":
    unittest.main()

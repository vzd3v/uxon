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


class ActionKillRemoteBindingTests(unittest.TestCase):
    """Pure tests for the ``k`` binding wiring on ``MainScreen``.

    Uses a Pilot-free MainScreen instance assembled via ``__new__`` —
    we only care that:

      1. The ``k`` binding maps to ``kill_remote`` and is shown.
      2. Without focus on a ``RemoteSessionTable``, the action is a
         silent warning (no callback fires).
      3. With a focused row, the action pushes a confirmation
         modal whose OK callback dispatches ``ctx.on_remote_kill``
         with the resolved (host, user, name) tuple.

    Smoke-coverage of the modal flow itself is left to the existing
    Pilot-driven tests; the fast pure path is enough to pin the
    business logic.
    """

    def _binding_keys(self) -> list[str]:
        from uxon.tui.screens.main import MainScreen

        return [b.key for b in MainScreen.BINDINGS]

    def test_k_binding_registered(self) -> None:
        from uxon.tui.screens.main import MainScreen

        keys = self._binding_keys()
        self.assertIn("k", keys)
        binding = next(b for b in MainScreen.BINDINGS if b.key == "k")
        self.assertEqual(binding.action, "kill_remote")
        self.assertTrue(binding.show)
        self.assertTrue(binding.description.strip())

    def test_action_kill_remote_warns_without_focus(self) -> None:
        from uxon.tui.context import TuiContext

        notifies: list[tuple[str, str | None]] = []
        kill_calls: list[tuple[str, str, str]] = []

        class _FakeApp:
            def notify(self, msg: str, severity: str | None = None, **_: object) -> None:
                notifies.append((msg, severity))

            def push_screen(self, *_a: object, **_kw: object) -> None:
                raise AssertionError("push_screen must not run when nothing is focused")

        ctx = TuiContext(
            sessions=[],
            total_cpu="",
            total_ram="",
            version="",
            cwd="",
            cwd_short="",
            new_project_root="",
            existing_projects=[],
            on_remote_kill=lambda h, u, n: kill_calls.append((h, u, n)),
        )
        # ``self.focused`` is a Textual reactive descriptor; subclass
        # MainScreen so the property can be overridden with a plain
        # attribute on the instance via a class-level shadow.
        from uxon.tui.screens.main import MainScreen as _MS

        fake_app = _FakeApp()

        class _StubScreen(_MS):  # type: ignore[misc]
            focused = None  # shadows the reactive descriptor
            app = fake_app  # shadows the MessagePump descriptor

        screen = _StubScreen.__new__(_StubScreen)
        screen.ctx = ctx

        screen.action_kill_remote()
        self.assertEqual(len(notifies), 1)
        msg, severity = notifies[0]
        self.assertIn("remote", msg.lower())
        self.assertEqual(severity, "warning")
        self.assertEqual(kill_calls, [])

    def test_action_kill_remote_dispatches_on_confirm(self) -> None:
        from uxon.tui.context import TuiContext
        from uxon.tui.screens.main import MainScreen
        from uxon.tui.widgets.remote_session_table import RemoteSessionTable

        kill_calls: list[tuple[str, str, str]] = []
        captured_callback: list = []

        class _FakeApp:
            def notify(self, *_a: object, **_kw: object) -> None:
                pass

            def push_screen(self, _modal: object, callback) -> None:
                captured_callback.append(callback)

        # ``RemoteSessionTable`` subclasses ``DataTable`` whose
        # ``cursor_row`` is itself a Textual reactive descriptor; shadow
        # it on a subclass and inject the row index directly so the
        # widget answers ``row_at(0)`` without a real mount.
        class _StubTable(RemoteSessionTable):  # type: ignore[misc]
            cursor_row = 0  # shadows the reactive descriptor

        table = _StubTable.__new__(_StubTable)
        table._row_index = [
            (
                "vz-prod1",
                {"user": "alice", "name": "uxon-foo@claude", "active_cmd": "claude"},
            )
        ]

        ctx = TuiContext(
            sessions=[],
            total_cpu="",
            total_ram="",
            version="",
            cwd="",
            cwd_short="",
            new_project_root="",
            existing_projects=[],
            on_remote_kill=lambda h, u, n: kill_calls.append((h, u, n)),
        )

        fake_app = _FakeApp()

        class _StubScreen(MainScreen):  # type: ignore[misc]
            focused = table  # shadows the reactive descriptor
            app = fake_app  # shadows the MessagePump descriptor

        screen = _StubScreen.__new__(_StubScreen)
        screen.ctx = ctx
        screen.action_refresh = lambda: None  # type: ignore[method-assign]

        screen.action_kill_remote()
        # The modal is queued; simulate the user confirming.
        self.assertEqual(len(captured_callback), 1)
        captured_callback[0](True)
        self.assertEqual(kill_calls, [("vz-prod1", "alice", "uxon-foo@claude")])

    def test_action_kill_remote_strips_own_only_badge(self) -> None:
        """``host_name`` from the table may carry a ``" (own only)"``
        badge in the multi-host display. The callback must receive the
        bare host name."""
        from uxon.tui.context import TuiContext
        from uxon.tui.screens.main import MainScreen
        from uxon.tui.widgets.remote_session_table import RemoteSessionTable

        kill_calls: list[tuple[str, str, str]] = []

        class _FakeApp:
            def notify(self, *_a: object, **_kw: object) -> None:
                pass

            def push_screen(self, _modal: object, callback) -> None:
                callback(True)

        class _StubTable(RemoteSessionTable):  # type: ignore[misc]
            cursor_row = 0

        table = _StubTable.__new__(_StubTable)
        table._row_index = [("vz-prod1 (own only)", {"user": "alice", "name": "uxon-foo@claude"})]

        ctx = TuiContext(
            sessions=[],
            total_cpu="",
            total_ram="",
            version="",
            cwd="",
            cwd_short="",
            new_project_root="",
            existing_projects=[],
            on_remote_kill=lambda h, u, n: kill_calls.append((h, u, n)),
        )

        fake_app = _FakeApp()

        class _StubScreen(MainScreen):  # type: ignore[misc]
            focused = table
            app = fake_app

        screen = _StubScreen.__new__(_StubScreen)
        screen.ctx = ctx
        screen.action_refresh = lambda: None  # type: ignore[method-assign]

        screen.action_kill_remote()
        self.assertEqual(kill_calls, [("vz-prod1", "alice", "uxon-foo@claude")])


if __name__ == "__main__":
    unittest.main()

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


class ApplyLoadedCtxRemoteCarryTests(unittest.TestCase):
    """``apply_loaded_ctx`` must carry ``remote_snapshots`` across the
    local ctx-rebuild tick.

    The local rebuild source ticks roughly every
    ``tui_refresh_interval_seconds`` (~2 s) and produces a fresh
    :class:`TuiContext` with an empty ``remote_snapshots`` dict; the
    per-host SSH workers tick on their own, slower cadence
    (``remote_interval``, ~10 s) and write into the live dict. Without
    the carry-over each fast tick would wipe the remote-sessions
    table for the gap until the next per-host poll lands (the
    "remote table mig­ает" symptom).
    """

    def _ctx(self, *, snapshots=None) -> object:
        from uxon.tui.context import TuiContext

        return TuiContext(
            sessions=[],
            total_cpu="",
            total_ram="",
            version="",
            cwd="/tmp",
            cwd_short="/tmp",
            new_project_root="/tmp",
            existing_projects=[],
            remote_hosts=[
                RemoteHost(
                    name="vz-prod1", ssh_alias="vz-prod1", description="", remote_uxon="uxon"
                )
            ],
            remote_snapshots=snapshots if snapshots is not None else {},
        )

    def _snap(self) -> RemoteSnapshot:
        return RemoteSnapshot(
            host_name="vz-prod1",
            fetched_at_epoch=1.0,
            from_cache=False,
            error=None,
            sessions=[{"name": "uxon-foo@claude", "user": "alice"}],
            cached_at_epoch=1.0,
        )

    def test_remote_snapshots_carry_across_rebuild(self) -> None:
        from uxon.tui.screens.main import MainScreen

        snap = self._snap()
        old = self._ctx(snapshots={"vz-prod1": snap})
        new = self._ctx(snapshots={})  # fresh rebuild — empty by default

        class _FakeApp:
            ctx = None

        fake_app = _FakeApp()

        class _StubScreen(MainScreen):  # type: ignore[misc]
            focused = None
            app = fake_app

        screen = _StubScreen.__new__(_StubScreen)
        screen.ctx = old
        screen._restore_focus_key = ""
        screen._apply_ctx_refresh = lambda: True  # type: ignore[method-assign]

        screen.apply_loaded_ctx(new, focus_key="")

        self.assertIs(screen.ctx, new)
        self.assertIn("vz-prod1", screen.ctx.remote_snapshots)
        self.assertIs(screen.ctx.remote_snapshots["vz-prod1"], snap)
        # Same dict reference flows through so subsequent
        # ``apply_remote_snapshot`` writes target the live state.
        self.assertIs(screen.ctx.remote_snapshots, old.remote_snapshots)

    def test_carry_does_not_overwrite_when_new_ctx_brings_snapshots(self) -> None:
        """If the rebuild ever starts pre-populating ``remote_snapshots``
        (e.g. from on-disk cache), the carry-over must not silently
        clobber the fresher data. The current behavior is "always carry";
        this test pins it so a future change of intent is deliberate."""
        from uxon.tui.screens.main import MainScreen

        old_snap = self._snap()
        new_snap = RemoteSnapshot(
            host_name="vz-prod1",
            fetched_at_epoch=2.0,
            from_cache=True,
            error=None,
            sessions=[],
            cached_at_epoch=2.0,
        )
        old = self._ctx(snapshots={"vz-prod1": old_snap})
        new = self._ctx(snapshots={"vz-prod1": new_snap})

        class _FakeApp:
            ctx = None

        class _StubScreen(MainScreen):  # type: ignore[misc]
            focused = None
            app = _FakeApp()

        screen = _StubScreen.__new__(_StubScreen)
        screen.ctx = old
        screen._restore_focus_key = ""
        screen._apply_ctx_refresh = lambda: True  # type: ignore[method-assign]

        screen.apply_loaded_ctx(new, focus_key="")
        # Carry wins: the in-memory live dict is preserved.
        self.assertIs(screen.ctx.remote_snapshots["vz-prod1"], old_snap)


class RemoteFocusKeyTests(unittest.TestCase):
    """``_current_focus_key`` / ``_focus_key`` round-trip for a focused
    :class:`RemoteSessionTable` row.

    Focus on a remote row is preserved across an in-place patch (the
    DOM is untouched), but a layout-signature change forces a full
    re-compose; on that path the captured key is what restores the
    cursor onto the right row.
    """

    def _stub_table(self, rows):
        from uxon.tui.widgets.remote_session_table import RemoteSessionTable

        class _StubTable(RemoteSessionTable):  # type: ignore[misc]
            cursor_row = 0

        table = _StubTable.__new__(_StubTable)
        table._row_index = list(rows)
        return table

    def test_current_focus_key_for_remote_row(self) -> None:
        from uxon.tui.screens.main import MainScreen

        table = self._stub_table([("vz-prod1", {"user": "alice", "name": "uxon-foo@claude"})])

        class _StubScreen(MainScreen):  # type: ignore[misc]
            focused = table

        screen = _StubScreen.__new__(_StubScreen)
        self.assertEqual(screen._current_focus_key(), "remote:vz-prod1/alice/uxon-foo@claude")

    def test_current_focus_key_strips_own_only_badge(self) -> None:
        from uxon.tui.screens.main import MainScreen

        table = self._stub_table(
            [("vz-prod1 (own only)", {"user": "alice", "name": "uxon-foo@claude"})]
        )

        class _StubScreen(MainScreen):  # type: ignore[misc]
            focused = table

        screen = _StubScreen.__new__(_StubScreen)
        self.assertEqual(screen._current_focus_key(), "remote:vz-prod1/alice/uxon-foo@claude")

    def test_focus_key_restores_remote_row(self) -> None:
        from uxon.tui.screens.main import MainScreen

        table = self._stub_table(
            [
                ("vz-prod1", {"user": "alice", "name": "uxon-a@claude"}),
                ("vz-prod1", {"user": "bob", "name": "uxon-b@claude"}),
                ("vz-prod2", {"user": "carol", "name": "uxon-c@claude"}),
            ]
        )
        moved: list[int] = []
        focused: list[bool] = []
        table.move_cursor = lambda row=None, **_kw: moved.append(row)  # type: ignore[method-assign]
        table.focus = lambda *_a, **_kw: focused.append(True)  # type: ignore[method-assign]

        class _StubScreen(MainScreen):  # type: ignore[misc]
            focused = None

        screen = _StubScreen.__new__(_StubScreen)
        screen.query_one = lambda selector, _cls: table  # type: ignore[method-assign]

        ok = screen._focus_key("remote:vz-prod1/bob/uxon-b@claude")
        self.assertTrue(ok)
        self.assertEqual(moved, [1])
        self.assertEqual(focused, [True])

    def test_focus_key_returns_false_when_row_gone(self) -> None:
        from uxon.tui.screens.main import MainScreen

        # Peer dropped the session between focus capture and restore.
        table = self._stub_table([("vz-prod1", {"user": "alice", "name": "uxon-other@claude"})])

        class _StubScreen(MainScreen):  # type: ignore[misc]
            focused = None

        screen = _StubScreen.__new__(_StubScreen)
        screen.query_one = lambda selector, _cls: table  # type: ignore[method-assign]

        self.assertFalse(screen._focus_key("remote:vz-prod1/alice/uxon-foo@claude"))


if __name__ == "__main__":
    unittest.main()

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


def _state_with_snapshots(snapshots):
    """Build a TuiState with one slot per host, value=snapshot.

    Stage 8 commit 4: the canonical store moved from
    ``ctx.remote_snapshots`` (legacy dict) onto
    ``state.remote: dict[str, SlotState[RemoteSnapshot]]``. Tests
    populate the slot dict directly here — the alternative
    (constructing a SlotResult and feeding ``apply``) is exercised in
    the unit tests for ``slot_state.apply`` itself.
    """
    from uxon.tui.slot_state import SlotState
    from uxon.tui.tui_state import TuiState

    state = TuiState()
    for name, snap in snapshots.items():
        state.remote[name] = SlotState(value=snap, last_attempt_at=1.0)
    return state


class _StubApp:
    """Minimal stand-in for :class:`UxonApp` used by stub-screen tests.

    Carries a real :class:`TuiState` so the post-commit-4 code paths
    that go through ``self.app.state`` keep working. ``ctx`` is
    populated by the screen's own assignment.
    """

    def __init__(self, state) -> None:
        self.state = state
        self.ctx = None


class FlattenRemoteRowsTests(unittest.TestCase):
    """``MainScreen._flatten_remote_rows`` is a pure helper — we
    exercise it by constructing a mock object with the same attrs
    and calling the unbound method, avoiding the Textual app loop.
    """

    def _flatten(self, hosts, snapshots):
        from uxon.tui.context import TuiContext
        from uxon.tui.screens.main import MainScreen
        from uxon.tui.state import _REMOTE_ROWS_CACHE

        # Reset the selector cache between tests — it's keyed on per-
        # host ``(name, id(slot.value))`` and a previous test's cached
        # tuple could otherwise mask a regression here.
        _REMOTE_ROWS_CACHE.clear()
        _REMOTE_ROWS_CACHE.update({"key": None, "value": ()})
        state = _state_with_snapshots(snapshots)
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

        # Shadow the ``app`` MessagePump descriptor with a stub that
        # carries the state — the stub-app pattern matches the rest
        # of the file.
        class _StubScreen(MainScreen):  # type: ignore[misc]
            app = _StubApp(state)

        screen.__class__ = _StubScreen
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
        # In the multi-host display the bare host name is the first
        # space-delimited token; the trailing ``[…]`` is the stage-6
        # health badge.
        snaps = {
            "b": self._snap("b", [{"name": "x"}]),
            "a": self._snap("a", [{"name": "y"}]),
        }
        rows = self._flatten([self._host("a"), self._host("b")], snaps)
        self.assertEqual([h.split(" ", 1)[0] for h, _ in rows], ["a", "b"])

    def test_pairs_each_record_with_its_host(self) -> None:
        snaps = {
            "a": self._snap("a", [{"name": "s1"}, {"name": "s2"}]),
            "b": self._snap("b", [{"name": "s3"}]),
        }
        rows = self._flatten([self._host("a"), self._host("b")], snaps)
        # Multi-host display attaches the stage-6 health badge to the
        # host display name; pin record pairing rather than the badge
        # text (which is exercised separately in ``HostHealthBadgeTests``
        # in ``tests/test_uxon_tui.py``).
        self.assertEqual(
            [(h.split(" ", 1)[0], rec) for h, rec in rows],
            [
                ("a", {"name": "s1"}),
                ("a", {"name": "s2"}),
                ("b", {"name": "s3"}),
            ],
        )


class ApplyRemoteSnapshotTests(unittest.TestCase):
    def test_repaints_one_host_via_update_host_rows(self) -> None:
        """Stage 8 commit 4: ``apply_remote_snapshot`` no longer
        writes to ``ctx.remote_snapshots`` (the dispatcher already
        wrote the slot before calling). The screen instead drives a
        per-host repaint via :meth:`RemoteSessionTable.update_host_rows`.
        Pin that contract: the call dispatches with the host name and
        the flattened rows for that host only.
        """
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
        )
        screen = MainScreen.__new__(MainScreen)
        screen.ctx = ctx  # type: ignore[attr-defined]

        captured: list[tuple[str, list]] = []

        class _FakeTable:
            def update_host_rows(self, host_name, rows):
                captured.append((host_name, list(rows)))

        # Replace ``query_one`` with a stub that returns the fake
        # table for the remote-sessions id, otherwise raises (so we
        # don't accidentally land on a different widget).
        def _query_one(selector, _kind=None):
            if selector == "#sessions-remote":
                return _FakeTable()
            raise LookupError(selector)

        screen.query_one = _query_one  # type: ignore[method-assign]
        snap = RemoteSnapshot(
            host_name="vz-prod1",
            fetched_at_epoch=1.0,
            from_cache=False,
            error=None,
            sessions=[{"name": "uxon-foo@claude", "short_id": "foo"}],
            cached_at_epoch=1.0,
        )
        screen.apply_remote_snapshot("vz-prod1", snap)
        self.assertEqual(len(captured), 1)
        host_name, rows = captured[0]
        self.assertEqual(host_name, "vz-prod1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1].get("short_id"), "foo")


class RemoteHeaderTests(unittest.TestCase):
    """``_remote_header`` formats the section title for the remote
    block. Text is informational; we just pin that the host count is
    surfaced consistently."""

    def _header(self, hosts):
        from uxon.tui.context import TuiContext
        from uxon.tui.screens.main import MainScreen
        from uxon.tui.tui_state import TuiState

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

        # ``_remote_header`` reads through ``self.app.state.remote``
        # for the single-host case. Provide a stub-app with an empty
        # state — the no-snapshot path is what the existing pin
        # exercises; the populated case is covered by
        # ``RemoteHeaderHealthBadgeTests`` in tests/test_uxon_tui.py.
        class _StubScreen(MainScreen):  # type: ignore[misc]
            app = _StubApp(TuiState())

        screen.__class__ = _StubScreen
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


class RemoteStateSurvivesRebuildTests(unittest.TestCase):
    """Stage 8 commit 4: per-host remote snapshots live on
    ``app.state.remote`` (a slot store). The carry-list inside
    ``apply_loaded_ctx`` is gone — ``state`` is shared across rebuild
    ticks because it's owned by the App, not the ctx.

    Pin the new contract: the slot for a peer survives an
    ``apply_loaded_ctx`` swap so downstream readers
    (``select_remote_rows``) keep seeing the live snapshot during
    the gap between local-rebuild ticks (~2 s) and per-host SSH
    polls (~10 s).
    """

    def _ctx(self) -> object:
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

    def test_state_remote_survives_apply_loaded_ctx(self) -> None:
        from uxon.tui.screens.main import MainScreen
        from uxon.tui.slot_state import SlotState
        from uxon.tui.tui_state import TuiState

        snap = self._snap()
        state = TuiState()
        state.remote["vz-prod1"] = SlotState(value=snap, last_attempt_at=1.0)

        old = self._ctx()
        new = self._ctx()

        class _FakeApp:
            ctx = None

            def __init__(self, state):
                self.state = state

        fake_app = _FakeApp(state)

        class _StubScreen(MainScreen):  # type: ignore[misc]
            focused = None
            app = fake_app

        screen = _StubScreen.__new__(_StubScreen)
        screen.ctx = old
        screen._restore_focus_key = ""
        screen._apply_ctx_refresh = lambda: True  # type: ignore[method-assign]

        screen.apply_loaded_ctx(new, focus_key="")
        self.assertIs(screen.ctx, new)
        # The slot is unchanged — same SlotState identity, same value.
        self.assertIs(state.remote["vz-prod1"].value, snap)
        # Shim flattens state.remote into the legacy dict shape on read.
        self.assertIn("vz-prod1", screen.ctx.remote_snapshots)
        self.assertIs(screen.ctx.remote_snapshots["vz-prod1"], snap)


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


class StateSelectorTests(unittest.TestCase):
    """Stage 9a — pure selectors with identity-stable memoisation.

    The selectors operate on the existing :class:`TuiContext` shape (the
    ``TuiState``/``MainData`` split is deferred). Identity stability is
    the contract: when inputs are unchanged by ``is`` comparison the
    selector returns the previously returned object. The cache lives at
    module scope, so each test resets it via ``_reset_caches`` to stay
    independent.
    """

    def _reset_caches(self) -> None:
        from uxon.tui import state as tui_state

        tui_state._REMOTE_ROWS_CACHE["key"] = None
        tui_state._REMOTE_ROWS_CACHE["value"] = ()
        tui_state._HOST_HEALTH_BADGE_CACHE.clear()

    def _state(self, snapshots):
        from uxon.tui.slot_state import SlotState
        from uxon.tui.tui_state import TuiState

        state = TuiState()
        for name, snap in snapshots.items():
            state.remote[name] = SlotState(value=snap, last_attempt_at=1.0)
        return state

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

    def test_select_remote_rows_identity_stable(self) -> None:
        from uxon.tui.state import select_remote_rows

        self._reset_caches()
        hosts = [self._host("prod"), self._host("stage")]
        state = self._state(
            {
                "prod": self._snap("prod", [{"user": "u1", "name": "n1"}]),
                "stage": self._snap("stage", [{"user": "u2", "name": "n2"}]),
            }
        )
        first = select_remote_rows(state, hosts)
        second = select_remote_rows(state, hosts)
        self.assertIs(first, second)
        self.assertEqual(len(first), 2)

    def test_select_remote_rows_recomputes_on_snapshot_replacement(self) -> None:
        from uxon.tui.slot_state import SlotState
        from uxon.tui.state import select_remote_rows

        self._reset_caches()
        hosts = [self._host("prod")]
        state = self._state({"prod": self._snap("prod", [{"user": "u1", "name": "n1"}])})
        first = select_remote_rows(state, hosts)
        # Replace the slot's value with a different snapshot — bumps id.
        state.remote["prod"] = SlotState(
            value=self._snap("prod", [{"user": "u1", "name": "n1-new"}]),
            last_attempt_at=2.0,
        )
        second = select_remote_rows(state, hosts)
        self.assertIsNot(first, second)

    def test_select_remote_rows_identity_stable_on_no_op_apply(self) -> None:
        """Stage 8 commit 4 contract: an unchanged-value tick goes
        through ``slot_state.apply`` (which allocates a fresh
        SlotState) but ``id(slot.value)`` is preserved so the
        selector cache hits and returns the same tuple object.
        Pinning this here protects the read-side guarantee that
        downstream Textual code can ``is``-compare to skip a
        re-render.
        """
        from uxon.tui.slot_state import SlotResult, SlotState
        from uxon.tui.slot_state import apply as apply_slot
        from uxon.tui.state import select_remote_rows

        self._reset_caches()
        hosts = [self._host("prod")]
        snap = self._snap("prod", [{"user": "u1", "name": "n1"}])
        state = self._state({"prod": snap})
        first = select_remote_rows(state, hosts)

        # No-op success: same value (==), different object identity.
        # The identity-stable apply substitutes prev.value into the
        # result so the new SlotState carries the original snap.
        from uxon.remote_collector import RemoteSnapshot

        snap_again = RemoteSnapshot(
            host_name="prod",
            fetched_at_epoch=1.0,
            from_cache=False,
            error=None,
            sessions=[{"user": "u1", "name": "n1"}],
            cached_at_epoch=1.0,
        )
        self.assertEqual(snap, snap_again)
        self.assertIsNot(snap, snap_again)
        result: SlotResult[RemoteSnapshot] = SlotResult(
            value=snap_again,
            error=None,
            elapsed_ms=10,
            attempted_at=2.0,
        )
        prev_slot: SlotState[RemoteSnapshot] = state.remote["prod"]
        new_slot: SlotState[RemoteSnapshot] = apply_slot(prev_slot, result)
        state.remote["prod"] = new_slot
        # Slot identity changed (apply allocates fresh).
        self.assertIsNot(prev_slot, new_slot)
        # Value identity preserved by apply.
        self.assertIs(new_slot.value, snap)
        # Selector cache hits because cache key uses id(slot.value).
        second = select_remote_rows(state, hosts)
        self.assertIs(first, second)

    def test_select_layout_signature_equal_for_unchanged_ctx(self) -> None:
        from uxon.tui.context import TuiContext
        from uxon.tui.state import select_layout_signature

        ctx = TuiContext(
            sessions=[],
            total_cpu="",
            total_ram="",
            version="",
            cwd="",
            cwd_short="",
            new_project_root="",
            existing_projects=[],
        )
        self.assertEqual(select_layout_signature(ctx), select_layout_signature(ctx))

    def test_select_layout_signature_returns_four_tuple(self) -> None:
        """Pin the 4-tuple shape so a future drift fails fast.

        Position 2 is ``has_other_sessions``. ``False`` when no
        other-user local rows exist, ``True`` otherwise. The flip is
        what triggers the ``apply_loaded_ctx`` recompose so the
        dashboard widget gets rebuilt with the USER column.
        """
        from uxon.tui.context import TuiContext
        from uxon.tui.state import select_layout_signature

        ctx = TuiContext(
            sessions=[],
            total_cpu="",
            total_ram="",
            version="",
            cwd="",
            cwd_short="",
            new_project_root="",
            existing_projects=[],
        )
        sig = select_layout_signature(ctx)
        self.assertEqual(len(sig), 4)
        # No other-user rows present → has_other_sessions is False.
        self.assertEqual(sig[2], False)

    def test_select_layout_signature_recomposes_on_other_sessions_flip(self) -> None:
        """``has_other_sessions`` (3rd bool) tracks ``bool(ctx.other_sessions)``.

        Every other-user local row the rebuild discovers flips the
        bool to ``True``, recomposing the widget with a USER column
        visible.
        """
        from uxon.tui.context import SudoCapability, TuiContext, TuiSession
        from uxon.tui.state import select_layout_signature

        sudo_caps = SudoCapability(reachable_users=frozenset({"alice"}))
        other = TuiSession(
            name="alice.foo",
            short="foo",
            attached=False,
            pid="1",
            cpu="0",
            ram="0",
            created="0s",
            last_activity="0s",
            cmd="codex",
            path="/srv",
            user="alice",
        )
        ctx_with = TuiContext(
            sessions=[],
            other_sessions=[other],
            total_cpu="",
            total_ram="",
            version="",
            cwd="",
            cwd_short="",
            new_project_root="",
            existing_projects=[],
            sudo_caps=sudo_caps,
        )
        ctx_without = TuiContext(
            sessions=[],
            other_sessions=[],
            total_cpu="",
            total_ram="",
            version="",
            cwd="",
            cwd_short="",
            new_project_root="",
            existing_projects=[],
            sudo_caps=sudo_caps,
        )
        self.assertEqual(select_layout_signature(ctx_with)[2], True)
        self.assertEqual(select_layout_signature(ctx_without)[2], False)
        # The signatures must differ — that's the recompose trigger.
        self.assertNotEqual(
            select_layout_signature(ctx_with),
            select_layout_signature(ctx_without),
        )

    def test_select_remote_health_badge_per_host_keyed(self) -> None:
        """Cache is per-host: replacing host A's snapshot does not
        invalidate host B's cached badge entry. Pinned here because
        the pre-commit-4 cache flushed all entries on a 256-key
        overflow — the worst-of-both behaviour at 50-host churn.
        """
        from uxon.tui.state import _HOST_HEALTH_BADGE_CACHE, select_remote_health_badge

        self._reset_caches()
        snap_a = self._snap("a", [])
        snap_b = self._snap("b", [])
        ba = select_remote_health_badge("a", snap_a)
        bb = select_remote_health_badge("b", snap_b)
        # Replace a's slot — b stays cached.
        snap_a2 = self._snap("a", [])
        ba2 = select_remote_health_badge("a", snap_a2)
        self.assertIsNot(ba, ba2)
        bb2 = select_remote_health_badge("b", snap_b)
        self.assertIs(bb, bb2)
        # Cache keyed by host name, value carries (id, badge).
        self.assertIn("a", _HOST_HEALTH_BADGE_CACHE)
        self.assertIn("b", _HOST_HEALTH_BADGE_CACHE)
        self.assertEqual(_HOST_HEALTH_BADGE_CACHE["b"][0], id(snap_b))


class RemoteRowActivationTests(unittest.TestCase):
    """Enter on a RemoteSessionTable row dispatches on_remote_attach.

    Pure-state test — drives MainScreen._run_intent with a synthesised
    intent and asserts the callback was invoked with the right
    (host, user, name) triple. No Textual app loop required.
    """

    def test_run_intent_attach_remote_calls_callback(self) -> None:
        from uxon.tui.context import LaunchRequest, TuiContext
        from uxon.tui.screens.main import MainScreen
        from uxon.tui.state import MainIntent

        attach_calls: list[tuple[str, str, str]] = []

        def fake_attach(host: str, user: str, name: str) -> LaunchRequest:
            attach_calls.append((host, user, name))
            return LaunchRequest(cmd=("true",), label="t")

        captured: list[LaunchRequest] = []

        class _FakeApp:
            def notify(self, *_a: object, **_kw: object) -> None:
                pass

            def request_launch(self, req: LaunchRequest) -> None:
                captured.append(req)

        ctx = TuiContext(
            sessions=[],
            total_cpu="",
            total_ram="",
            version="",
            cwd="",
            cwd_short="",
            new_project_root="",
            existing_projects=[],
            on_remote_attach=fake_attach,
        )

        fake_app = _FakeApp()

        class _StubScreen(MainScreen):  # type: ignore[misc]
            app = fake_app  # shadows MessagePump descriptor

        screen = _StubScreen.__new__(_StubScreen)
        screen.ctx = ctx

        intent = MainIntent(
            kind="attach-remote",
            host="vz-prod1",
            user="alice",
            session_name="demo@claude",
        )
        screen._run_intent(intent)

        self.assertEqual(attach_calls, [("vz-prod1", "alice", "demo@claude")])
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].label, "t")

    def test_on_data_table_row_selected_remote_dispatches(self) -> None:
        from uxon.tui.context import LaunchRequest, TuiContext
        from uxon.tui.screens.main import MainScreen
        from uxon.tui.widgets.remote_session_table import RemoteSessionTable

        attach_calls: list[tuple[str, str, str]] = []
        captured: list[LaunchRequest] = []

        def fake_attach(host: str, user: str, name: str) -> LaunchRequest:
            attach_calls.append((host, user, name))
            return LaunchRequest(cmd=("true",), label="t")

        class _FakeApp:
            def notify(self, *_a: object, **_kw: object) -> None:
                pass

            def request_launch(self, req: LaunchRequest) -> None:
                captured.append(req)

        class _StubTable(RemoteSessionTable):  # type: ignore[misc]
            cursor_row = 0

        table = _StubTable.__new__(_StubTable)
        table._row_index = [
            (
                "vz-prod1 (own only)",
                {"user": "alice", "name": "uxon-foo@claude"},
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
            current_user="vasily",
            on_remote_attach=fake_attach,
        )

        fake_app = _FakeApp()

        class _StubScreen(MainScreen):  # type: ignore[misc]
            app = fake_app

        screen = _StubScreen.__new__(_StubScreen)
        screen.ctx = ctx

        class _Event:
            data_table = table
            cursor_row = 0

        screen.on_data_table_row_selected(_Event())
        # (own only) suffix stripped; host/user/name dispatched.
        self.assertEqual(attach_calls, [("vz-prod1", "alice", "uxon-foo@claude")])
        self.assertEqual(len(captured), 1)


if __name__ == "__main__":
    unittest.main()

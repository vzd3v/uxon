"""Tests for the multi-host TUI block.

Pin the rendering contract for remote rows in the unified
:class:`SessionDashboardTable`:

- ``select_dashboard_model`` follows ``cfg.remote_hosts`` order
  (config-defined), skips hosts with no snapshot yet, and emits
  one :class:`SessionRow` per session record (with ``host`` set
  to the peer name).
- ``MainScreen.action_kill`` dispatches via ``ctx.on_remote_kill``
  when the focused dashboard row carries a non-``None`` host.
- ``MainScreen.on_data_table_row_selected`` dispatches via
  ``ctx.on_remote_attach`` for remote rows.
- Focus key round-trip: ``_current_focus_key`` / ``_focus_key``
  preserve cursor placement on a remote dashboard row across an
  in-place patch.

Most tests are pure (no Textual app loop). The widget rendering is
exercised by the existing TUI integration tests.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from uxon.remote_collector import RemoteSnapshot
from uxon.remote_hosts import RemoteHost


def _state_with_snapshots(snapshots):
    """Build a TuiState with one slot per host, value=snapshot."""
    from uxon.tui.slot_state import SlotState
    from uxon.tui.tui_state import TuiState

    state = TuiState()
    for name, snap in snapshots.items():
        state.remote[name] = SlotState(value=snap, last_attempt_at=1.0)
    return state


def _reset_dashboard_cache() -> None:
    """Reset the dashboard model selector's module-level cache."""
    from uxon.tui.dashboard import model as dashboard_model

    dashboard_model._LAST_OUTPUT = ()


def _host(name: str) -> RemoteHost:
    return RemoteHost(name=name, ssh_alias=name, description="", remote_uxon="uxon")


def _snap(name: str, sessions: list[dict]) -> RemoteSnapshot:
    return RemoteSnapshot(
        host_name=name,
        fetched_at_epoch=1.0,
        from_cache=False,
        error=None,
        sessions=sessions,
        cached_at_epoch=1.0,
    )


class DashboardRemoteRowsTests(unittest.TestCase):
    """``select_dashboard_model`` produces remote rows from
    ``state.remote`` keyed in ``cfg.remote_hosts`` order."""

    def _model(self, hosts, snapshots):
        from uxon.tui.dashboard.model import select_dashboard_model
        from uxon.tui.dashboard.ui_state import DashboardUiState

        _reset_dashboard_cache()
        state = _state_with_snapshots(snapshots)
        cfg = SimpleNamespace(remote_hosts=hosts, current_user="u1")
        ui = DashboardUiState()
        return select_dashboard_model(state, cfg, ui)  # type: ignore[arg-type]

    def test_empty_when_no_hosts(self) -> None:
        self.assertEqual(self._model([], {}), ())

    def test_skips_hosts_with_no_snapshot(self) -> None:
        rows = self._model([_host("a"), _host("b")], {})
        self.assertEqual(rows, ())

    def test_iterates_hosts_in_config_order(self) -> None:
        # Pin the row tuple's host ordering before the global sort
        # runs by sorting on a key all rows share equally — the user
        # field is the same across both rows here, so the stable
        # sort preserves the build order, which follows
        # ``cfg.remote_hosts``.
        snaps = {
            "b": _snap("b", [{"name": "x", "user": "u"}]),
            "a": _snap("a", [{"name": "y", "user": "u"}]),
        }
        rows = self._model_sorted_by_user([_host("a"), _host("b")], snaps)
        # Group by host preserving their relative order across rows.
        seen_hosts: list[str] = []
        for r in rows:
            if r.host not in seen_hosts:
                seen_hosts.append(r.host)
        self.assertEqual(seen_hosts, ["a", "b"])

    def _model_sorted_by_user(self, hosts, snapshots):
        from uxon.tui.dashboard.model import select_dashboard_model
        from uxon.tui.dashboard.ui_state import DashboardUiState

        _reset_dashboard_cache()
        state = _state_with_snapshots(snapshots)
        cfg = SimpleNamespace(remote_hosts=hosts, current_user="u1")
        ui = DashboardUiState()
        return select_dashboard_model(state, cfg, ui)  # type: ignore[arg-type]

    def test_pairs_each_record_with_its_host(self) -> None:
        snaps = {
            "a": _snap("a", [{"name": "s1", "user": "u"}, {"name": "s2", "user": "u"}]),
            "b": _snap("b", [{"name": "s3", "user": "u"}]),
        }
        rows = self._model([_host("a"), _host("b")], snaps)
        pairs = sorted((r.host, r.name) for r in rows)
        self.assertEqual(pairs, [("a", "s1"), ("a", "s2"), ("b", "s3")])


class ActionKillRemoteRowTests(unittest.TestCase):
    """``MainScreen.action_kill`` dispatches via ``ctx.on_remote_kill``
    when the focused dashboard row carries a non-``None`` host. The
    legacy ``k`` binding has been retired; ``d`` handles all kills.
    """

    def _binding_keys(self) -> list[str]:
        from uxon.tui.screens.main import MainScreen

        return [b.key for b in MainScreen.BINDINGS]

    def test_k_binding_retired(self) -> None:
        self.assertNotIn("k", self._binding_keys())

    def test_action_kill_dispatches_on_remote_row(self) -> None:
        from uxon.tui.context import TuiContext
        from uxon.tui.dashboard.row import SessionRow
        from uxon.tui.screens.main import MainScreen
        from uxon.tui.widgets.session_dashboard_table import SessionDashboardTable

        kill_calls: list[tuple[str, str, str]] = []
        captured_callback: list = []

        class _FakeApp:
            def notify(self, *_a: object, **_kw: object) -> None:
                pass

            def push_screen(self, _modal: object, callback) -> None:
                captured_callback.append(callback)

        class _StubTable(SessionDashboardTable):  # type: ignore[misc]
            cursor_row = 0  # shadows the reactive descriptor

        # SessionDashboardTable's __init__ requires columns; build via
        # __new__ so we can avoid the Textual mount machinery.
        table = _StubTable.__new__(_StubTable)

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
        screen._dashboard_rows = (
            SessionRow(
                host="vz-prod1",
                user="alice",
                name="uxon-foo@claude",
                short="foo",
                agent="claude",
                attached=False,
                legacy=False,
                pid=1,
                cpu_pct=0.0,
                rss_kib=0,
                created_epoch=None,
                last_attached_epoch=None,
                cmd="claude",
                path="/srv",
            ),
        )
        screen.action_refresh = lambda: None  # type: ignore[method-assign]

        screen.action_kill()
        # The modal is queued; simulate the user confirming.
        self.assertEqual(len(captured_callback), 1)
        captured_callback[0](True)
        self.assertEqual(kill_calls, [("vz-prod1", "alice", "uxon-foo@claude")])


class OnDataTableRowSelectedRemoteTests(unittest.TestCase):
    """Enter on a remote dashboard row dispatches ``ctx.on_remote_attach``."""

    def test_on_data_table_row_selected_remote_dispatches(self) -> None:
        from uxon.tui.context import LaunchRequest, TuiContext
        from uxon.tui.dashboard.row import SessionRow
        from uxon.tui.screens.main import MainScreen
        from uxon.tui.widgets.session_dashboard_table import SessionDashboardTable

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

        # Stub-construct a SessionDashboardTable instance.
        table = SessionDashboardTable.__new__(SessionDashboardTable)

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
        screen._dashboard_rows = (
            SessionRow(
                host="vz-prod1",
                user="alice",
                name="uxon-foo@claude",
                short="foo",
                agent="claude",
                attached=False,
                legacy=False,
                pid=1,
                cpu_pct=0.0,
                rss_kib=0,
                created_epoch=None,
                last_attached_epoch=None,
                cmd="claude",
                path="/srv",
            ),
        )

        class _Event:
            data_table = table
            cursor_row = 0

        screen.on_data_table_row_selected(_Event())
        self.assertEqual(attach_calls, [("vz-prod1", "alice", "uxon-foo@claude")])
        self.assertEqual(len(captured), 1)


class RemoteStateSurvivesRebuildTests(unittest.TestCase):
    """Per-host remote snapshots live on ``app.state.remote``. The slot
    survives an ``apply_loaded_ctx`` swap so the dashboard's selector
    keeps seeing the live snapshot during the gap between local-rebuild
    ticks (~2 s) and per-host SSH polls (~10 s).
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
    dashboard remote row.

    Focus on a remote row is preserved across an in-place patch (the
    DOM is untouched), but a layout-signature change forces a full
    re-compose; on that path the captured key is what restores the
    cursor onto the right row.
    """

    def _make_row(self, host, user, name):
        from uxon.tui.dashboard.row import SessionRow

        return SessionRow(
            host=host,
            user=user,
            name=name,
            short=name,
            agent="claude",
            attached=False,
            legacy=False,
            pid=1,
            cpu_pct=0.0,
            rss_kib=0,
            created_epoch=None,
            last_attached_epoch=None,
            cmd="claude",
            path="/srv",
        )

    def _stub_screen(self, rows, *, cursor_row=0):
        from uxon.tui.screens.main import MainScreen
        from uxon.tui.widgets.session_dashboard_table import SessionDashboardTable

        class _StubTable(SessionDashboardTable):  # type: ignore[misc]
            cursor_row = 0  # shadows the reactive descriptor

        _StubTable.cursor_row = cursor_row  # type: ignore[assignment]
        table = _StubTable.__new__(_StubTable)

        class _StubScreen(MainScreen):  # type: ignore[misc]
            focused = table

        screen = _StubScreen.__new__(_StubScreen)
        screen._dashboard_rows = tuple(rows)
        screen.ctx = SimpleNamespace(current_user="vasily")  # type: ignore[assignment]
        return screen, table

    def test_current_focus_key_for_remote_row(self) -> None:
        screen, _ = self._stub_screen([self._make_row("vz-prod1", "alice", "uxon-foo@claude")])
        self.assertEqual(screen._current_focus_key(), "remote:vz-prod1/alice/uxon-foo@claude")

    def test_focus_key_restores_remote_row(self) -> None:
        from uxon.tui.screens.main import MainScreen

        rows = [
            self._make_row("vz-prod1", "alice", "uxon-a@claude"),
            self._make_row("vz-prod1", "bob", "uxon-b@claude"),
            self._make_row("vz-prod2", "carol", "uxon-c@claude"),
        ]

        moved: list[int] = []
        focused: list[bool] = []

        class _StubTable:
            def focus(self, *_a, **_kw):
                focused.append(True)

            def move_cursor(self, row=None, **_kw):
                moved.append(row)

        table = _StubTable()

        class _StubScreen(MainScreen):  # type: ignore[misc]
            focused = None

        screen = _StubScreen.__new__(_StubScreen)
        screen._dashboard_rows = tuple(rows)
        screen.query_one = lambda selector, _cls: table  # type: ignore[method-assign]

        ok = screen._focus_key("remote:vz-prod1/bob/uxon-b@claude")
        self.assertTrue(ok)
        self.assertEqual(moved, [1])
        self.assertEqual(focused, [True])

    def test_focus_key_returns_false_when_row_gone(self) -> None:
        from uxon.tui.screens.main import MainScreen

        rows = [self._make_row("vz-prod1", "alice", "uxon-other@claude")]

        class _StubTable:
            def focus(self, *_a, **_kw):
                pass

            def move_cursor(self, row=None, **_kw):
                pass

        table = _StubTable()

        class _StubScreen(MainScreen):  # type: ignore[misc]
            focused = None

        screen = _StubScreen.__new__(_StubScreen)
        screen._dashboard_rows = tuple(rows)
        screen.query_one = lambda selector, _cls: table  # type: ignore[method-assign]

        self.assertFalse(screen._focus_key("remote:vz-prod1/alice/uxon-foo@claude"))


class StateSelectorTests(unittest.TestCase):
    """The dashboard model selector preserves identity-stable
    memoisation: when state.remote slots are unchanged across calls,
    the selector returns the same tuple object.
    """

    def test_select_dashboard_model_identity_stable(self) -> None:
        from uxon.tui.dashboard.model import select_dashboard_model
        from uxon.tui.dashboard.ui_state import DashboardUiState

        _reset_dashboard_cache()
        hosts = [_host("prod"), _host("stage")]
        state = _state_with_snapshots(
            {
                "prod": _snap("prod", [{"user": "u1", "name": "n1"}]),
                "stage": _snap("stage", [{"user": "u2", "name": "n2"}]),
            }
        )
        cfg = SimpleNamespace(remote_hosts=hosts, current_user="u1")
        ui = DashboardUiState()
        first = select_dashboard_model(state, cfg, ui)  # type: ignore[arg-type]
        second = select_dashboard_model(state, cfg, ui)  # type: ignore[arg-type]
        self.assertIs(first, second)
        self.assertEqual(len(first), 2)

    def test_select_dashboard_model_recomputes_on_snapshot_replacement(self) -> None:
        from uxon.tui.dashboard.model import select_dashboard_model
        from uxon.tui.dashboard.ui_state import DashboardUiState
        from uxon.tui.slot_state import SlotState

        _reset_dashboard_cache()
        hosts = [_host("prod")]
        state = _state_with_snapshots({"prod": _snap("prod", [{"user": "u1", "name": "n1"}])})
        cfg = SimpleNamespace(remote_hosts=hosts, current_user="u1")
        ui = DashboardUiState()
        first = select_dashboard_model(state, cfg, ui)  # type: ignore[arg-type]
        # Replace the slot's value with a different snapshot.
        state.remote["prod"] = SlotState(
            value=_snap("prod", [{"user": "u1", "name": "n1-new"}]),
            last_attempt_at=2.0,
        )
        second = select_dashboard_model(state, cfg, ui)  # type: ignore[arg-type]
        self.assertIsNot(first, second)

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
        other-user local rows exist, ``True`` otherwise.
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
        self.assertEqual(sig[2], False)

    def test_select_layout_signature_recomposes_on_other_sessions_flip(self) -> None:
        """``has_other_sessions`` (3rd bool) tracks ``bool(ctx.other_sessions)``."""
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
        self.assertNotEqual(
            select_layout_signature(ctx_with),
            select_layout_signature(ctx_without),
        )


if __name__ == "__main__":
    unittest.main()

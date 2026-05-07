"""Tests for :mod:`uxon.tui.dashboard.model`.

The selector is pure: same inputs → equal outputs, and equal-by-content
inputs return the cached reference for ``is`` identity stability across
no-op rebuilds. The tests below pin both the structural contract
(filtering / sorting / cross-host fold) and the identity contract.

A small ``SimpleNamespace``-based fixture builder substitutes for the
heavy :class:`uxon.tui.config.TuiConfig` — the selector reads only
``cfg.remote_hosts`` (and each host's ``.name``) plus
``cfg.current_user`` is *not* read by the selector itself (that field
is consumed by the caller for ``cross_user``). Duck-typed config keeps
the fixtures small.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from uxon.remote_collector import RemoteSnapshot
from uxon.tui.context import ServerStatus, SudoCapability, TuiSession
from uxon.tui.dashboard import model as model_mod
from uxon.tui.dashboard.model import select_dashboard_model
from uxon.tui.dashboard.ui_state import DashboardUiState
from uxon.tui.main_data import MainData
from uxon.tui.slot_state import SlotState
from uxon.tui.tui_state import TuiState


def _session(
    *,
    name: str = "uxon-foo@claude",
    short: str | None = None,
    user: str = "alice",
    cpu: str = "5.0",
    ram: str = "100M",
    cmd: str = "claude",
    path: str = "/srv/foo",
    attached: bool = False,
    pid: str = "123",
) -> TuiSession:
    return TuiSession(
        name=name,
        short=short or name.split("uxon-")[-1],
        attached=attached,
        pid=pid,
        cpu=cpu,
        ram=ram,
        created="1m",
        last_activity="0m",
        cmd=cmd,
        path=path,
        user=user,
        stem=name.split("@")[0].removeprefix("uxon-"),
        agent="claude",
        legacy=False,
    )


def _main_data(
    *,
    sessions: tuple[TuiSession, ...] = (),
    other_sessions: tuple[TuiSession, ...] = (),
) -> MainData:
    return MainData(
        sessions=sessions,
        other_sessions=other_sessions,
        server_status=ServerStatus(),
        sudo_caps=SudoCapability(),
        scope_skipped_users=(),
        cwd="/srv/foo",
        cwd_short="foo",
        new_project_root="/srv",
        existing_projects=(),
        total_cpu="",
        total_ram="",
        version="3.3.0.dev0",
        repo_config_writable=True,
    )


def _wire_rec(
    *,
    name: str = "uxon-x@claude",
    user: str = "alice",
    cpu_pct: float = 9.9,
    cmd: str = "claude",
    path: str = "/",
) -> dict[str, Any]:
    return {
        "user": user,
        "name": name,
        "short_id": name.split("uxon-")[-1],
        "agent": "claude",
        "attached": False,
        "windows": "1",
        "created": "",
        "last_attached": "",
        "pane_pids": [],
        "active_pid": None,
        "active_cmd": cmd,
        "active_path": path,
        "cpu_pct": cpu_pct,
        "rss_kib": 1000,
        "legacy": False,
    }


def _snapshot(host: str, sessions: list[dict[str, Any]]) -> RemoteSnapshot:
    return RemoteSnapshot(
        host_name=host,
        fetched_at_epoch=0.0,
        from_cache=False,
        error=None,
        sessions=sessions,
    )


def _cfg(*, current_user: str = "alice", host_names: tuple[str, ...] = ()) -> Any:
    """Minimal duck-typed stand-in for TuiConfig.

    The selector reads ``cfg.remote_hosts`` and each host's ``.name``.
    Other TuiConfig fields are not touched here. Building a real
    TuiConfig requires ~20 callbacks; the SimpleNamespace shape keeps
    the fixtures focused.
    """
    hosts = tuple(SimpleNamespace(name=n) for n in host_names)
    return SimpleNamespace(current_user=current_user, remote_hosts=hosts)


class _ModelTestBase(unittest.TestCase):
    def setUp(self) -> None:
        # Reset the module-level identity cache between tests.
        model_mod._LAST_OUTPUT = ()

    def tearDown(self) -> None:
        model_mod._LAST_OUTPUT = ()


class IdentityStabilityTests(_ModelTestBase):
    def test_identity_stable_across_no_op_rebuild(self) -> None:
        ui = DashboardUiState()
        cfg = _cfg()
        state = TuiState(main=_main_data(sessions=(_session(),)))
        first = select_dashboard_model(state, cfg, ui)
        # Re-call: identical inputs, identical output by ``is``.
        second = select_dashboard_model(state, cfg, ui)
        self.assertIs(first, second)

        # Now rebuild a fresh-but-equal MainData: fresh TuiSession
        # instances with the same field values. SessionRow equality is
        # structural so the cached reference must come back.
        state2 = TuiState(main=_main_data(sessions=(_session(),)))
        third = select_dashboard_model(state2, cfg, ui)
        self.assertIs(first, third)

    def test_identity_stable_across_no_op_remote_landing(self) -> None:
        ui = DashboardUiState()
        cfg = _cfg(host_names=("h1",))
        state = TuiState()
        state.remote["h1"] = SlotState(value=_snapshot("h1", [_wire_rec()]))
        first = select_dashboard_model(state, cfg, ui)

        # Replace the slot with a structurally equal RemoteSnapshot.
        state.remote["h1"] = SlotState(value=_snapshot("h1", [_wire_rec()]))
        second = select_dashboard_model(state, cfg, ui)
        self.assertIs(first, second)

    def test_per_host_slot_replacement_only_changes_that_host(self) -> None:
        ui = DashboardUiState()
        cfg = _cfg(host_names=("h1", "h2"))
        state = TuiState()
        state.remote["h1"] = SlotState(value=_snapshot("h1", [_wire_rec(name="uxon-h1a@claude")]))
        state.remote["h2"] = SlotState(value=_snapshot("h2", [_wire_rec(name="uxon-h2a@claude")]))
        before = select_dashboard_model(state, cfg, ui)
        before_h2 = tuple(r for r in before if r.host == "h2")

        # Mutate host h1 only.
        state.remote["h1"] = SlotState(
            value=_snapshot(
                "h1",
                [_wire_rec(name="uxon-h1a@claude"), _wire_rec(name="uxon-h1b@claude")],
            )
        )
        after = select_dashboard_model(state, cfg, ui)
        self.assertNotEqual(before, after)
        after_h2 = tuple(r for r in after if r.host == "h2")
        self.assertEqual(before_h2, after_h2)

    def test_empty_filter_returns_full_model(self) -> None:
        ui = DashboardUiState(filter_text="")
        cfg = _cfg()
        state = TuiState(main=_main_data(sessions=(_session(), _session(name="uxon-bar@claude"))))
        first = select_dashboard_model(state, cfg, ui)

        # Reset cache, build again with filter_text="" → equality
        # holds, so the second call returns the cached reference.
        model_mod._LAST_OUTPUT = ()
        second = select_dashboard_model(state, cfg, ui)
        # Different reference (cache reset), but same content.
        self.assertEqual(first, second)


class SortingAndFilteringTests(_ModelTestBase):
    def test_cross_host_locals_first_then_cfg_order(self) -> None:
        ui = DashboardUiState()
        cfg = _cfg(host_names=("h1", "h2"))
        state = TuiState(main=_main_data(sessions=(_session(cpu="3.0"),)))
        state.remote["h1"] = SlotState(
            value=_snapshot(
                "h1",
                [
                    _wire_rec(name="uxon-low@claude", cpu_pct=1.0),
                    _wire_rec(name="uxon-high@claude", cpu_pct=99.0),
                ],
            )
        )
        state.remote["h2"] = SlotState(
            value=_snapshot(
                "h2",
                [_wire_rec(name="uxon-mid@claude", cpu_pct=50.0)],
            )
        )
        rows = select_dashboard_model(state, cfg, ui)
        # Locals form the leading block regardless of remote CPU.
        self.assertIsNone(rows[0].host)
        # Then h1 in cfg order, then h2. CPU is irrelevant to the sort
        # contract — only host priority + within-block recency matter.
        self.assertEqual([r.host for r in rows[1:3]], ["h1", "h1"])
        self.assertEqual(rows[3].host, "h2")
        self.assertEqual(rows[3].name, "uxon-mid@claude")

    def test_filter_substring_matches_name_and_user_by_default(self) -> None:
        cfg = _cfg()
        sess = (
            _session(name="uxon-foo@claude", user="alice", cmd="claude", path="/srv/foo"),
            _session(name="uxon-bar@claude", user="bob", cmd="codex", path="/srv/bar"),
            _session(name="uxon-baz@claude", user="carol", cmd="claude", path="/home/x"),
        )
        state = TuiState(main=_main_data(sessions=sess))

        # Filter on name fragment.
        rows = select_dashboard_model(state, cfg, DashboardUiState(filter_text="foo"))
        self.assertEqual([r.name for r in rows], ["uxon-foo@claude"])

        # Filter on user. Reset cache so the previous result doesn't
        # accidentally compare equal (it won't — but be explicit).
        model_mod._LAST_OUTPUT = ()
        rows = select_dashboard_model(state, cfg, DashboardUiState(filter_text="bob"))
        self.assertEqual([r.user for r in rows], ["bob"])

        # Case-insensitive name match.
        model_mod._LAST_OUTPUT = ()
        rows = select_dashboard_model(state, cfg, DashboardUiState(filter_text="BAR"))
        self.assertEqual([r.name for r in rows], ["uxon-bar@claude"])

    def test_filter_search_fields_extends_to_path_and_cmd(self) -> None:
        # Operators can opt into extra fields via cfg.tui_search_fields.
        cfg = SimpleNamespace(
            current_user="alice",
            remote_hosts=(),
            tui_search_fields=("name", "user", "path", "cmd"),
        )
        sess = (
            _session(name="uxon-foo@claude", user="alice", cmd="claude", path="/srv/foo"),
            _session(name="uxon-bar@claude", user="bob", cmd="codex", path="/srv/bar"),
            _session(name="uxon-baz@claude", user="carol", cmd="claude", path="/home/x"),
        )
        state = TuiState(main=_main_data(sessions=sess))

        rows = select_dashboard_model(state, cfg, DashboardUiState(filter_text="codex"))
        self.assertEqual([r.cmd for r in rows], ["codex"])
        model_mod._LAST_OUTPUT = ()
        rows = select_dashboard_model(state, cfg, DashboardUiState(filter_text="/home"))
        self.assertEqual([r.path for r in rows], ["/home/x"])

    def test_no_main_yields_only_remote_rows(self) -> None:
        cfg = _cfg(host_names=("h1",))
        state = TuiState()  # main=None
        state.remote["h1"] = SlotState(value=_snapshot("h1", [_wire_rec(name="uxon-x@claude")]))
        rows = select_dashboard_model(state, cfg, DashboardUiState())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].host, "h1")

    def test_filter_with_only_whitespace_treated_as_empty(self) -> None:
        cfg = _cfg()
        state = TuiState(main=_main_data(sessions=(_session(), _session(name="uxon-bar@claude"))))
        rows_blank = select_dashboard_model(state, cfg, DashboardUiState(filter_text="   "))
        model_mod._LAST_OUTPUT = ()
        rows_empty = select_dashboard_model(state, cfg, DashboardUiState(filter_text=""))
        self.assertEqual(rows_blank, rows_empty)
        self.assertEqual(len(rows_blank), 2)


if __name__ == "__main__":
    unittest.main()

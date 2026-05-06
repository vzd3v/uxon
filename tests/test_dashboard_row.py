"""Pure-data tests for :mod:`uxon.tui.dashboard.row`.

The two adapters bridge two different source shapes (``TuiSession``
with pre-formatted strings, wire-schema dicts with raw numeric and
ISO fields) into the unified :class:`SessionRow`. The invariants that
later action routing relies on:

* ``from_tui_session(...).host is None`` (always).
* ``from_wire_record(host, ...).host == host`` (never ``None``).
* Missing wire fields default to safe zeros, never raise.
* ISO timestamps parse to epoch once at adapter time.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from uxon.tui.context import TuiSession
from uxon.tui.dashboard.row import SessionRow, from_tui_session, from_wire_record


def _make_tui_session(**overrides: object) -> TuiSession:
    base = dict(
        name="cc-myproj",
        short="myproj",
        attached=False,
        pid="1234",
        cpu="12.3",
        ram="456M",
        created="10:00",
        last_activity="11:00",
        cmd="claude",
        path="/home/user/proj",
        user="alice",
        stem="myproj",
        agent="claude",
        legacy=False,
    )
    base.update(overrides)
    return TuiSession(**base)  # type: ignore[arg-type]


class FromTuiSessionTests(unittest.TestCase):
    def test_host_is_always_none(self) -> None:
        row = from_tui_session(_make_tui_session())
        self.assertIsNone(row.host)

    def test_all_fields_map(self) -> None:
        s = _make_tui_session(
            name="cc-foo",
            short="foo",
            attached=True,
            pid="4321",
            cpu="5.0",
            ram="1.2G",
            cmd="claude run",
            path="/tmp",
            user="bob",
            agent="codex",
            legacy=True,
        )
        row = from_tui_session(s)
        self.assertEqual(row.user, "bob")
        self.assertEqual(row.name, "cc-foo")
        self.assertEqual(row.short, "foo")
        self.assertEqual(row.agent, "codex")
        self.assertTrue(row.attached)
        self.assertTrue(row.legacy)
        self.assertEqual(row.pid, 4321)
        self.assertAlmostEqual(row.cpu_pct, 5.0)
        # 1.2 GiB → 1.2 * 1024 * 1024 KiB.
        self.assertEqual(row.rss_kib, int(1.2 * 1024 * 1024))
        self.assertEqual(row.cmd, "claude run")
        self.assertEqual(row.path, "/tmp")

    def test_pid_dash_or_empty_becomes_none(self) -> None:
        self.assertIsNone(from_tui_session(_make_tui_session(pid="-")).pid)
        self.assertIsNone(from_tui_session(_make_tui_session(pid="")).pid)
        self.assertIsNone(from_tui_session(_make_tui_session(pid="abc")).pid)

    def test_cpu_dash_or_empty_becomes_zero(self) -> None:
        self.assertEqual(from_tui_session(_make_tui_session(cpu="-")).cpu_pct, 0.0)
        self.assertEqual(from_tui_session(_make_tui_session(cpu="")).cpu_pct, 0.0)
        self.assertEqual(from_tui_session(_make_tui_session(cpu="bogus")).cpu_pct, 0.0)

    def test_ram_parses_short_and_long_units(self) -> None:
        self.assertEqual(from_tui_session(_make_tui_session(ram="456M")).rss_kib, 456 * 1024)
        self.assertEqual(
            from_tui_session(_make_tui_session(ram="456 MiB")).rss_kib,
            456 * 1024,
        )
        self.assertEqual(from_tui_session(_make_tui_session(ram="42K")).rss_kib, 42)
        self.assertEqual(from_tui_session(_make_tui_session(ram="-")).rss_kib, 0)
        self.assertEqual(from_tui_session(_make_tui_session(ram="")).rss_kib, 0)
        self.assertEqual(from_tui_session(_make_tui_session(ram="garbage")).rss_kib, 0)

    def test_local_epochs_are_none(self) -> None:
        row = from_tui_session(_make_tui_session())
        self.assertIsNone(row.created_epoch)
        self.assertIsNone(row.last_attached_epoch)

    def test_legacy_flag_preserved(self) -> None:
        self.assertFalse(from_tui_session(_make_tui_session(legacy=False)).legacy)
        self.assertTrue(from_tui_session(_make_tui_session(legacy=True)).legacy)


def _wire(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "user": "alice",
        "name": "cc-myproj",
        "short_id": "myproj",
        "agent": "claude",
        "attached": False,
        "windows": "1",
        "created": "2026-05-03T12:34:56+00:00",
        "last_attached": "2026-05-03T13:00:00+00:00",
        "pane_pids": [1234],
        "active_pid": 1234,
        "active_cmd": "claude",
        "active_path": "/home/alice/proj",
        "cpu_pct": 12.3,
        "rss_kib": 4096,
        "legacy": False,
    }
    base.update(overrides)
    return base


class FromWireRecordTests(unittest.TestCase):
    def test_host_always_set_to_peer(self) -> None:
        row = from_wire_record("peer-1", _wire())
        self.assertEqual(row.host, "peer-1")
        self.assertIsNotNone(row.host)

    def test_all_fields_map(self) -> None:
        row = from_wire_record("peer-1", _wire(attached=True, legacy=True))
        self.assertEqual(row.user, "alice")
        self.assertEqual(row.name, "cc-myproj")
        self.assertEqual(row.short, "myproj")
        self.assertEqual(row.agent, "claude")
        self.assertTrue(row.attached)
        self.assertTrue(row.legacy)
        self.assertEqual(row.pid, 1234)
        self.assertAlmostEqual(row.cpu_pct, 12.3)
        self.assertEqual(row.rss_kib, 4096)
        self.assertEqual(row.cmd, "claude")
        self.assertEqual(row.path, "/home/alice/proj")

    def test_iso_timestamps_parse_to_epoch(self) -> None:
        row = from_wire_record("peer-1", _wire())
        expected_created = datetime(2026, 5, 3, 12, 34, 56, tzinfo=UTC).timestamp()
        expected_last = datetime(2026, 5, 3, 13, 0, 0, tzinfo=UTC).timestamp()
        self.assertIsNotNone(row.created_epoch)
        self.assertIsNotNone(row.last_attached_epoch)
        assert row.created_epoch is not None  # for type-narrowing
        assert row.last_attached_epoch is not None
        self.assertAlmostEqual(row.created_epoch, expected_created, places=3)
        self.assertAlmostEqual(row.last_attached_epoch, expected_last, places=3)

    def test_empty_iso_strings_yield_none_epoch(self) -> None:
        row = from_wire_record("peer-1", _wire(created="", last_attached=""))
        self.assertIsNone(row.created_epoch)
        self.assertIsNone(row.last_attached_epoch)

    def test_unparseable_iso_yields_none_epoch(self) -> None:
        row = from_wire_record("peer-1", _wire(created="not-a-date"))
        self.assertIsNone(row.created_epoch)

    def test_short_id_falls_back_to_name(self) -> None:
        row = from_wire_record("peer-1", _wire(short_id=""))
        self.assertEqual(row.short, "cc-myproj")

    def test_missing_active_pid_is_none(self) -> None:
        row = from_wire_record("peer-1", _wire(active_pid=None))
        self.assertIsNone(row.pid)

    def test_missing_fields_default_to_zero(self) -> None:
        # Older peer schema: half the fields absent.
        sparse: dict[str, object] = {"user": "u", "name": "n"}
        row = from_wire_record("peer-old", sparse)
        self.assertEqual(row.host, "peer-old")
        self.assertEqual(row.user, "u")
        self.assertEqual(row.name, "n")
        self.assertEqual(row.short, "n")  # falls back to name
        self.assertEqual(row.agent, "")
        self.assertFalse(row.attached)
        self.assertFalse(row.legacy)
        self.assertIsNone(row.pid)
        self.assertEqual(row.cpu_pct, 0.0)
        self.assertEqual(row.rss_kib, 0)
        self.assertIsNone(row.created_epoch)
        self.assertIsNone(row.last_attached_epoch)
        self.assertEqual(row.cmd, "")
        self.assertEqual(row.path, "")

    def test_legacy_flag_preserved(self) -> None:
        self.assertTrue(from_wire_record("p", _wire(legacy=True)).legacy)
        self.assertFalse(from_wire_record("p", _wire(legacy=False)).legacy)


class FrozenRowTests(unittest.TestCase):
    def test_session_row_is_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        row = from_tui_session(_make_tui_session())
        with self.assertRaises(FrozenInstanceError):
            row.host = "peer"  # type: ignore[misc]

    def test_session_row_equality_is_structural(self) -> None:
        a = SessionRow(
            host=None,
            user="u",
            name="n",
            short="n",
            agent="claude",
            attached=False,
            legacy=False,
            pid=None,
            cpu_pct=0.0,
            rss_kib=0,
            created_epoch=None,
            last_attached_epoch=None,
            cmd="",
            path="",
        )
        b = SessionRow(
            host=None,
            user="u",
            name="n",
            short="n",
            agent="claude",
            attached=False,
            legacy=False,
            pid=None,
            cpu_pct=0.0,
            rss_kib=0,
            created_epoch=None,
            last_attached_epoch=None,
            cmd="",
            path="",
        )
        self.assertEqual(a, b)
        self.assertIsNot(a, b)


if __name__ == "__main__":
    unittest.main()

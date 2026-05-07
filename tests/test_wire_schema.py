"""Tests for ``uxon.wire_schema``.

Pins down the wire contract that ``--json`` (next commit) and the
multi-host remote collector (later commits) both depend on. The
tests must fail loudly if the field set, types, or
prefix-strip / attached-conversion semantics drift.

These tests are pure: no tmux, no subprocess, no Textual. They use
the real :class:`uxon.cli.SessionInfo` dataclass as input so they
also catch a SessionInfo field rename that breaks the protocol.
"""

from __future__ import annotations

import json
import unittest

from uxon.cli import SessionInfo
from uxon.wire_schema import (
    WIRE_SCHEMA_VERSION,
    SessionRecord,
    build_session_records,
)


def _make_session(**overrides: object) -> SessionInfo:
    base: dict[str, object] = {
        "user": "alice",
        "name": "uxon-foo@claude",
        "attached": "0",
        "windows": "1",
        "created": "2026-05-03T12:00:00+00:00",
        "last_attached": "2026-05-03T12:30:00+00:00",
        "pane_pids": (111, 222),
        "active_pid": 111,
        "active_cmd": "claude",
        "active_path": "/home/alice/proj",
        "cpu_pct": 1.5,
        "rss_kib": 4096,
        "agent": "claude",
        "legacy": False,
    }
    base.update(overrides)
    return SessionInfo(**base)  # type: ignore[arg-type]


class WireSchemaVersionTests(unittest.TestCase):
    def test_version_is_a_short_string(self) -> None:
        # The version is shipped over the wire; it must be a plain
        # string and short enough to be human-grep-able in transcripts.
        self.assertIsInstance(WIRE_SCHEMA_VERSION, str)
        self.assertTrue(WIRE_SCHEMA_VERSION)
        self.assertLess(len(WIRE_SCHEMA_VERSION), 16)


class BuildSessionRecordsTests(unittest.TestCase):
    def test_empty_input_returns_empty_list(self) -> None:
        self.assertEqual(build_session_records([], session_prefix="uxon-"), [])

    def test_single_session_round_trip_fields(self) -> None:
        s = _make_session()
        [rec] = build_session_records([s], session_prefix="uxon-")
        self.assertEqual(rec["user"], "alice")
        self.assertEqual(rec["name"], "uxon-foo@claude")
        self.assertEqual(rec["short_id"], "foo@claude")
        self.assertEqual(rec["agent"], "claude")
        self.assertFalse(rec["attached"])
        self.assertEqual(rec["windows"], "1")
        self.assertEqual(rec["created"], "2026-05-03T12:00:00+00:00")
        self.assertEqual(rec["last_attached"], "2026-05-03T12:30:00+00:00")
        self.assertEqual(rec["pane_pids"], [111, 222])
        self.assertEqual(rec["active_pid"], 111)
        self.assertEqual(rec["active_cmd"], "claude")
        self.assertEqual(rec["active_path"], "/home/alice/proj")
        self.assertEqual(rec["cpu_pct"], 1.5)
        self.assertEqual(rec["rss_kib"], 4096)
        self.assertFalse(rec["legacy"])

    def test_attached_string_converted_to_bool(self) -> None:
        attached = build_session_records([_make_session(attached="1")], session_prefix="uxon-")
        self.assertTrue(attached[0]["attached"])
        # Anything that is not literally "1" must be False — the tmux
        # format emits "1"/"0", but defensive: blank strings, "true",
        # etc., must not flip the bit.
        for raw in ("0", "", "true", "yes", "2"):
            recs = build_session_records([_make_session(attached=raw)], session_prefix="uxon-")
            self.assertFalse(recs[0]["attached"], f"attached={raw!r} unexpectedly mapped to True")

    def test_short_id_strips_current_prefix_only(self) -> None:
        # Current prefix on the session: gets stripped.
        modern = _make_session(name="uxon-bar@codex")
        [m] = build_session_records([modern], session_prefix="uxon-")
        self.assertEqual(m["short_id"], "bar@codex")

        # Legacy-prefix session: name does NOT start with current
        # prefix, so short_id is the full name verbatim. Mirrors
        # print_list, which displays legacy sessions un-stripped so
        # they are visually distinguishable.
        legacy = _make_session(name="uxon_baz@claude", legacy=True)
        [r] = build_session_records([legacy], session_prefix="uxon-")
        self.assertEqual(r["short_id"], "uxon_baz@claude")
        self.assertEqual(r["name"], "uxon_baz@claude")
        self.assertTrue(r["legacy"])

    def test_pane_pids_tuple_becomes_list(self) -> None:
        # JSON has no tuples; the wire form must use a list so
        # ``json.dumps`` works without a custom encoder.
        s = _make_session(pane_pids=(1, 2, 3))
        [rec] = build_session_records([s], session_prefix="uxon-")
        self.assertEqual(rec["pane_pids"], [1, 2, 3])
        self.assertIsInstance(rec["pane_pids"], list)

    def test_empty_strings_passed_through_not_substituted(self) -> None:
        # ``print_list`` substitutes empty ``active_cmd`` / ``active_path``
        # with ``"-"`` at render time. The wire form must NOT do that —
        # ``"-"`` is a display artefact, the data layer ships the empty
        # string and lets each consumer apply its own placeholder.
        s = _make_session(active_cmd="", active_path="")
        [rec] = build_session_records([s], session_prefix="uxon-")
        self.assertEqual(rec["active_cmd"], "")
        self.assertEqual(rec["active_path"], "")

    def test_active_pid_none_preserved(self) -> None:
        s = _make_session(active_pid=None)
        [rec] = build_session_records([s], session_prefix="uxon-")
        self.assertIsNone(rec["active_pid"])

    def test_record_is_json_serialisable(self) -> None:
        # The whole point of the wire schema: a record must round-trip
        # through json.dumps/loads without losing fields or types.
        s = _make_session(active_pid=None, attached="1")
        [rec] = build_session_records([s], session_prefix="uxon-")
        encoded = json.dumps(rec)
        decoded = json.loads(encoded)
        self.assertEqual(decoded, rec)

    def test_order_preserved(self) -> None:
        a = _make_session(name="uxon-a@claude")
        b = _make_session(name="uxon-b@claude")
        c = _make_session(name="uxon-c@claude")
        recs = build_session_records([a, b, c], session_prefix="uxon-")
        self.assertEqual([r["short_id"] for r in recs], ["a@claude", "b@claude", "c@claude"])

    def test_record_fields_are_a_fixed_set(self) -> None:
        # Pin the field set. Adding a field is intentional and must
        # also bump the optional-field documentation; this test
        # forces the change to be noticed.
        rec = build_session_records([_make_session()], session_prefix="uxon-")[0]
        expected_keys = {
            "user",
            "name",
            "short_id",
            "agent",
            "attached",
            "windows",
            "created",
            "last_attached",
            "pane_pids",
            "active_pid",
            "active_cmd",
            "active_path",
            "cpu_pct",
            "rss_kib",
            "legacy",
        }
        self.assertEqual(set(rec.keys()), expected_keys)
        self.assertEqual(set(SessionRecord.__annotations__.keys()), expected_keys)


if __name__ == "__main__":
    unittest.main()

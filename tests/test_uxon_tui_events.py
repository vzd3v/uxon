"""Tests for the debug-logging channel in ``uxon.tui.events``.

The user-facing ``_log_event`` is exercised end-to-end by the TUI
integration suite; this module covers ``debug()`` — the off-by-default
diagnostic channel gated on ``UXON_DEBUG``.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from uxon.tui import events


class DebugChannelTests(unittest.TestCase):
    def setUp(self) -> None:
        # Save and restore module-level topic state so tests don't bleed.
        self._saved = events._DEBUG_TOPICS

    def tearDown(self) -> None:
        events._DEBUG_TOPICS = self._saved

    def test_no_op_when_unset(self) -> None:
        events._DEBUG_TOPICS = frozenset()
        # Must not raise, must not write — no log dir override needed.
        events.debug("refresh", action="x")  # smoke

    def test_writes_when_topic_enabled(self) -> None:
        with mock.patch.dict(os.environ, {"USER": "tester"}, clear=False):
            events._DEBUG_TOPICS = frozenset({"refresh"})
            with mock.patch("uxon.tui.events._log_dir") as log_dir:
                with self._tmp_log_dir(log_dir) as tmp:
                    events.debug("refresh", at="worker", elapsed_ms=42)
                    line = self._read_only_line(tmp)
            data = json.loads(line)
            self.assertEqual(data["topic"], "refresh")
            self.assertEqual(data["at"], "worker")
            self.assertEqual(data["elapsed_ms"], 42)
            self.assertIn("ts", data)

    def test_topic_filter_drops_other_topics(self) -> None:
        with mock.patch.dict(os.environ, {"USER": "tester"}, clear=False):
            events._DEBUG_TOPICS = frozenset({"refresh"})
            with mock.patch("uxon.tui.events._log_dir") as log_dir:
                with self._tmp_log_dir(log_dir) as tmp:
                    events.debug("probe", at="x")
                    self.assertEqual(os.listdir(tmp), [])

    def test_wildcard_topic_writes_everything(self) -> None:
        with mock.patch.dict(os.environ, {"USER": "tester"}, clear=False):
            events._DEBUG_TOPICS = frozenset({"*"})
            with mock.patch("uxon.tui.events._log_dir") as log_dir:
                with self._tmp_log_dir(log_dir) as tmp:
                    events.debug("anything", value=1)
                    line = self._read_only_line(tmp)
            self.assertEqual(json.loads(line)["topic"], "anything")

    def test_logging_failures_are_swallowed(self) -> None:
        events._DEBUG_TOPICS = frozenset({"*"})
        # Force makedirs to succeed but open() to fail — call must not raise.
        with mock.patch("uxon.tui.events._log_dir", return_value="/no/such/path/exists"):
            events.debug("refresh", x=1)  # silent on PermissionError / FileNotFoundError

    # ── helpers ──
    def _tmp_log_dir(self, log_dir_mock: mock.MagicMock):
        import tempfile
        from contextlib import contextmanager

        @contextmanager
        def ctx():
            with tempfile.TemporaryDirectory() as tmp:
                log_dir_mock.return_value = tmp
                yield tmp

        return ctx()

    def _read_only_line(self, tmp: str) -> str:
        files = os.listdir(tmp)
        self.assertEqual(len(files), 1, f"expected one log file, got {files}")
        with open(os.path.join(tmp, files[0]), encoding="utf-8") as fh:
            lines = fh.readlines()
        self.assertEqual(len(lines), 1)
        return lines[0]


class DebugTopicParserTests(unittest.TestCase):
    def test_unset_is_empty(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UXON_DEBUG", None)
            self.assertEqual(events._parse_debug_topics(), frozenset())

    def test_truthy_aliases_become_wildcard(self) -> None:
        for raw in ("1", "true", "all", "*", "yes", "on", "TRUE", "ON"):
            with mock.patch.dict(os.environ, {"UXON_DEBUG": raw}):
                self.assertEqual(events._parse_debug_topics(), frozenset({"*"}))

    def test_comma_list_parses_topics(self) -> None:
        with mock.patch.dict(os.environ, {"UXON_DEBUG": "refresh, probe ,launch"}):
            self.assertEqual(
                events._parse_debug_topics(),
                frozenset({"refresh", "probe", "launch"}),
            )


if __name__ == "__main__":
    unittest.main()

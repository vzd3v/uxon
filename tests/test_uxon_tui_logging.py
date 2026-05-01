"""Event-log tests for :func:`uxon_tui.events._log_event`."""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB = os.path.abspath(os.path.join(_HERE, "..", "lib"))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from uxon_tui.events import _log_event  # noqa: E402


class LogEventTests(unittest.TestCase):
    def _find_log(self, log_dir: str) -> pathlib.Path:
        files = list(pathlib.Path(log_dir).glob("*.log"))
        self.assertEqual(len(files), 1, f"expected 1 log file, got {files}")
        return files[0]

    def test_log_event_writes_jsonl_line(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"UXON_LOG_DIR": td}):
                _log_event(
                    "tui_start",
                    caller_user="u",
                    launch_user="u",
                    extra={"version": "1.0.0"},
                )
            lines = self._find_log(td).read_text().splitlines()
            self.assertEqual(len(lines), 1)
            rec = json.loads(lines[0])
            self.assertEqual(rec["event"], "tui_start")
            self.assertEqual(rec["caller_user"], "u")
            self.assertEqual(rec["extra"], {"version": "1.0.0"})

    def test_log_event_appends_multiple_records(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"UXON_LOG_DIR": td}):
                _log_event("a", caller_user="u")
                _log_event("b", caller_user="u")
            lines = self._find_log(td).read_text().splitlines()
            self.assertEqual([json.loads(x)["event"] for x in lines], ["a", "b"])

    def test_log_event_swallows_errors(self) -> None:
        # Un-writable directory — log_event must not raise.
        with mock.patch.dict(os.environ, {"UXON_LOG_DIR": "/proc/nonexistent"}):
            try:
                _log_event("x", caller_user="u")
            except Exception as exc:
                self.fail(f"_log_event raised: {exc!r}")

    def test_log_event_creates_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            nested = os.path.join(td, "a", "b")
            with mock.patch.dict(os.environ, {"UXON_LOG_DIR": nested}):
                _log_event("x", caller_user="u")
            self.assertTrue(os.path.isdir(nested))


if __name__ == "__main__":
    unittest.main()

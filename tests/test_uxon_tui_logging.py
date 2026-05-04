"""Event-log tests for :func:`uxon.tui.events._log_event`."""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from uxon.tui.events import _log_event


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


class StartupChannelTests(unittest.TestCase):
    """Stage 10a — ``UXON_DEBUG=startup`` channel.

    The channel is implemented entirely via the existing ``_debug``
    helper (which already filters on ``UXON_DEBUG`` topics). Stage 10a
    only adds the call sites; the tests assert that those sites emit
    a record with ``topic == "startup"`` when the env var is set.
    """

    def _enable(self, td: str):
        return mock.patch.dict(os.environ, {"UXON_LOG_DIR": td, "UXON_DEBUG": "startup"})

    def _read(self, td: str) -> list[dict]:
        files = sorted(pathlib.Path(td).glob("tui-debug-*.log"))
        if not files:
            return []
        return [json.loads(line) for line in files[0].read_text().splitlines()]

    def test_debug_emits_startup_topic(self) -> None:
        # The ``startup`` channel rides the same ``_debug`` helper used
        # everywhere else; we only need to verify the topic filter
        # passes the new topic name through.
        from uxon.tui.events import _parse_debug_topics, debug

        with tempfile.TemporaryDirectory() as td:
            with self._enable(td):
                # Re-resolve the topic set: events.py snapshots
                # UXON_DEBUG at import. We patch the module-level set
                # for the duration of the test.
                from uxon.tui import events as ev

                with mock.patch.object(ev, "_DEBUG_TOPICS", _parse_debug_topics()):
                    debug("startup", at="mount_started", ts=1.5)
                    debug("startup", at="first_paint", ts=2.0)
                    debug("startup", at="first_data_landed", source="x", ts=2.5)
            recs = self._read(td)
            self.assertEqual([r["topic"] for r in recs], ["startup"] * 3)
            self.assertEqual(
                [r["at"] for r in recs],
                ["mount_started", "first_paint", "first_data_landed"],
            )

    def test_handle_main_ctx_rebuild_logs_first_data_landed_once(self) -> None:
        """The ``first_data_landed`` latch fires once per app instance."""
        from unittest.mock import MagicMock

        from uxon.tui.app import UxonApp, _RefreshSourceLanded

        # Build an app stub that bypasses ``__init__`` (Textual's
        # ``App.__init__`` requires a running event loop). We only need
        # ``_first_data_landed_logged`` and ``post_message`` for the
        # handler.
        app = UxonApp.__new__(UxonApp)
        app._first_data_landed_logged = False  # type: ignore[attr-defined]
        app.post_message = MagicMock()  # type: ignore[method-assign]

        captured: list[dict] = []

        def _fake_debug(topic: str, **fields):  # type: ignore[no-untyped-def]
            captured.append({"topic": topic, **fields})

        with mock.patch("uxon.tui.app._debug", _fake_debug):
            ev1 = _RefreshSourceLanded(name="main_ctx_rebuild", value=None)
            UxonApp._handle_main_ctx_rebuild(app, ev1)
            ev2 = _RefreshSourceLanded(name="main_ctx_rebuild", value=None)
            UxonApp._handle_main_ctx_rebuild(app, ev2)

        startup_records = [r for r in captured if r["topic"] == "startup"]
        self.assertEqual(len(startup_records), 1)
        self.assertEqual(startup_records[0]["at"], "first_data_landed")
        self.assertEqual(startup_records[0]["source"], "main_ctx_rebuild")


if __name__ == "__main__":
    unittest.main()

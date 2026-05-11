"""Tests for the debug and metrics logging channels.

The previous JSONL TUI event log (``_log_event`` / ``tui-{user}-{date}.log``)
was removed in 4.0; audit events now go to journald / syslog via
``uxon.audit``.  The ``debug`` channel (off by default,
``UXON_DEBUG``-gated) and the ``metrics`` channel
(``UXON_METRICS=1``-gated) are unchanged and tested below.
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock


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
        """The ``first_data_landed`` latch fires once per app instance —
        on the first successful landing. Error/empty landings do not
        consume the latch.
        """
        from unittest.mock import MagicMock

        from uxon.tui.app import UxonApp, _RefreshSourceLanded
        from uxon.tui.context import TuiContext
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
        )

        app = UxonApp.__new__(UxonApp)
        app._first_data_landed_logged = False  # type: ignore[attr-defined]
        app.state = TuiState()  # type: ignore[attr-defined]
        app.post_message = MagicMock()  # type: ignore[method-assign]
        app._render = MagicMock()  # type: ignore[attr-defined]

        captured: list[dict] = []

        def _fake_debug(topic: str, **fields):  # type: ignore[no-untyped-def]
            captured.append({"topic": topic, **fields})

        with (
            mock.patch("uxon.tui.app._debug", _fake_debug),
            mock.patch.object(
                UxonApp, "screen_stack", new_callable=mock.PropertyMock, return_value=[]
            ),
        ):
            ev1 = _RefreshSourceLanded(name="main_ctx_rebuild", value=ctx)
            UxonApp._handle_main_ctx_rebuild(app, ev1)
            ev2 = _RefreshSourceLanded(name="main_ctx_rebuild", value=ctx)
            UxonApp._handle_main_ctx_rebuild(app, ev2)

        startup_records = [r for r in captured if r["topic"] == "startup"]
        self.assertEqual(len(startup_records), 1)
        self.assertEqual(startup_records[0]["at"], "first_data_landed")
        self.assertEqual(startup_records[0]["source"], "main_ctx_rebuild")


class MetricsJsonlTests(unittest.TestCase):
    """Stage 10b — ``UXON_METRICS=1`` JSONL with rotation.

    Telemetry path; never raises. The rotation threshold is exposed
    as a module-level test seam (``_METRICS_ROTATE_BYTES``) so we can
    exercise rotation without writing an actual megabyte.
    """

    def _enable(self, td: str):
        return mock.patch.dict(os.environ, {"UXON_LOG_DIR": td, "UXON_METRICS": "1"})

    def test_disabled_by_default(self) -> None:
        from uxon.tui.events import metrics_record

        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"UXON_LOG_DIR": td}, clear=False):
                os.environ.pop("UXON_METRICS", None)
                metrics_record("main_ctx_rebuild", elapsed_ms=12, error=None)
            self.assertEqual(list(pathlib.Path(td).iterdir()), [])

    def test_writes_one_jsonl_line(self) -> None:
        from uxon.tui.events import metrics_record

        with tempfile.TemporaryDirectory() as td:
            with self._enable(td):
                metrics_record(
                    "remote:prod",
                    elapsed_ms=42,
                    error=None,
                    from_cache=True,
                    attempted_at=1700000000.0,
                )
            path = pathlib.Path(td) / "metrics.jsonl"
            self.assertTrue(path.exists())
            lines = path.read_text().splitlines()
            self.assertEqual(len(lines), 1)
            rec = json.loads(lines[0])
            self.assertEqual(rec["source_id"], "remote:prod")
            self.assertEqual(rec["elapsed_ms"], 42)
            self.assertIsNone(rec["error"])
            self.assertTrue(rec["from_cache"])
            self.assertEqual(rec["attempted_at"], 1700000000.0)

    def test_swallows_errors(self) -> None:
        from uxon.tui.events import metrics_record

        with mock.patch.dict(
            os.environ,
            {"UXON_LOG_DIR": "/proc/nonexistent", "UXON_METRICS": "1"},
        ):
            try:
                metrics_record("x", elapsed_ms=1, error=None)
            except Exception as exc:
                self.fail(f"metrics_record raised: {exc!r}")

    def test_rotation_at_threshold(self) -> None:
        from uxon.tui import events as ev

        with tempfile.TemporaryDirectory() as td:
            with self._enable(td), mock.patch.object(ev, "_METRICS_ROTATE_BYTES", 256):
                # Each record is around 100 bytes — write 8 to push past
                # 256 bytes and force a rotation. The rotation check
                # happens before the *next* write, so after rotation the
                # main file holds the post-rotation entries and ``.1``
                # holds the pre-rotation batch.
                for i in range(8):
                    ev.metrics_record(
                        f"remote:host{i}",
                        elapsed_ms=i,
                        error=None,
                        from_cache=False,
                    )
            main = pathlib.Path(td) / "metrics.jsonl"
            rotated = pathlib.Path(td) / "metrics.jsonl.1"
            self.assertTrue(main.exists())
            self.assertTrue(rotated.exists())
            # Both files contain valid JSON lines.
            for p in (main, rotated):
                for line in p.read_text().splitlines():
                    rec = json.loads(line)
                    self.assertIn("source_id", rec)
                    self.assertIn("elapsed_ms", rec)


if __name__ == "__main__":
    unittest.main()

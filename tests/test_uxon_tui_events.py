"""Tests for the debug-logging channel in ``uxon.infra.events``.

The user-facing ``_log_event`` is exercised end-to-end by the TUI
integration suite; this module covers ``debug()`` — the off-by-default
diagnostic channel gated on ``UXON_DEBUG``.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from uxon.infra import events


class DebugChannelTests(unittest.TestCase):
    def setUp(self) -> None:
        # Save and restore module-level topic state so tests don't bleed.
        self._saved = events._DEBUG_TOPICS

    def tearDown(self) -> None:
        events._DEBUG_TOPICS = self._saved

    def test_debug_topics_private_file_and_disabled_lifecycle(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"UXON_LOG_DIR": tmp, "USER": "alice", "SUDO_USER": ""}),
        ):
            directory = Path(tmp)
            events._DEBUG_TOPICS = frozenset()
            events.debug("refresh", action="disabled")
            self.assertEqual(list(directory.iterdir()), [])
            events._DEBUG_TOPICS = frozenset({"refresh"})
            events.debug("probe", action="filtered")
            self.assertEqual(list(directory.iterdir()), [])
            events.debug("refresh", at="worker", elapsed_ms=42)
            events.debug("probe", action="still-filtered")
            events._DEBUG_TOPICS = frozenset({"*"})
            events.debug("anything", value=1)
            [log] = directory.iterdir()
            records = [json.loads(line) for line in log.read_text().splitlines()]
            for record in records:
                self.assertTrue(record.pop("ts").endswith("Z"))
            self.assertEqual(
                records,
                [
                    {"topic": "refresh", "at": "worker", "elapsed_ms": 42},
                    {"topic": "anything", "value": 1},
                ],
            )
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)

    def test_debug_and_metrics_unusable_directory_preserve_regular_file(self) -> None:
        events._DEBUG_TOPICS = frozenset({"*"})
        with tempfile.TemporaryDirectory() as tmp:
            sentinel = Path(tmp) / "sentinel"
            sentinel.write_bytes(b"unchanged")
            with mock.patch.dict(
                os.environ, {"UXON_LOG_DIR": str(sentinel / "logs"), "UXON_METRICS": "1"}
            ):
                events.debug("refresh", x=1)
                events.metrics_record("local", elapsed_ms=1, error=None)
            self.assertEqual(sentinel.read_bytes(), b"unchanged")
            self.assertEqual(list(Path(tmp).iterdir()), [sentinel])

    def test_existing_nonprivate_directory_is_not_chmodded_or_used(self) -> None:
        events._DEBUG_TOPICS = frozenset({"keys"})
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            directory.chmod(0o755)
            files = [directory / ("metrics.jsonl" + suffix) for suffix in ("", ".1", ".2")]
            for index, path in enumerate(files):
                path.write_text(f"generation-{index}")
            before = {p.name: (p.read_bytes(), p.stat().st_mode) for p in files}
            with (
                mock.patch.dict(os.environ, {"UXON_LOG_DIR": tmp, "UXON_METRICS": "1"}),
                mock.patch.object(events, "_METRICS_ROTATE_BYTES", 1),
            ):
                events.debug("keys", at="stdin_read", nbytes=1)
                events.metrics_record("local", elapsed_ms=1, error=None)
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755)
            self.assertEqual(
                {p.name: (p.read_bytes(), p.stat().st_mode) for p in directory.iterdir()}, before
            )

    def test_metrics_rejects_linked_generations_before_rotation(self) -> None:
        for suffix in ("", ".1", ".2"):
            for link_kind in ("symlink", "hardlink"):
                with (
                    self.subTest(suffix=suffix, link=link_kind),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    directory = Path(tmp) / "logs"
                    directory.mkdir(mode=0o700)
                    external = Path(tmp) / "untouched"
                    external.write_text("do not overwrite")
                    external.chmod(0o600)
                    files = [directory / ("metrics.jsonl" + s) for s in ("", ".1", ".2")]
                    for index, path in enumerate(files):
                        path.write_text(f"generation-{index}")
                        path.chmod(0o600)
                    linked = directory / ("metrics.jsonl" + suffix)
                    linked.unlink()
                    if link_kind == "symlink":
                        linked.symlink_to(external)
                    else:
                        os.link(external, linked)
                    before = {p.name: (p.read_bytes(), p.lstat().st_ino) for p in files}
                    with (
                        mock.patch.dict(
                            os.environ, {"UXON_LOG_DIR": str(directory), "UXON_METRICS": "1"}
                        ),
                        mock.patch.object(events, "_METRICS_ROTATE_BYTES", 1),
                    ):
                        events.metrics_record("local", elapsed_ms=1, error=None)
                    self.assertEqual(
                        {p.name: (p.read_bytes(), p.lstat().st_ino) for p in directory.iterdir()},
                        before,
                    )
                    self.assertEqual(external.read_text(), "do not overwrite")

    def test_real_parser_preserves_paste_without_logging_its_content(self) -> None:
        from textual._xterm_parser import XTermParser
        from textual.events import Paste

        from uxon.tui.keylog import diagnostic_key, install_stdin_tap

        secret = "synthetic-confidential-paste"
        events._DEBUG_TOPICS = frozenset({"keys"})
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"UXON_LOG_DIR": tmp}),
            mock.patch.object(XTermParser, "feed", XTermParser.feed),
        ):
            self.assertTrue(install_stdin_tap())
            parsed = list(XTermParser().feed("\x1b[200~" + secret + "\x1b[201~"))
            self.assertTrue(
                any(isinstance(event, Paste) and event.text == secret for event in parsed)
            )
            logged = self._read_only_line(tmp)
            self.assertNotIn(secret, logged)
            self.assertNotIn("data", json.loads(logged))
            self.assertGreater(json.loads(logged)["nbytes"], len(secret))
        self.assertEqual(diagnostic_key("up"), "up")
        for key in ("p", secret, "unknown-key", "ctrl+p"):
            self.assertEqual(diagnostic_key(key), "input")

    def _read_only_line(self, tmp: str) -> str:
        files = os.listdir(tmp)
        self.assertEqual(len(files), 1, f"expected one log file, got {files}")
        with open(os.path.join(tmp, files[0]), encoding="utf-8") as fh:
            lines = fh.readlines()
        self.assertEqual(len(lines), 1)
        return lines[0]


class DebugTopicParserTests(unittest.TestCase):
    def test_debug_topic_environment_grammar(self) -> None:
        for raw, expected in [
            (None, frozenset()),
            ("", frozenset()),
            *[(v, frozenset({"*"})) for v in ("1", "true", "all", "*", "yes", "on", "TRUE", "ON")],
            ("refresh, probe ,launch,,refresh", frozenset({"refresh", "probe", "launch"})),
        ]:
            with self.subTest(raw=raw), mock.patch.dict(os.environ):
                if raw is None:
                    os.environ.pop("UXON_DEBUG", None)
                else:
                    os.environ["UXON_DEBUG"] = raw
                self.assertEqual(events._parse_debug_topics(), expected)


if __name__ == "__main__":
    unittest.main()

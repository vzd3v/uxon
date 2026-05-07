"""Tests for the per-user dismissed-detected-agents state file."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from uxon import dismissed as ud


class StateDirTests(unittest.TestCase):
    def test_honours_xdg_state_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp}, clear=False):
                self.assertEqual(ud.state_dir(), Path(tmp) / "uxon")

    def test_falls_back_to_local_state(self) -> None:
        # When ``$XDG_STATE_HOME`` is unset, platformdirs derives the
        # path from ``$HOME`` per the XDG Base Directory spec.
        env = dict(os.environ)
        env.pop("XDG_STATE_HOME", None)
        env["HOME"] = "/home/x"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(ud.state_dir(), Path("/home/x/.local/state/uxon"))


class LoadDismissedTests(unittest.TestCase):
    def test_missing_file_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp}, clear=False):
                self.assertEqual(ud.load_dismissed(), [])

    def test_malformed_file_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp}, clear=False):
                path = ud.dismissed_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{not json", encoding="utf-8")
                self.assertEqual(ud.load_dismissed(), [])

    def test_valid_file_returns_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp}, clear=False):
                path = ud.dismissed_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps({"dismissed_detected_agents": ["codex", "cursor"]}),
                    encoding="utf-8",
                )
                self.assertEqual(ud.load_dismissed(), ["codex", "cursor"])

    def test_non_dict_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp}, clear=False):
                path = ud.dismissed_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("[1, 2, 3]", encoding="utf-8")
                self.assertEqual(ud.load_dismissed(), [])


class AddDismissedTests(unittest.TestCase):
    def test_adds_new_entry_and_creates_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp}, clear=False):
                result = ud.add_dismissed("codex")
                self.assertEqual(result, ["codex"])
                self.assertTrue(ud.dismissed_path().exists())
                self.assertEqual(ud.load_dismissed(), ["codex"])

    def test_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp}, clear=False):
                ud.add_dismissed("codex")
                result = ud.add_dismissed("codex")
                self.assertEqual(result, ["codex"])

    def test_appends_second_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp}, clear=False):
                ud.add_dismissed("codex")
                ud.add_dismissed("cursor")
                self.assertEqual(ud.load_dismissed(), ["codex", "cursor"])


if __name__ == "__main__":
    unittest.main()

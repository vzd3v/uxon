"""Tests for ``uxon attach --user`` and ``uxon attach --host`` (multi-host).

Symmetric to ``test_uxon_kill_multi.py``. Parser-level tests live
here; behaviour tests for cross-user / cross-host execution paths
live in companion classes below.
"""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from uxon import cli as uxon
from uxon.remote_hosts import RemoteHost


class AttachParserTests(unittest.TestCase):
    def test_attach_with_user(self) -> None:
        a = uxon.parse_args(["attach", "demo@claude", "--user", "alice"])
        self.assertEqual(a.action, "attach")
        self.assertEqual(a.target_id, "demo@claude")
        self.assertEqual(a.user, "alice")
        self.assertIsNone(a.host)

    def test_attach_with_host_and_user(self) -> None:
        a = uxon.parse_args(["attach", "demo@claude", "--host", "box-b", "--user", "alice"])
        self.assertEqual(a.host, "box-b")
        self.assertEqual(a.user, "alice")

    def test_attach_with_host_without_user_fails(self) -> None:
        # --host without --user is rejected at parse time: implicit
        # peer-login-user defaults invite "where did this attach
        # actually go?" surprises.
        with self.assertRaises(SystemExit):
            uxon.parse_args(["attach", "demo@claude", "--host", "box-b"])

    def test_attach_dry_run(self) -> None:
        a = uxon.parse_args(
            ["attach", "demo@claude", "--host", "box-b", "--user", "alice", "--dry-run"]
        )
        self.assertTrue(a.dry_run)

    def test_attach_unknown_flag(self) -> None:
        with self.assertRaises(SystemExit):
            uxon.parse_args(["attach", "demo@claude", "--unknown"])

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


class AttachCrossUserTests(unittest.TestCase):
    """Peer-side ``uxon attach --user`` cross-user gating.

    Mirror of ``KillUserCrossUserTests`` in test_uxon_kill_multi.py.
    """

    def _cfg(self) -> uxon.Config:
        from tests.test_uxon_kill_multi import _make_config
        return _make_config()

    def test_same_user_no_sudo_path(self) -> None:
        cfg = self._cfg()
        args = uxon.ParsedArgs(action="attach", target_id="demo@claude", user="u-vz", dry_run=True)
        with mock.patch.object(uxon, "collect_sessions") as cs, \
             mock.patch.object(uxon, "resolve_session") as rs, \
             mock.patch.object(uxon, "attach_session", return_value=0) as att:
            cs.return_value = []
            rs.return_value = mock.Mock(name="demo@claude")
            rc = uxon.do_attach(args, cfg, "u-vz")
        self.assertEqual(rc, 0)
        # No probe needed when target == launch_user
        att.assert_called_once()

    def test_cross_user_unreachable_emits_stable_tag(self) -> None:
        cfg = self._cfg()
        args = uxon.ParsedArgs(action="attach", target_id="demo@claude", user="alice")
        from uxon.sudo_probe import SudoCapability
        caps = SudoCapability(reachable_users=frozenset(), can_root=False)
        buf = io.StringIO()
        with mock.patch("uxon.sudo_probe.probe_sudo_capability", return_value=caps), \
             redirect_stdout(buf):
            with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                rc = uxon.do_attach(args, cfg, "u-vz")
        # Stable error tag — aggregator's UI surfaces it via
        # pause_on_launch_failure.
        self.assertEqual(rc, 1)
        self.assertIn("uxon-error: not-reachable", err.getvalue())

    def test_cross_user_reachable_dry_run_shows_sudo_prefix(self) -> None:
        cfg = self._cfg()
        args = uxon.ParsedArgs(
            action="attach", target_id="demo@claude", user="alice", dry_run=True
        )
        from uxon.sudo_probe import SudoCapability
        caps = SudoCapability(reachable_users=frozenset({"alice"}), can_root=False)
        buf = io.StringIO()
        with mock.patch("uxon.sudo_probe.probe_sudo_capability", return_value=caps), \
             mock.patch.object(uxon, "collect_sessions", return_value=[]), \
             mock.patch.object(uxon, "tmux_socket_path", return_value="/tmp/uxon-alice.sock"), \
             mock.patch.object(uxon, "process_user", return_value="u-vz"), \
             mock.patch.object(uxon, "resolve_session") as rs:
            rs.return_value = mock.Mock(name="demo@claude")
            rs.return_value.name = "demo@claude"
            with redirect_stdout(buf):
                rc = uxon.do_attach(args, cfg, "u-vz")
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("sudo", out)
        self.assertIn("alice", out)
        self.assertIn("tmux", out)


class AttachHostRemoteTests(unittest.TestCase):
    """``uxon attach --host <alias> --user <u>`` SSH-routed dispatch."""

    def _cfg_with_host(self, **host_kwargs) -> uxon.Config:
        from tests.test_uxon_kill_multi import _make_config
        return _make_config(
            remote_hosts=[
                RemoteHost(
                    name="box-b",
                    ssh_alias="ssh-b",
                    description="",
                    remote_uxon="uxon",
                    **host_kwargs,
                )
            ]
        )

    def test_host_dry_run_prints_ssh_attach_command(self) -> None:
        cfg = self._cfg_with_host()
        args = uxon.ParsedArgs(
            action="attach",
            target_id="demo@claude",
            host="box-b",
            user="alice",
            dry_run=True,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = uxon.do_attach(args, cfg, "u-vz")
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("ssh", out)
        self.assertIn("-tt", out)  # interactive PTY
        self.assertIn("ssh-b", out)
        self.assertIn("uxon attach", out)
        self.assertIn("--user", out)
        self.assertIn("alice", out)
        self.assertIn("demo@claude", out)

    def test_host_honours_command_template(self) -> None:
        cfg = self._cfg_with_host(
            command_template=("ssh", "-J", "bastion", "{ssh_alias}", "{remote_command}"),
        )
        args = uxon.ParsedArgs(
            action="attach",
            target_id="demo@claude",
            host="box-b",
            user="alice",
            dry_run=True,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            uxon.do_attach(args, cfg, "u-vz")
        out = buf.getvalue()
        self.assertIn("-J", out)
        self.assertIn("bastion", out)
        # -tt still injected after the outermost ssh.
        first_ssh = out.find("ssh")
        first_tt = out.find("-tt")
        self.assertGreater(first_tt, first_ssh)

    def test_host_unknown_alias_fails(self) -> None:
        cfg = self._cfg_with_host()
        args = uxon.ParsedArgs(
            action="attach",
            target_id="demo@claude",
            host="unknown",
            user="alice",
        )
        with self.assertRaises(SystemExit):
            uxon.do_attach(args, cfg, "u-vz")

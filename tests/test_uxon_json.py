"""Managed listing authority and post-kill cleanup regression contracts."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

from helpers import make_config as _make_config
from helpers import make_session as _make_session

import uxon.app.kill as kill_app
import uxon.app.listing as listing_app
from uxon.domain.args import ParsedArgs


class ListJsonTests(unittest.TestCase):
    def test_session_records_split_profile_agent_and_container(self) -> None:
        cfg = _make_config()
        same_agent_a = _make_session("uxon-demo@claude_fast")
        same_agent_a.profile = "claude_fast"
        same_agent_a.agent = "claude"
        same_agent_a.launch_record_verified = True
        same_agent_b = _make_session("uxon-demo@claude_safe")
        same_agent_b.profile = "claude_safe"
        same_agent_b.agent = "claude"
        same_agent_b.launch_record_verified = True
        boxed = _make_session("uxon-demo@claude_box")
        boxed.profile = "claude_box"
        boxed.agent = "claude"
        boxed.launch_record_verified = True
        boxed.runtime = "box_a"
        boxed.runtime_resource = "uxon-box-a"

        data = listing_app._list_data(
            cfg, [same_agent_a, same_agent_b, boxed], ["alice"], all_users=False
        )
        records = data["sessions"]
        self.assertEqual(
            [(r["profile"], r["agent"]) for r in records],
            [("claude_fast", "claude"), ("claude_safe", "claude"), ("claude_box", "claude")],
        )
        self.assertEqual(records[2]["runtime"], "box_a")
        self.assertEqual(records[2]["runtime_resource"], "uxon-box-a")

    def test_all_users_flag_propagates(self) -> None:
        cfg = _make_config()
        data = listing_app._list_data(cfg, [], ["alice", "bob"], all_users=True)
        self.assertTrue(data["all_users"])
        self.assertEqual(data["scope_users"], ["alice", "bob"])

    def test_collect_sessions_uses_verified_launch_record_fields(self) -> None:
        from uxon.infra import sessions_probe

        cfg = _make_config()
        list_row = "\t".join(
            [
                "uxon-demo@claude_fast",
                "$1",
                "0",
                "1",
                "1780000000",
                "1780000100",
                "nonce-1",
                "env-box",
                "/env.scope",
            ]
        )
        pane_row = "1\t111\tsh\t/srv/repos/demo\t1\t%0\t0\t\t"
        record = {
            "profile": "claude_fast",
            "agent": "claude",
            "launch_user": "alice",
            "runtime": "box",
            "runtime_fingerprint": "fp",
            "runtime_resource": "record-box",
            "runtime_id": "cid-1",
            "runtime_cgroup": "/record.scope",
            "runtime_epoch": "1000",
        }
        with (
            mock.patch(
                "uxon.infra.sessions_probe.tmux.probe_tmux_server",
                return_value=mock.Mock(state="running", error=""),
            ),
            mock.patch(
                "uxon.infra.sessions_probe.run_query",
                return_value=mock.Mock(returncode=0, stdout=""),
            ),
            mock.patch(
                "uxon.infra.sessions_probe.run_cmd",
                side_effect=[
                    mock.Mock(returncode=0, stdout=list_row),
                    mock.Mock(returncode=0, stdout=pane_row),
                ],
            ),
            mock.patch("uxon.infra.sessions_probe.read_verified_record", return_value=record),
            mock.patch("uxon.infra.sessions_probe.garbage_collect_records"),
            mock.patch("uxon.infra.sessions_probe.enrich_session_usage"),
        ):
            [session] = sessions_probe.collect_sessions_for_user(
                cfg, "alice", "uxon-", "/tmp/uxon-alice.sock"
            )

        self.assertEqual(session.profile, "claude_fast")
        self.assertEqual(session.agent, "claude")
        self.assertEqual(session.launch_user, "alice")
        self.assertEqual(session.runtime, "box")
        self.assertEqual(session.runtime_resource, "record-box")
        self.assertEqual(session.runtime_id, "cid-1")
        self.assertEqual(session.runtime_cgroup, "/record.scope")
        self.assertEqual(session.runtime_epoch, "1000")
        self.assertEqual(session.runtime_marker, "env-box")

    def test_collect_sessions_without_record_uses_suffix_display_only(self) -> None:
        from uxon.infra import sessions_probe

        cfg = _make_config()
        list_row = "\t".join(
            [
                "uxon-demo@claude_fast",
                "$1",
                "0",
                "1",
                "1780000000",
                "1780000100",
                "nonce-1",
                "env-box",
                "/env.scope",
            ]
        )
        pane_row = "1\t111\tdocker\t/srv/repos/demo\t1\t%0\t0\t\t"
        with (
            mock.patch(
                "uxon.infra.sessions_probe.tmux.probe_tmux_server",
                return_value=mock.Mock(state="running", error=""),
            ),
            mock.patch(
                "uxon.infra.sessions_probe.run_query",
                return_value=mock.Mock(returncode=0, stdout=""),
            ),
            mock.patch(
                "uxon.infra.sessions_probe.run_cmd",
                side_effect=[
                    mock.Mock(returncode=0, stdout=list_row),
                    mock.Mock(returncode=0, stdout=pane_row),
                ],
            ),
            mock.patch("uxon.infra.sessions_probe.read_verified_record", return_value=None),
            mock.patch("uxon.infra.sessions_probe.garbage_collect_records"),
            mock.patch("uxon.infra.sessions_probe.enrich_session_usage"),
        ):
            [session] = sessions_probe.collect_sessions_for_user(
                cfg, "alice", "uxon-", "/tmp/uxon-alice.sock"
            )

        self.assertEqual(session.profile, "claude_fast")
        self.assertEqual(session.agent, "")
        self.assertFalse(session.launch_record_verified)
        self.assertEqual(session.runtime_resource, "")
        self.assertEqual(session.runtime, "")
        self.assertEqual(session.runtime_marker, "env-box")


class KillJsonTests(unittest.TestCase):
    def test_real_kill_emits_killed(self) -> None:
        cfg = _make_config()
        target = _make_session("uxon-demo@claude")
        args = ParsedArgs(action="kill", target_id="demo@claude", json_output=True)
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with (
            mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[target]),
            mock.patch("uxon.infra.tmux.tmux_socket_path", return_value="/tmp/uxon-alice.sock"),
            mock.patch("uxon.infra.tmux.configured_tmux_base", return_value=["tmux"]),
            mock.patch("uxon.infra.process.run_cmd", return_value=completed),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = kill_app.do_kill(args, cfg, "alice")
        self.assertEqual(rc, 0)
        env = json.loads(buf.getvalue())
        self.assertEqual(env["data"]["action"], "killed")
        self.assertFalse(env["data"]["dry_run"])


class KillAllJsonTests(unittest.TestCase):
    def test_failed_kill_records_failed_action(self) -> None:
        cfg = _make_config()
        s1 = _make_session("uxon-a@claude")
        args = ParsedArgs(action="kill-all", force=True, json_output=True)
        cp_fail = mock.Mock(returncode=1, stdout="", stderr="boom")
        with (
            mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[s1]),
            mock.patch("uxon.infra.tmux.tmux_socket_path", return_value="/tmp/uxon-alice.sock"),
            mock.patch("uxon.infra.tmux.configured_tmux_base", return_value=["tmux"]),
            mock.patch("uxon.infra.process.run_cmd", return_value=cp_fail),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = kill_app.do_kill_all(args, cfg, "alice")
        self.assertEqual(rc, 0)
        env = json.loads(buf.getvalue())
        self.assertEqual(env["data"]["sessions"][0]["action"], "failed")
        self.assertEqual(env["data"]["sessions"][0]["cleanup_outcome"], "not_run")

    def test_cleanup_failure_keeps_killed_action_and_reports_cleanup_outcome(self) -> None:
        cfg = _make_config()
        target = _make_session("uxon-a@claude")
        args = ParsedArgs(action="kill-all", force=True, json_output=True)
        with (
            mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[target]),
            mock.patch("uxon.infra.tmux.tmux_socket_path", return_value="/tmp/uxon-alice.sock"),
            mock.patch("uxon.infra.tmux.configured_tmux_base", return_value=["tmux"]),
            mock.patch(
                "uxon.infra.process.run_cmd",
                return_value=mock.Mock(returncode=0, stdout="", stderr=""),
            ),
            mock.patch("uxon.app.kill.prepare_runtime_teardown", return_value=None),
            mock.patch("uxon.app.kill.finish_killed_session", return_value=False),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = kill_app.do_kill_all(args, cfg, "alice")
        self.assertEqual(rc, 0)
        [result] = json.loads(buf.getvalue())["data"]["sessions"]
        self.assertEqual(result["action"], "killed")
        self.assertEqual(result["cleanup_outcome"], "error")

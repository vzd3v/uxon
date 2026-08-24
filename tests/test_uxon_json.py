"""Tests for ``--json`` output across list/version/kill/kill-all.

The ``--json`` flag is the single producer surface that the future
multi-host RemoteCollector parses by SSH-running ``uxon ... --json``
on a remote host. These tests pin:

- The CLI parser accepts ``--json`` on every action where it is
  documented (list, version, doctor, kill, kill-all), via the
  subcommand form, the dash-flag form, and where applicable the
  ``-V`` / ``-l`` / ``-k`` / ``--killall`` shortcuts.
- The success-path stdout of each handler is exactly one wire-schema
  envelope: a JSON object with ``schema_version``, ``uxon_version``,
  ``kind``, and ``data``. Nothing else (no human-readable preamble,
  no trailing print).
- The shape of ``data`` for each ``kind`` matches the contract
  documented in ``wire_schema.py``.

``do_doctor``'s JSON branch is covered at the parser surface only;
its end-to-end exercise needs heavy host-probe stubs and is left to
follow-up tests if the doctor JSON shape changes.
"""

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
from uxon.cli.parsing import parse_args
from uxon.domain.args import ParsedArgs
from uxon.domain.config import Config
from uxon.domain.wire_schema import WIRE_SCHEMA_VERSION
from uxon.infra import version_probe


class JsonFlagParsingTests(unittest.TestCase):
    """``--json`` is recognised on every action it is documented for,
    and not on actions where it has no defined meaning (run, attach,
    new — those would need a separate design for streaming output)."""

    def test_list_subcommand(self) -> None:
        self.assertTrue(parse_args(["list", "--json"]).json_output)
        self.assertTrue(parse_args(["list", "--all-users", "--json"]).json_output)

    def test_list_short_flag(self) -> None:
        self.assertTrue(parse_args(["-l", "--json"]).json_output)

    def test_version_subcommand_and_flags(self) -> None:
        self.assertTrue(parse_args(["version", "--json"]).json_output)
        self.assertTrue(parse_args(["-V", "--json"]).json_output)
        self.assertTrue(parse_args(["--version", "--json"]).json_output)

    def test_doctor_subcommand(self) -> None:
        self.assertTrue(parse_args(["doctor", "--json"]).json_output)

    def test_kill_subcommand_and_flag(self) -> None:
        a = parse_args(["kill", "uxon-foo@claude", "--json"])
        self.assertTrue(a.json_output)
        self.assertEqual(a.action, "kill")
        b = parse_args(["-k", "uxon-foo@claude", "--json", "--dry-run"])
        self.assertTrue(b.json_output)
        self.assertTrue(b.dry_run)

    def test_kill_all_subcommand_and_flag(self) -> None:
        self.assertTrue(parse_args(["kill-all", "--json", "--force"]).json_output)
        self.assertTrue(parse_args(["--killall", "--json", "--dry-run"]).json_output)

    def test_default_is_off(self) -> None:
        self.assertFalse(parse_args(["list"]).json_output)
        self.assertFalse(parse_args(["version"]).json_output)


class VersionJsonTests(unittest.TestCase):
    def test_emits_versioned_envelope(self) -> None:
        with (
            mock.patch("uxon.infra.version_probe.read_repo_version", return_value="9.9.9"),
            mock.patch("uxon.infra.version_probe.read_git_commit_short", return_value="deadbee"),
            mock.patch("uxon.infra.version_probe.repo_is_dirty", return_value=False),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                listing_app._emit_json("version", version_probe._version_data())
        env = json.loads(buf.getvalue())
        self.assertEqual(env["schema_version"], WIRE_SCHEMA_VERSION)
        self.assertEqual(env["uxon_version"], "9.9.9")
        self.assertEqual(env["kind"], "version")
        self.assertEqual(
            env["data"],
            {
                "uxon_version": "9.9.9",
                "commit": "deadbee",
                "commit_dirty": False,
            },
        )

    def test_no_commit_marks_dirty_false(self) -> None:
        # When git is unavailable, ``commit`` must be ``null`` (not "-")
        # and ``commit_dirty`` must default to False so consumers can
        # treat a missing checkout as "no dirty signal" rather than
        # parsing a placeholder string.
        with (
            mock.patch("uxon.infra.version_probe.read_repo_version", return_value="0.0.1"),
            mock.patch("uxon.infra.version_probe.read_git_commit_short", return_value=None),
        ):
            data = version_probe._version_data()
        self.assertIsNone(data["commit"])
        self.assertFalse(data["commit_dirty"])


class ListJsonTests(unittest.TestCase):
    def test_envelope_kind_and_session_records(self) -> None:
        cfg = _make_config()
        sessions = [_make_session("uxon-alpha@claude"), _make_session("uxon-beta@claude")]
        data = listing_app._list_data(cfg, sessions, ["u-vz"], all_users=False)
        self.assertEqual(data["all_users"], False)
        self.assertEqual(data["scope_users"], ["u-vz"])
        self.assertEqual(data["session_prefix"], "uxon-")
        self.assertEqual(len(data["sessions"]), 2)
        self.assertEqual(data["sessions"][0]["short_id"], "alpha@claude")
        self.assertEqual(data["sessions"][1]["short_id"], "beta@claude")

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
            cfg, [same_agent_a, same_agent_b, boxed], ["u-vz"], all_users=False
        )
        records = data["sessions"]
        self.assertEqual(
            [(r["profile"], r["agent"]) for r in records],
            [("claude_fast", "claude"), ("claude_safe", "claude"), ("claude_box", "claude")],
        )
        self.assertEqual(records[2]["runtime"], "box_a")
        self.assertEqual(records[2]["runtime_resource"], "uxon-box-a")

    def test_empty_sessions_emits_empty_list(self) -> None:
        cfg = _make_config()
        data = listing_app._list_data(cfg, [], ["u-vz"], all_users=False)
        self.assertEqual(data["sessions"], [])

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
        pane_row = "1\t111\tsh\t/srv/repos/demo"
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
                    mock.Mock(stdout=list_row),
                    mock.Mock(returncode=0, stdout=pane_row),
                ],
            ),
            mock.patch("uxon.infra.sessions_probe.read_verified_record", return_value=record),
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
        pane_row = "1\t111\tdocker\t/srv/repos/demo"
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
                    mock.Mock(stdout=list_row),
                    mock.Mock(returncode=0, stdout=pane_row),
                ],
            ),
            mock.patch("uxon.infra.sessions_probe.read_verified_record", return_value=None),
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
    def test_dry_run_emits_would_kill(self) -> None:
        cfg = _make_config()
        target = _make_session("uxon-demo@claude")
        args = ParsedArgs(action="kill", target_id="demo@claude", dry_run=True, json_output=True)
        with (
            mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[target]),
            mock.patch("uxon.infra.tmux.tmux_socket_path", return_value="/tmp/uxon-u-vz.sock"),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = kill_app.do_kill(args, cfg, "u-vz")
        self.assertEqual(rc, 0)
        env = json.loads(buf.getvalue())
        self.assertEqual(env["kind"], "kill")
        self.assertEqual(env["data"]["target"], "uxon-demo@claude")
        self.assertEqual(env["data"]["action"], "would-kill")
        self.assertTrue(env["data"]["dry_run"])
        self.assertEqual(env["data"]["socket"], "/tmp/uxon-u-vz.sock")

    def test_real_kill_emits_killed(self) -> None:
        cfg = _make_config()
        target = _make_session("uxon-demo@claude")
        args = ParsedArgs(action="kill", target_id="demo@claude", json_output=True)
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with (
            mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[target]),
            mock.patch("uxon.infra.tmux.tmux_socket_path", return_value="/tmp/uxon-u-vz.sock"),
            mock.patch("uxon.infra.tmux.configured_tmux_base", return_value=["tmux"]),
            mock.patch("uxon.infra.process.run_cmd", return_value=completed),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = kill_app.do_kill(args, cfg, "u-vz")
        self.assertEqual(rc, 0)
        env = json.loads(buf.getvalue())
        self.assertEqual(env["data"]["action"], "killed")
        self.assertFalse(env["data"]["dry_run"])


class KillAllJsonTests(unittest.TestCase):
    def test_no_sessions_emits_empty_envelope(self) -> None:
        cfg = _make_config()
        args = ParsedArgs(action="kill-all", force=True, json_output=True)
        with (
            mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[]),
            mock.patch("uxon.infra.tmux.tmux_socket_path", return_value="/tmp/uxon-u-vz.sock"),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = kill_app.do_kill_all(args, cfg, "u-vz")
        self.assertEqual(rc, 0)
        env = json.loads(buf.getvalue())
        self.assertEqual(env["kind"], "kill-all")
        self.assertEqual(env["data"]["sessions"], [])

    def test_dry_run_lists_all_with_would_kill(self) -> None:
        cfg = _make_config()
        s1 = _make_session("uxon-a@claude")
        s2 = _make_session("uxon-b@claude")
        args = ParsedArgs(action="kill-all", dry_run=True, json_output=True)
        with (
            mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[s1, s2]),
            mock.patch("uxon.infra.tmux.tmux_socket_path", return_value="/tmp/uxon-u-vz.sock"),
            mock.patch("uxon.infra.tmux.configured_tmux_base", return_value=["tmux"]),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = kill_app.do_kill_all(args, cfg, "u-vz")
        self.assertEqual(rc, 0)
        env = json.loads(buf.getvalue())
        actions = [(r["name"], r["action"]) for r in env["data"]["sessions"]]
        self.assertEqual(
            actions, [("uxon-a@claude", "would-kill"), ("uxon-b@claude", "would-kill")]
        )
        self.assertTrue(env["data"]["dry_run"])

    def test_json_without_force_or_dry_run_refuses(self) -> None:
        # Interactive prompt with --json would corrupt the JSON stream
        # AND there is nowhere to read confirmation from. We require
        # the caller to be explicit.
        cfg = _make_config()
        args = ParsedArgs(action="kill-all", json_output=True)
        with (
            mock.patch(
                "uxon.infra.sessions_probe.collect_sessions", return_value=[_make_session()]
            ),
            mock.patch("uxon.errors.eprint") as eprint,
            self.assertRaises(SystemExit),
        ):
            kill_app.do_kill_all(args, cfg, "u-vz")
        self.assertIn("--json requires", eprint.call_args[0][0])

    def test_failed_kill_records_failed_action(self) -> None:
        cfg = _make_config()
        s1 = _make_session("uxon-a@claude")
        args = ParsedArgs(action="kill-all", force=True, json_output=True)
        cp_fail = mock.Mock(returncode=1, stdout="", stderr="boom")
        with (
            mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[s1]),
            mock.patch("uxon.infra.tmux.tmux_socket_path", return_value="/tmp/uxon-u-vz.sock"),
            mock.patch("uxon.infra.tmux.configured_tmux_base", return_value=["tmux"]),
            mock.patch("uxon.infra.process.run_cmd", return_value=cp_fail),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = kill_app.do_kill_all(args, cfg, "u-vz")
        self.assertEqual(rc, 0)
        env = json.loads(buf.getvalue())
        self.assertEqual(env["data"]["sessions"][0]["action"], "failed")


class HostFlagParsingTests(unittest.TestCase):
    """``--host`` / ``--all-hosts`` are recognised on ``list`` only and
    are mutually exclusive."""

    def test_host_with_value(self) -> None:
        a = parse_args(["list", "--host", "vz-prod1"])
        self.assertEqual(a.host, "vz-prod1")
        self.assertFalse(a.all_hosts)

    def test_all_hosts_flag(self) -> None:
        a = parse_args(["list", "--all-hosts"])
        self.assertTrue(a.all_hosts)
        self.assertIsNone(a.host)

    def test_host_requires_value(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(["list", "--host"])

    def test_host_and_all_hosts_mutually_exclusive(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(["list", "--host", "x", "--all-hosts"])

    def test_combines_with_json(self) -> None:
        a = parse_args(["list", "--host", "x", "--json"])
        self.assertEqual(a.host, "x")
        self.assertTrue(a.json_output)

    def test_default_off(self) -> None:
        a = parse_args(["list"])
        self.assertIsNone(a.host)
        self.assertFalse(a.all_hosts)


class HostDispatchTests(unittest.TestCase):
    def _cfg_with_hosts(self, hosts: list) -> Config:
        from uxon.infra.remote_hosts import RemoteHost

        cfg = _make_config()
        cfg.remote_hosts = [
            RemoteHost(name=n, ssh_alias=n, description="", remote_uxon="uxon") for n in hosts
        ]
        return cfg

    def test_unknown_host_fails_with_listing(self) -> None:
        from uxon.app.listing import _do_list_host

        cfg = self._cfg_with_hosts(["a", "b"])
        args = ParsedArgs(action="list", host="missing")
        with mock.patch("uxon.errors.eprint") as eprint:
            with self.assertRaises(SystemExit):
                _do_list_host(args, cfg)
        # Error message lists the configured hosts so the operator can
        # see what they typo'd against.
        msg = eprint.call_args[0][0]
        self.assertIn("missing", msg)
        self.assertIn("a, b", msg)

    def test_no_remote_hosts_configured_fails(self) -> None:
        from uxon.app.listing import _do_list_host

        cfg = _make_config()
        cfg.remote_hosts = []
        args = ParsedArgs(action="list", host="any")
        with mock.patch("uxon.errors.eprint") as eprint:
            with self.assertRaises(SystemExit):
                _do_list_host(args, cfg)
        self.assertIn("no [[remote_hosts]]", eprint.call_args[0][0])

    def test_host_json_envelope_carries_host_field(self) -> None:
        from uxon.app.listing import _do_list_host
        from uxon.domain.wire_schema import RemoteSnapshot

        cfg = self._cfg_with_hosts(["vz-prod1"])
        args = ParsedArgs(action="list", host="vz-prod1", json_output=True)
        snap = RemoteSnapshot(
            host_name="vz-prod1",
            fetched_at_epoch=1.0,
            from_cache=False,
            error=None,
            sessions=[{"name": "uxon-foo@claude", "user": "alice"}],
            cached_at_epoch=1.0,
        )
        with mock.patch("uxon.infra.remote.collector.fetch_remote_snapshot", return_value=snap):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = _do_list_host(args, cfg)
        self.assertEqual(rc, 0)
        env = json.loads(buf.getvalue())
        self.assertEqual(env["kind"], "list")
        # The envelope-level ``host`` field attributes the snapshot
        # to the named peer; absent on local listings.
        self.assertEqual(env["host"], "vz-prod1")
        self.assertEqual(env["data"]["sessions"], snap.sessions)

    def test_host_failure_with_no_cache_returns_nonzero(self) -> None:
        from uxon.app.listing import _do_list_host
        from uxon.domain.wire_schema import RemoteSnapshot

        cfg = self._cfg_with_hosts(["vz-prod1"])
        args = ParsedArgs(action="list", host="vz-prod1", json_output=True)
        snap = RemoteSnapshot(
            host_name="vz-prod1",
            fetched_at_epoch=1.0,
            from_cache=False,
            error="ssh exited 255",
            sessions=[],
            cached_at_epoch=None,
        )
        with mock.patch("uxon.infra.remote.collector.fetch_remote_snapshot", return_value=snap):
            buf = io.StringIO()
            with redirect_stdout(buf):
                with mock.patch.object(listing_app, "eprint"):
                    rc = _do_list_host(args, cfg)
        # Failure with no cache: empty sessions, exit non-zero so the
        # operator's pipeline knows to investigate.
        self.assertEqual(rc, 1)
        env = json.loads(buf.getvalue())
        self.assertEqual(env["data"]["sessions"], [])

    def test_host_failure_with_cache_succeeds(self) -> None:
        # When the live fetch failed but the disk cache is populated,
        # the collector returns from_cache=True with the cached
        # sessions. We treat that as a soft success — still exit 0
        # so a watchdog doesn't page on every brief outage.
        from uxon.app.listing import _do_list_host
        from uxon.domain.wire_schema import RemoteSnapshot

        cfg = self._cfg_with_hosts(["vz-prod1"])
        args = ParsedArgs(action="list", host="vz-prod1", json_output=True)
        snap = RemoteSnapshot(
            host_name="vz-prod1",
            fetched_at_epoch=2.0,
            from_cache=True,
            error="ssh exited 255",
            sessions=[{"name": "uxon-cached@claude", "user": "bob"}],
            cached_at_epoch=1.0,
        )
        with mock.patch("uxon.infra.remote.collector.fetch_remote_snapshot", return_value=snap):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = _do_list_host(args, cfg)
        self.assertEqual(rc, 0)
        env = json.loads(buf.getvalue())
        self.assertEqual(len(env["data"]["sessions"]), 1)


class AllHostsJsonLinesTests(unittest.TestCase):
    """``--all-hosts --json`` MUST emit valid JSON Lines (one envelope
    per line, no internal newlines) so a downstream consumer can
    split on ``\\n`` and parse each record independently."""

    def test_each_envelope_is_one_line(self) -> None:
        from uxon.app.listing import _do_list_all_hosts
        from uxon.domain.wire_schema import RemoteSnapshot
        from uxon.infra.remote_hosts import RemoteHost

        cfg = _make_config()
        cfg.remote_hosts = [
            RemoteHost(name="a", ssh_alias="a", description="", remote_uxon="uxon"),
            RemoteHost(name="b", ssh_alias="b", description="", remote_uxon="uxon"),
        ]
        args = ParsedArgs(action="list", all_hosts=True, json_output=True)

        def _fake_fetch(host, **_kwargs) -> RemoteSnapshot:
            return RemoteSnapshot(
                host_name=host.name,
                fetched_at_epoch=1.0,
                from_cache=False,
                error=None,
                sessions=[{"name": f"uxon-{host.name}@claude", "user": "alice"}],
                cached_at_epoch=1.0,
            )

        with (
            mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[]),
            mock.patch(
                "uxon.infra.remote.collector.fetch_remote_snapshot", side_effect=_fake_fetch
            ),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = _do_list_all_hosts(args, cfg, "alice")
        self.assertEqual(rc, 0)
        # Must be one envelope per non-empty line. No interior
        # newlines inside an envelope (that would make json.loads on
        # a single line fail).
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        self.assertEqual(
            len(lines), 3, msg=f"expected 3 envelopes (local + 2 hosts), got {len(lines)}"
        )
        envs = [json.loads(ln) for ln in lines]
        self.assertEqual(envs[0]["kind"], "list")
        self.assertNotIn("host", envs[0])  # local envelope has no host attribute
        self.assertEqual(envs[1]["host"], "a")
        self.assertEqual(envs[2]["host"], "b")


class WireRoundTripTests(unittest.TestCase):
    """End-to-end producer ↔ consumer test: emit an envelope the way
    ``_emit_json`` / ``_list_data`` actually does, then feed the
    captured stdout through the collector's ``parse_envelope``. This
    catches drift between the two sides of the wire that the
    producer-only and consumer-only test suites would miss."""

    def test_local_list_payload_parses_in_collector(self) -> None:
        from uxon.infra.remote.envelope import parse_envelope

        cfg = _make_config()
        sessions = [_make_session("uxon-foo@claude"), _make_session("uxon-bar@claude")]
        buf = io.StringIO()
        with redirect_stdout(buf):
            listing_app._emit_json(
                "list", listing_app._list_data(cfg, sessions, ["u-vz"], all_users=False)
            )
        parsed, _scope_skipped, _host_stats, err = parse_envelope(buf.getvalue())
        self.assertIsNone(err)
        assert parsed is not None
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["short_id"], "foo@claude")
        self.assertEqual(parsed[1]["short_id"], "bar@claude")

    def test_compact_local_list_payload_parses_in_collector(self) -> None:
        # The JSON Lines compact form must also parse — the same
        # bytes a peer would emit when invoked with ``--all-hosts
        # --json`` from the local side.
        from uxon.infra.remote.envelope import parse_envelope

        cfg = _make_config()
        buf = io.StringIO()
        with redirect_stdout(buf):
            listing_app._emit_json(
                "list",
                listing_app._list_data(cfg, [_make_session()], ["u-vz"], all_users=False),
                compact=True,
            )
        parsed, _scope_skipped, _host_stats, err = parse_envelope(buf.getvalue())
        self.assertIsNone(err)
        assert parsed is not None
        self.assertEqual(len(parsed), 1)


if __name__ == "__main__":
    unittest.main()

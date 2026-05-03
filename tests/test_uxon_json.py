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

import uxon.cli as uxon


def _make_config(**overrides: object) -> uxon.Config:
    base: dict[str, object] = {
        "runtime_user": "",
        "default_launch_mode": "caller",
        "enable_all_users_list": False,
        "launch_user_by_caller": {},
        "session_users": [],
        "allowed_roots": ["/srv/repos"],
        "session_prefix": "uxon-",
        "legacy_session_prefixes": (),
        "enabled_agents": ("claude",),
        "default_agent": "claude",
        "agent_default_args": {"claude": (), "codex": (), "cursor": ()},
        "new_project_root": "/srv/repos",
        "repeat_noninteractive_mode": "fail",
        "tmux_socket_template": "/tmp/uxon-{user}.sock",
        "tui_refresh_interval_seconds": 2.0,
        "git_create_enabled": False,
        "default_git_remote_profile": "",
        "git_remote_profiles": [],
    }
    base.update(overrides)
    return uxon.Config(**base)  # type: ignore[arg-type]


def _make_session(name: str = "uxon-demo@claude") -> uxon.SessionInfo:
    return uxon.SessionInfo(
        user="u-vz",
        name=name,
        attached="0",
        windows="1",
        created="2026-05-03T12:00:00+00:00",
        last_attached="2026-05-03T12:30:00+00:00",
        pane_pids=(111,),
        active_pid=111,
        active_cmd="claude",
        active_path="/srv/repos/demo",
    )


class JsonFlagParsingTests(unittest.TestCase):
    """``--json`` is recognised on every action it is documented for,
    and not on actions where it has no defined meaning (run, attach,
    new — those would need a separate design for streaming output)."""

    def test_list_subcommand(self) -> None:
        self.assertTrue(uxon.parse_args(["list", "--json"]).json_output)
        self.assertTrue(uxon.parse_args(["list", "--all-users", "--json"]).json_output)

    def test_list_short_flag(self) -> None:
        self.assertTrue(uxon.parse_args(["-l", "--json"]).json_output)

    def test_version_subcommand_and_flags(self) -> None:
        self.assertTrue(uxon.parse_args(["version", "--json"]).json_output)
        self.assertTrue(uxon.parse_args(["-V", "--json"]).json_output)
        self.assertTrue(uxon.parse_args(["--version", "--json"]).json_output)

    def test_doctor_subcommand(self) -> None:
        self.assertTrue(uxon.parse_args(["doctor", "--json"]).json_output)

    def test_kill_subcommand_and_flag(self) -> None:
        a = uxon.parse_args(["kill", "uxon-foo@claude", "--json"])
        self.assertTrue(a.json_output)
        self.assertEqual(a.action, "kill")
        b = uxon.parse_args(["-k", "uxon-foo@claude", "--json", "--dry-run"])
        self.assertTrue(b.json_output)
        self.assertTrue(b.dry_run)

    def test_kill_all_subcommand_and_flag(self) -> None:
        self.assertTrue(uxon.parse_args(["kill-all", "--json", "--force"]).json_output)
        self.assertTrue(uxon.parse_args(["--killall", "--json", "--dry-run"]).json_output)

    def test_default_is_off(self) -> None:
        self.assertFalse(uxon.parse_args(["list"]).json_output)
        self.assertFalse(uxon.parse_args(["version"]).json_output)


class VersionJsonTests(unittest.TestCase):
    def test_emits_versioned_envelope(self) -> None:
        with (
            mock.patch.object(uxon, "read_repo_version", return_value="9.9.9"),
            mock.patch.object(uxon, "read_git_commit_short", return_value="deadbee"),
            mock.patch.object(uxon, "repo_is_dirty", return_value=False),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                uxon._emit_json("version", uxon._version_data())
        env = json.loads(buf.getvalue())
        self.assertEqual(env["schema_version"], "1")
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
            mock.patch.object(uxon, "read_repo_version", return_value="0.0.1"),
            mock.patch.object(uxon, "read_git_commit_short", return_value=None),
        ):
            data = uxon._version_data()
        self.assertIsNone(data["commit"])
        self.assertFalse(data["commit_dirty"])


class ListJsonTests(unittest.TestCase):
    def test_envelope_kind_and_session_records(self) -> None:
        cfg = _make_config()
        sessions = [_make_session("uxon-alpha@claude"), _make_session("uxon-beta@claude")]
        data = uxon._list_data(cfg, sessions, ["u-vz"], all_users=False)
        self.assertEqual(data["all_users"], False)
        self.assertEqual(data["scope_users"], ["u-vz"])
        self.assertEqual(data["session_prefix"], "uxon-")
        self.assertEqual(len(data["sessions"]), 2)
        self.assertEqual(data["sessions"][0]["short_id"], "alpha@claude")
        self.assertEqual(data["sessions"][1]["short_id"], "beta@claude")

    def test_empty_sessions_emits_empty_list(self) -> None:
        cfg = _make_config()
        data = uxon._list_data(cfg, [], ["u-vz"], all_users=False)
        self.assertEqual(data["sessions"], [])

    def test_all_users_flag_propagates(self) -> None:
        cfg = _make_config()
        data = uxon._list_data(cfg, [], ["alice", "bob"], all_users=True)
        self.assertTrue(data["all_users"])
        self.assertEqual(data["scope_users"], ["alice", "bob"])


class KillJsonTests(unittest.TestCase):
    def test_dry_run_emits_would_kill(self) -> None:
        cfg = _make_config()
        target = _make_session("uxon-demo@claude")
        args = uxon.ParsedArgs(
            action="kill", target_id="demo@claude", dry_run=True, json_output=True
        )
        with (
            mock.patch.object(uxon, "collect_sessions", return_value=[target]),
            mock.patch.object(uxon, "tmux_socket_path", return_value="/tmp/uxon-u-vz.sock"),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = uxon.do_kill(args, cfg, "u-vz")
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
        args = uxon.ParsedArgs(action="kill", target_id="demo@claude", json_output=True)
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with (
            mock.patch.object(uxon, "collect_sessions", return_value=[target]),
            mock.patch.object(uxon, "tmux_socket_path", return_value="/tmp/uxon-u-vz.sock"),
            mock.patch.object(uxon, "configured_tmux_base", return_value=["tmux"]),
            mock.patch.object(uxon, "run_cmd", return_value=completed),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = uxon.do_kill(args, cfg, "u-vz")
        self.assertEqual(rc, 0)
        env = json.loads(buf.getvalue())
        self.assertEqual(env["data"]["action"], "killed")
        self.assertFalse(env["data"]["dry_run"])


class KillAllJsonTests(unittest.TestCase):
    def test_no_sessions_emits_empty_envelope(self) -> None:
        cfg = _make_config()
        args = uxon.ParsedArgs(action="kill-all", force=True, json_output=True)
        with (
            mock.patch.object(uxon, "collect_sessions", return_value=[]),
            mock.patch.object(uxon, "tmux_socket_path", return_value="/tmp/uxon-u-vz.sock"),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = uxon.do_kill_all(args, cfg, "u-vz")
        self.assertEqual(rc, 0)
        env = json.loads(buf.getvalue())
        self.assertEqual(env["kind"], "kill-all")
        self.assertEqual(env["data"]["sessions"], [])

    def test_dry_run_lists_all_with_would_kill(self) -> None:
        cfg = _make_config()
        s1 = _make_session("uxon-a@claude")
        s2 = _make_session("uxon-b@claude")
        args = uxon.ParsedArgs(action="kill-all", dry_run=True, json_output=True)
        with (
            mock.patch.object(uxon, "collect_sessions", return_value=[s1, s2]),
            mock.patch.object(uxon, "tmux_socket_path", return_value="/tmp/uxon-u-vz.sock"),
            mock.patch.object(uxon, "configured_tmux_base", return_value=["tmux"]),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = uxon.do_kill_all(args, cfg, "u-vz")
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
        args = uxon.ParsedArgs(action="kill-all", json_output=True)
        with (
            mock.patch.object(uxon, "collect_sessions", return_value=[_make_session()]),
            mock.patch.object(uxon, "eprint") as eprint,
            self.assertRaises(SystemExit),
        ):
            uxon.do_kill_all(args, cfg, "u-vz")
        self.assertIn("--json requires", eprint.call_args[0][0])

    def test_failed_kill_records_failed_action(self) -> None:
        cfg = _make_config()
        s1 = _make_session("uxon-a@claude")
        args = uxon.ParsedArgs(action="kill-all", force=True, json_output=True)
        cp_fail = mock.Mock(returncode=1, stdout="", stderr="boom")
        with (
            mock.patch.object(uxon, "collect_sessions", return_value=[s1]),
            mock.patch.object(uxon, "tmux_socket_path", return_value="/tmp/uxon-u-vz.sock"),
            mock.patch.object(uxon, "configured_tmux_base", return_value=["tmux"]),
            mock.patch.object(uxon, "run_cmd", return_value=cp_fail),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = uxon.do_kill_all(args, cfg, "u-vz")
        self.assertEqual(rc, 0)
        env = json.loads(buf.getvalue())
        self.assertEqual(env["data"]["sessions"][0]["action"], "failed")


class HostFlagParsingTests(unittest.TestCase):
    """``--host`` / ``--all-hosts`` are recognised on ``list`` only and
    are mutually exclusive."""

    def test_host_with_value(self) -> None:
        a = uxon.parse_args(["list", "--host", "vz-prod1"])
        self.assertEqual(a.host, "vz-prod1")
        self.assertFalse(a.all_hosts)

    def test_all_hosts_flag(self) -> None:
        a = uxon.parse_args(["list", "--all-hosts"])
        self.assertTrue(a.all_hosts)
        self.assertIsNone(a.host)

    def test_host_requires_value(self) -> None:
        with self.assertRaises(SystemExit):
            uxon.parse_args(["list", "--host"])

    def test_host_and_all_hosts_mutually_exclusive(self) -> None:
        with self.assertRaises(SystemExit):
            uxon.parse_args(["list", "--host", "x", "--all-hosts"])

    def test_combines_with_json(self) -> None:
        a = uxon.parse_args(["list", "--host", "x", "--json"])
        self.assertEqual(a.host, "x")
        self.assertTrue(a.json_output)

    def test_default_off(self) -> None:
        a = uxon.parse_args(["list"])
        self.assertIsNone(a.host)
        self.assertFalse(a.all_hosts)


class HostDispatchTests(unittest.TestCase):
    def _cfg_with_hosts(self, hosts: list) -> uxon.Config:
        from uxon.remote_hosts import RemoteHost

        cfg = _make_config()
        cfg.remote_hosts = [
            RemoteHost(name=n, ssh_alias=n, description="", remote_uxon="uxon") for n in hosts
        ]
        return cfg

    def test_unknown_host_fails_with_listing(self) -> None:
        from uxon.cli import _do_list_host

        cfg = self._cfg_with_hosts(["a", "b"])
        args = uxon.ParsedArgs(action="list", host="missing")
        with mock.patch.object(uxon, "eprint") as eprint:
            with self.assertRaises(SystemExit):
                _do_list_host(args, cfg)
        # Error message lists the configured hosts so the operator can
        # see what they typo'd against.
        msg = eprint.call_args[0][0]
        self.assertIn("missing", msg)
        self.assertIn("a, b", msg)

    def test_no_remote_hosts_configured_fails(self) -> None:
        from uxon.cli import _do_list_host

        cfg = _make_config()
        cfg.remote_hosts = []
        args = uxon.ParsedArgs(action="list", host="any")
        with mock.patch.object(uxon, "eprint") as eprint:
            with self.assertRaises(SystemExit):
                _do_list_host(args, cfg)
        self.assertIn("no [[remote_hosts]]", eprint.call_args[0][0])

    def test_host_json_envelope_carries_host_field(self) -> None:
        from uxon.cli import _do_list_host
        from uxon.remote_collector import RemoteSnapshot

        cfg = self._cfg_with_hosts(["vz-prod1"])
        args = uxon.ParsedArgs(action="list", host="vz-prod1", json_output=True)
        snap = RemoteSnapshot(
            host_name="vz-prod1",
            fetched_at_epoch=1.0,
            from_cache=False,
            error=None,
            sessions=[{"name": "uxon-foo@claude", "user": "alice"}],
            cached_at_epoch=1.0,
        )
        with mock.patch("uxon.remote_collector.fetch_remote_snapshot", return_value=snap):
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
        from uxon.cli import _do_list_host
        from uxon.remote_collector import RemoteSnapshot

        cfg = self._cfg_with_hosts(["vz-prod1"])
        args = uxon.ParsedArgs(action="list", host="vz-prod1", json_output=True)
        snap = RemoteSnapshot(
            host_name="vz-prod1",
            fetched_at_epoch=1.0,
            from_cache=False,
            error="ssh exited 255",
            sessions=[],
            cached_at_epoch=None,
        )
        with mock.patch("uxon.remote_collector.fetch_remote_snapshot", return_value=snap):
            buf = io.StringIO()
            with redirect_stdout(buf):
                with mock.patch.object(uxon, "eprint"):
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
        from uxon.cli import _do_list_host
        from uxon.remote_collector import RemoteSnapshot

        cfg = self._cfg_with_hosts(["vz-prod1"])
        args = uxon.ParsedArgs(action="list", host="vz-prod1", json_output=True)
        snap = RemoteSnapshot(
            host_name="vz-prod1",
            fetched_at_epoch=2.0,
            from_cache=True,
            error="ssh exited 255",
            sessions=[{"name": "uxon-cached@claude", "user": "bob"}],
            cached_at_epoch=1.0,
        )
        with mock.patch("uxon.remote_collector.fetch_remote_snapshot", return_value=snap):
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
        from uxon.cli import _do_list_all_hosts
        from uxon.remote_collector import RemoteSnapshot
        from uxon.remote_hosts import RemoteHost

        cfg = _make_config()
        cfg.remote_hosts = [
            RemoteHost(name="a", ssh_alias="a", description="", remote_uxon="uxon"),
            RemoteHost(name="b", ssh_alias="b", description="", remote_uxon="uxon"),
        ]
        args = uxon.ParsedArgs(action="list", all_hosts=True, json_output=True)

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
            mock.patch.object(uxon, "collect_sessions", return_value=[]),
            mock.patch("uxon.remote_collector.fetch_remote_snapshot", side_effect=_fake_fetch),
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
    captured stdout through the collector's ``_parse_envelope``. This
    catches drift between the two sides of the wire that the
    producer-only and consumer-only test suites would miss."""

    def test_local_list_payload_parses_in_collector(self) -> None:
        from uxon.remote_collector import _parse_envelope

        cfg = _make_config()
        sessions = [_make_session("uxon-foo@claude"), _make_session("uxon-bar@claude")]
        buf = io.StringIO()
        with redirect_stdout(buf):
            uxon._emit_json("list", uxon._list_data(cfg, sessions, ["u-vz"], all_users=False))
        parsed, _scope_skipped, err = _parse_envelope(buf.getvalue())
        self.assertIsNone(err)
        assert parsed is not None
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["short_id"], "foo@claude")
        self.assertEqual(parsed[1]["short_id"], "bar@claude")

    def test_compact_local_list_payload_parses_in_collector(self) -> None:
        # The JSON Lines compact form must also parse — the same
        # bytes a peer would emit when invoked with ``--all-hosts
        # --json`` from the local side.
        from uxon.remote_collector import _parse_envelope

        cfg = _make_config()
        buf = io.StringIO()
        with redirect_stdout(buf):
            uxon._emit_json(
                "list",
                uxon._list_data(cfg, [_make_session()], ["u-vz"], all_users=False),
                compact=True,
            )
        parsed, _scope_skipped, err = _parse_envelope(buf.getvalue())
        self.assertIsNone(err)
        assert parsed is not None
        self.assertEqual(len(parsed), 1)


if __name__ == "__main__":
    unittest.main()

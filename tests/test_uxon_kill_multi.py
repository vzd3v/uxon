"""Tests for ``uxon kill --user`` and ``uxon kill --host`` (3.4.0).

The spec lives in ``CHANGELOG.md`` for 3.4.0:

- ``--user <name>``: per-target sudo gating, single-target probe;
  unreachable target emits the stable ``uxon-error: not-reachable``
  tag and exits 1.
- ``--host <alias>``: SSH-routed dispatch to a configured
  ``[[remote_hosts]]`` peer. The peer's own ``uxon kill`` does the
  per-target sudo gating; the local side never speaks the peer's
  user table. ``--force`` is always passed on the wire.
- Bulk kill (``kill-all``) stays strictly local; that constraint is
  not under test here, only the per-session kill paths.

Tests are unit-level — every sudo / SSH / tmux call is mocked.
"""

from __future__ import annotations

import io
import json
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import uxon.cli as uxon
from uxon import audit as uxon_audit
from uxon.remote_hosts import RemoteHost
from uxon.tui.context import SudoCapability


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


def _make_session(name: str, *, user: str = "u-vz") -> uxon.SessionInfo:
    return uxon.SessionInfo(
        user=user,
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


class ParseKillFlagsTests(unittest.TestCase):
    """Both ``kill <id> ...`` and ``-k <id> ...`` accept the same flags."""

    def test_subcommand_user_flag(self) -> None:
        a = uxon.parse_args(["kill", "demo@claude", "--user", "alice"])
        self.assertEqual(a.action, "kill")
        self.assertEqual(a.target_id, "demo@claude")
        self.assertEqual(a.user, "alice")
        self.assertIsNone(a.host)

    def test_subcommand_host_flag(self) -> None:
        a = uxon.parse_args(["kill", "demo@claude", "--host", "box-b"])
        self.assertEqual(a.host, "box-b")
        self.assertIsNone(a.user)

    def test_subcommand_user_and_host(self) -> None:
        a = uxon.parse_args(["kill", "demo@claude", "--host", "box-b", "--user", "alice"])
        self.assertEqual(a.host, "box-b")
        self.assertEqual(a.user, "alice")

    def test_short_form_user_and_host(self) -> None:
        a = uxon.parse_args(["-k", "demo@claude", "--user", "alice", "--host", "box-b", "--json"])
        self.assertEqual(a.action, "kill")
        self.assertEqual(a.user, "alice")
        self.assertEqual(a.host, "box-b")
        self.assertTrue(a.json_output)

    def test_force_flag(self) -> None:
        a = uxon.parse_args(["kill", "demo@claude", "--force"])
        self.assertTrue(a.force)

    def test_kill_requires_id(self) -> None:
        with self.assertRaises(SystemExit):
            uxon.parse_args(["kill"])
        with self.assertRaises(SystemExit):
            uxon.parse_args(["-k"])

    def test_unknown_extras_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            uxon.parse_args(["kill", "demo@claude", "--bogus"])

    def test_user_requires_value(self) -> None:
        with self.assertRaises(SystemExit):
            uxon.parse_args(["kill", "demo@claude", "--user"])


class KillUserLocalTests(unittest.TestCase):
    """``uxon kill --user <name>`` cross-user local path."""

    def test_user_equals_self_skips_probe(self) -> None:
        cfg = _make_config()
        target = _make_session("uxon-demo@claude")
        args = uxon.ParsedArgs(action="kill", target_id="demo@claude", user="u-vz")
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with (
            mock.patch.object(uxon, "collect_sessions", return_value=[target]),
            mock.patch.object(uxon, "configured_tmux_base", return_value=["tmux"]),
            mock.patch.object(uxon, "run_cmd", return_value=completed),
            mock.patch("uxon.sudo_probe.probe_sudo_capability") as probe,
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = uxon.do_kill(args, cfg, "u-vz")
        self.assertEqual(rc, 0)
        probe.assert_not_called()

    def test_user_other_reachable_kills_via_sudo(self) -> None:
        cfg = _make_config()
        target = _make_session("uxon-demo@claude", user="alice")
        args = uxon.ParsedArgs(action="kill", target_id="demo@claude", user="alice", force=True)
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        caps = SudoCapability(reachable_users=frozenset({"alice"}), can_root=False)
        with (
            mock.patch.object(uxon, "collect_sessions", return_value=[target]),
            mock.patch.object(uxon, "tmux_socket_path", return_value="/tmp/uxon-alice.sock"),
            mock.patch("uxon.sudo_probe.probe_sudo_capability", return_value=caps) as probe,
            mock.patch.object(uxon, "run_cmd", return_value=completed) as run,
        ):
            with mock.patch.object(uxon, "process_user", return_value="u-vz"):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = uxon.do_kill(args, cfg, "u-vz")
        self.assertEqual(rc, 0)
        probe.assert_called_once_with(["alice"])
        # The argv contains the non-interactive sudo prefix and kill-session.
        argv = run.call_args[0][0]
        # ``sudo -niu alice -- tmux ... kill-session -t uxon-demo@claude``
        self.assertEqual(argv[0:4], ["sudo", "-niu", "alice", "--"])
        self.assertIn("kill-session", argv)
        self.assertIn("uxon-demo@claude", argv)

    def test_user_other_unreachable_emits_error_tag(self) -> None:
        cfg = _make_config()
        args = uxon.ParsedArgs(action="kill", target_id="demo@claude", user="alice", force=True)
        caps = SudoCapability(reachable_users=frozenset(), can_root=False)
        with (
            mock.patch("uxon.sudo_probe.probe_sudo_capability", return_value=caps),
            mock.patch.object(uxon, "run_cmd") as run,
            mock.patch.object(uxon, "collect_sessions") as collect,
        ):
            err = io.StringIO()
            with redirect_stderr(err):
                rc = uxon.do_kill(args, cfg, "u-vz")
        self.assertEqual(rc, 1)
        self.assertIn("uxon-error: not-reachable", err.getvalue())
        run.assert_not_called()
        collect.assert_not_called()

    def test_user_dry_run_json_includes_target_user_and_reachable(self) -> None:
        cfg = _make_config()
        target = _make_session("uxon-demo@claude", user="alice")
        args = uxon.ParsedArgs(
            action="kill",
            target_id="demo@claude",
            user="alice",
            dry_run=True,
            json_output=True,
        )
        caps = SudoCapability(reachable_users=frozenset({"alice"}), can_root=False)
        with (
            mock.patch.object(uxon, "collect_sessions", return_value=[target]),
            mock.patch.object(uxon, "tmux_socket_path", return_value="/tmp/uxon-alice.sock"),
            mock.patch("uxon.sudo_probe.probe_sudo_capability", return_value=caps),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = uxon.do_kill(args, cfg, "u-vz")
        self.assertEqual(rc, 0)
        env = json.loads(buf.getvalue())
        self.assertEqual(env["kind"], "kill")
        self.assertEqual(env["data"]["target_user"], "alice")
        self.assertTrue(env["data"]["reachable"])
        self.assertEqual(env["data"]["action"], "would-kill")

    def test_user_dry_run_unreachable_emits_error_tag(self) -> None:
        # Critical regression: dry-run + unreachable used to fall through
        # to ``collect_sessions`` (which silently returns [] on a sudo
        # failure) and then ``resolve_session`` failed with a misleading
        # "no sessions found" exit 2. The contract is: if the target is
        # unreachable, surface the error tag and exit 1 even on dry-run.
        cfg = _make_config()
        args = uxon.ParsedArgs(
            action="kill",
            target_id="demo@claude",
            user="alice",
            dry_run=True,
        )
        caps = SudoCapability(reachable_users=frozenset(), can_root=False)
        with (
            mock.patch("uxon.sudo_probe.probe_sudo_capability", return_value=caps),
            mock.patch.object(uxon, "run_cmd") as run,
            mock.patch.object(uxon, "collect_sessions") as collect,
        ):
            err = io.StringIO()
            with redirect_stderr(err):
                rc = uxon.do_kill(args, cfg, "u-vz")
        self.assertEqual(rc, 1)
        self.assertIn("uxon-error: not-reachable", err.getvalue())
        run.assert_not_called()
        collect.assert_not_called()

    def test_json_without_force_or_dry_run_fails(self) -> None:
        cfg = _make_config()
        args = uxon.ParsedArgs(
            action="kill", target_id="demo@claude", user="alice", json_output=True
        )
        caps = SudoCapability(reachable_users=frozenset({"alice"}), can_root=False)
        with (
            mock.patch("uxon.sudo_probe.probe_sudo_capability", return_value=caps),
            mock.patch.object(uxon, "is_interactive_tty", return_value=False),
        ):
            with self.assertRaises(SystemExit):
                uxon.do_kill(args, cfg, "u-vz")

    def test_run_cmd_failure_emits_session_kill_outcome_error(self) -> None:
        # Regression for the failure-path emit added in commit bd9ba0c:
        # if ``tmux kill-session`` returns non-zero (sudo blockage,
        # tmux server gone, busy session), ``run_cmd(check=True)``
        # raises CalledProcessError. ``do_kill`` must emit
        # ``session.kill outcome=error`` with the captured rc *before*
        # re-raising — spec line 208.
        cfg = _make_config()
        target = _make_session("uxon-demo@claude")
        args = uxon.ParsedArgs(action="kill", target_id="demo@claude", user="u-vz")
        boom = subprocess.CalledProcessError(returncode=2, cmd=["tmux", "kill-session"])
        recorded: list[tuple[str, dict]] = []

        def fake_audit(event: str, *, outcome: str = "ok", **fields: object) -> None:
            recorded.append((event, {"outcome": outcome, **fields}))

        with (
            mock.patch.object(uxon, "collect_sessions", return_value=[target]),
            mock.patch.object(uxon, "configured_tmux_base", return_value=["tmux"]),
            mock.patch.object(uxon, "run_cmd", side_effect=boom),
            mock.patch.object(uxon_audit, "audit", side_effect=fake_audit),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                uxon.do_kill(args, cfg, "u-vz")

        kill_emits = [e for e in recorded if e[0] == "session.kill"]
        self.assertTrue(kill_emits, "no session.kill emit recorded")
        # The failure-path emit is the one that fired — outcome=error
        # with rc=2 from the captured CalledProcessError.
        outcomes = [fields["outcome"] for _, fields in kill_emits]
        self.assertIn("error", outcomes)
        err_emit = next(fields for _, fields in kill_emits if fields["outcome"] == "error")
        self.assertEqual(err_emit["rc"], 2)
        self.assertEqual(err_emit["session"], "uxon-demo@claude")
        self.assertEqual(err_emit["target_user"], "u-vz")


class KillHostRemoteTests(unittest.TestCase):
    """``uxon kill --host <alias>`` SSH-routed remote dispatch."""

    def _cfg_with_host(self) -> uxon.Config:
        return _make_config(
            remote_hosts=[
                RemoteHost(
                    name="box-b",
                    ssh_alias="ssh-b",
                    description="",
                    remote_uxon="uxon",
                )
            ]
        )

    def test_host_dry_run_prints_ssh_command(self) -> None:
        cfg = self._cfg_with_host()
        args = uxon.ParsedArgs(
            action="kill",
            target_id="demo@claude",
            host="box-b",
            dry_run=True,
        )
        with mock.patch.object(uxon.subprocess, "run") as srun:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = uxon.do_kill(args, cfg, "u-vz")
        self.assertEqual(rc, 0)
        srun.assert_not_called()
        out = buf.getvalue()
        self.assertIn("dry-run:", out)
        self.assertIn("ssh ", out)
        self.assertIn("ssh-b", out)
        self.assertIn("kill --force", out)
        self.assertIn("demo@claude", out)

    def test_host_with_user_appends_user_flag(self) -> None:
        cfg = self._cfg_with_host()
        args = uxon.ParsedArgs(
            action="kill",
            target_id="demo@claude",
            host="box-b",
            user="alice",
            dry_run=True,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = uxon.do_kill(args, cfg, "u-vz")
        self.assertEqual(rc, 0)
        self.assertIn("--user", buf.getvalue())
        self.assertIn("alice", buf.getvalue())

    def test_host_executes_ssh_with_expected_argv(self) -> None:
        cfg = self._cfg_with_host()
        args = uxon.ParsedArgs(action="kill", target_id="demo@claude", host="box-b", force=True)
        cp = mock.Mock(returncode=0, stdout="killed: uxon-demo@claude\n", stderr="")
        with mock.patch.object(uxon.subprocess, "run", return_value=cp) as srun:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = uxon.do_kill(args, cfg, "u-vz")
        self.assertEqual(rc, 0)
        srun.assert_called_once()
        argv = srun.call_args[0][0]
        # Expected SSH argv shape pinned by spec.
        self.assertEqual(argv[0], "ssh")
        self.assertIn("-o", argv)
        self.assertIn("BatchMode=yes", argv)
        # After unification onto build_peer_ssh_argv kill-remote shares
        # the default fetch template, which sets ServerAliveInterval=15.
        self.assertIn("ServerAliveInterval=15", argv)
        # ControlMaster=auto comes for free now — kill reuses the
        # warm master started by the poller.
        self.assertIn("ControlMaster=auto", argv)
        # ssh alias appears before the remote command string.
        ssh_alias_idx = argv.index("ssh-b")
        remote_cmd = argv[ssh_alias_idx + 1]
        self.assertIn("uxon", remote_cmd)
        self.assertIn("kill", remote_cmd)
        self.assertIn("--force", remote_cmd)
        self.assertIn("demo@claude", remote_cmd)
        # Peer stdout was forwarded.
        self.assertIn("killed: uxon-demo@claude", buf.getvalue())

    def test_host_user_combined_in_remote_cmd(self) -> None:
        cfg = self._cfg_with_host()
        args = uxon.ParsedArgs(
            action="kill",
            target_id="demo@claude",
            host="box-b",
            user="alice",
            force=True,
        )
        cp = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(uxon.subprocess, "run", return_value=cp) as srun:
            uxon.do_kill(args, cfg, "u-vz")
        argv = srun.call_args[0][0]
        remote_cmd = argv[-1]
        self.assertIn("--user", remote_cmd)
        self.assertIn("alice", remote_cmd)

    def test_host_honours_command_template(self) -> None:
        cfg = _make_config(
            remote_hosts=[
                RemoteHost(
                    name="box-b",
                    ssh_alias="ssh-b",
                    description="",
                    remote_uxon="uxon",
                    command_template=("ssh", "-J", "bastion", "{ssh_alias}", "{remote_command}"),
                )
            ]
        )
        args = uxon.ParsedArgs(action="kill", target_id="demo@claude", host="box-b", force=True)
        cp = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(uxon.subprocess, "run", return_value=cp) as srun:
            uxon.do_kill(args, cfg, "u-vz")
        argv = srun.call_args[0][0]
        # Bug fix: kill-remote now honours command_template.
        self.assertIn("-J", argv)
        self.assertIn("bastion", argv)

    def test_host_unknown_alias_exits_2_with_hint(self) -> None:
        cfg = self._cfg_with_host()
        args = uxon.ParsedArgs(action="kill", target_id="demo@claude", host="bogus", force=True)
        err = io.StringIO()
        with redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                uxon.do_kill(args, cfg, "u-vz")
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("configured:", err.getvalue())
        self.assertIn("box-b", err.getvalue())

    def test_host_no_remote_hosts_exits_2(self) -> None:
        cfg = _make_config()  # no remote_hosts
        args = uxon.ParsedArgs(action="kill", target_id="demo@claude", host="box-b", force=True)
        with self.assertRaises(SystemExit):
            uxon.do_kill(args, cfg, "u-vz")

    def test_host_peer_nonzero_rc_returns_1_and_forwards_stderr(self) -> None:
        cfg = self._cfg_with_host()
        args = uxon.ParsedArgs(action="kill", target_id="demo@claude", host="box-b", force=True)
        cp = mock.Mock(
            returncode=1,
            stdout="",
            stderr="uxon-error: not-reachable (cannot sudo -niu alice; ...)\n",
        )
        with mock.patch.object(uxon.subprocess, "run", return_value=cp):
            err = io.StringIO()
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = uxon.do_kill(args, cfg, "u-vz")
        self.assertEqual(rc, 1)
        # Peer's stderr surfaced unwrapped — the error tag must be parseable.
        self.assertIn("uxon-error: not-reachable", err.getvalue())

    def test_host_ssh_timeout_returns_1(self) -> None:
        cfg = self._cfg_with_host()
        args = uxon.ParsedArgs(action="kill", target_id="demo@claude", host="box-b", force=True)
        with mock.patch.object(
            uxon.subprocess,
            "run",
            side_effect=uxon.subprocess.TimeoutExpired(cmd=["ssh"], timeout=10),
        ):
            err = io.StringIO()
            with redirect_stderr(err):
                rc = uxon.do_kill(args, cfg, "u-vz")
        self.assertEqual(rc, 1)
        self.assertIn("ssh timeout", err.getvalue())

    def test_host_json_without_force_or_dry_run_fails(self) -> None:
        cfg = self._cfg_with_host()
        args = uxon.ParsedArgs(
            action="kill", target_id="demo@claude", host="box-b", json_output=True
        )
        with mock.patch.object(uxon, "is_interactive_tty", return_value=False):
            with self.assertRaises(SystemExit):
                uxon.do_kill(args, cfg, "u-vz")

    def test_host_dry_run_json_envelope_has_host(self) -> None:
        cfg = self._cfg_with_host()
        args = uxon.ParsedArgs(
            action="kill",
            target_id="demo@claude",
            host="box-b",
            user="alice",
            dry_run=True,
            json_output=True,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = uxon.do_kill(args, cfg, "u-vz")
        self.assertEqual(rc, 0)
        env = json.loads(buf.getvalue())
        self.assertEqual(env["kind"], "kill")
        self.assertEqual(env.get("host"), "box-b")
        self.assertEqual(env["data"]["target_user"], "alice")
        self.assertEqual(env["data"]["action"], "would-kill")


if __name__ == "__main__":
    unittest.main()

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
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from helpers import make_config as _make_config
from helpers import make_session as _make_session

import uxon.app.kill as kill_app
from uxon.cli.parsing import parse_args
from uxon.domain.args import ParsedArgs
from uxon.domain.config import Config
from uxon.domain.sudo import SudoCapability
from uxon.infra import audit as uxon_audit
from uxon.infra.remote_hosts import RemoteHost
from uxon.tui.bridge import TuiBridge


class ParseKillFlagsTests(unittest.TestCase):
    """Both ``kill <id> ...`` and ``-k <id> ...`` accept the same flags."""

    def test_subcommand_user_flag(self) -> None:
        a = parse_args(["kill", "demo@claude", "--user", "alice"])
        self.assertEqual(a.action, "kill")
        self.assertEqual(a.target_id, "demo@claude")
        self.assertEqual(a.user, "alice")
        self.assertIsNone(a.host)

    def test_subcommand_host_flag(self) -> None:
        a = parse_args(["kill", "demo@claude", "--host", "box-b"])
        self.assertEqual(a.host, "box-b")
        self.assertIsNone(a.user)

    def test_subcommand_user_and_host(self) -> None:
        a = parse_args(["kill", "demo@claude", "--host", "box-b", "--user", "alice"])
        self.assertEqual(a.host, "box-b")
        self.assertEqual(a.user, "alice")

    def test_short_form_user_and_host(self) -> None:
        a = parse_args(["-k", "demo@claude", "--user", "alice", "--host", "box-b", "--json"])
        self.assertEqual(a.action, "kill")
        self.assertEqual(a.user, "alice")
        self.assertEqual(a.host, "box-b")
        self.assertTrue(a.json_output)

    def test_force_flag(self) -> None:
        a = parse_args(["kill", "demo@claude", "--force"])
        self.assertTrue(a.force)

    def test_kill_requires_id(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(["kill"])
        with self.assertRaises(SystemExit):
            parse_args(["-k"])

    def test_unknown_extras_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(["kill", "demo@claude", "--bogus"])

    def test_user_requires_value(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(["kill", "demo@claude", "--user"])


class KillUserLocalTests(unittest.TestCase):
    """``uxon kill --user <name>`` cross-user local path."""

    def test_user_equals_self_skips_probe(self) -> None:
        cfg = _make_config()
        target = _make_session("uxon-demo@claude")
        args = ParsedArgs(action="kill", target_id="demo@claude", user="u-vz")
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with (
            mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[target]),
            mock.patch("uxon.infra.tmux.configured_tmux_base", return_value=["tmux"]),
            mock.patch("uxon.infra.process.run_cmd", return_value=completed),
            mock.patch("uxon.infra.sudo_probe.probe_sudo_capability") as probe,
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = kill_app.do_kill(args, cfg, "u-vz")
        self.assertEqual(rc, 0)
        probe.assert_not_called()

    def test_user_other_reachable_kills_via_sudo(self) -> None:
        cfg = _make_config()
        target = _make_session("uxon-demo@claude", user="alice")
        args = ParsedArgs(action="kill", target_id="demo@claude", user="alice", force=True)
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        caps = SudoCapability(reachable_users=frozenset({"alice"}), can_root=False)
        with (
            mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[target]),
            mock.patch("uxon.infra.tmux.tmux_socket_path", return_value="/tmp/uxon-alice.sock"),
            mock.patch("uxon.infra.sudo_probe.probe_sudo_capability", return_value=caps) as probe,
            mock.patch("uxon.infra.process.run_cmd", return_value=completed) as run,
        ):
            with mock.patch("uxon.infra.identity.process_user", return_value="u-vz"):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = kill_app.do_kill(args, cfg, "u-vz")
        self.assertEqual(rc, 0)
        probe.assert_called_once_with(cfg, ["alice"])
        # The argv contains the non-interactive sudo prefix and kill-session.
        argv = run.call_args[0][0]
        # argv-preserving sudo wraps the tmux kill command.
        self.assertEqual(argv[0:6], ["/usr/bin/sudo", "-n", "-H", "-u", "alice", "--"])
        self.assertIn("kill-session", argv)
        self.assertIn("uxon-demo@claude", argv)

    def test_user_other_unreachable_emits_error_tag(self) -> None:
        cfg = _make_config()
        args = ParsedArgs(action="kill", target_id="demo@claude", user="alice", force=True)
        caps = SudoCapability(reachable_users=frozenset(), can_root=False)
        with (
            mock.patch("uxon.infra.sudo_probe.probe_sudo_capability", return_value=caps),
            mock.patch("uxon.infra.process.run_cmd") as run,
            mock.patch("uxon.infra.sessions_probe.collect_sessions") as collect,
        ):
            err = io.StringIO()
            with redirect_stderr(err):
                rc = kill_app.do_kill(args, cfg, "u-vz")
        self.assertEqual(rc, 1)
        self.assertIn("uxon-error: not-reachable", err.getvalue())
        run.assert_not_called()
        collect.assert_not_called()

    def test_user_dry_run_json_includes_target_user_and_reachable(self) -> None:
        cfg = _make_config()
        target = _make_session("uxon-demo@claude", user="alice")
        args = ParsedArgs(
            action="kill",
            target_id="demo@claude",
            user="alice",
            dry_run=True,
            json_output=True,
        )
        caps = SudoCapability(reachable_users=frozenset({"alice"}), can_root=False)
        with (
            mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[target]),
            mock.patch("uxon.infra.tmux.tmux_socket_path", return_value="/tmp/uxon-alice.sock"),
            mock.patch("uxon.infra.sudo_probe.probe_sudo_capability", return_value=caps),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = kill_app.do_kill(args, cfg, "u-vz")
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
        args = ParsedArgs(
            action="kill",
            target_id="demo@claude",
            user="alice",
            dry_run=True,
        )
        caps = SudoCapability(reachable_users=frozenset(), can_root=False)
        with (
            mock.patch("uxon.infra.sudo_probe.probe_sudo_capability", return_value=caps),
            mock.patch("uxon.infra.process.run_cmd") as run,
            mock.patch("uxon.infra.sessions_probe.collect_sessions") as collect,
        ):
            err = io.StringIO()
            with redirect_stderr(err):
                rc = kill_app.do_kill(args, cfg, "u-vz")
        self.assertEqual(rc, 1)
        self.assertIn("uxon-error: not-reachable", err.getvalue())
        run.assert_not_called()
        collect.assert_not_called()

    def test_json_without_force_or_dry_run_fails(self) -> None:
        cfg = _make_config()
        args = ParsedArgs(action="kill", target_id="demo@claude", user="alice", json_output=True)
        caps = SudoCapability(reachable_users=frozenset({"alice"}), can_root=False)
        with (
            mock.patch("uxon.infra.sudo_probe.probe_sudo_capability", return_value=caps),
            mock.patch("uxon.infra.identity.is_interactive_tty", return_value=False),
        ):
            with self.assertRaises(SystemExit):
                kill_app.do_kill(args, cfg, "u-vz")

    def test_peer_side_parses_remote_kill_argv_built_by_local(self) -> None:
        # Regression: ``_do_kill_remote`` and TUI ``on_remote_kill``
        # construct ``uxon kill <target> --force --user <u> --audit-correlation-id <uuid>``.
        # A previous shape put flags before ``<target>``, which made
        # ``parse_subcommand`` (which reads ``argv[1]`` as the target)
        # treat ``--force`` as the target and reject ``<target>`` as
        # an unknown arg. Peer-side parse of the new shape must succeed.
        argv = [
            "kill",
            "demo@claude",
            "--force",
            "--user",
            "alice",
            "--audit-correlation-id",
            "8f3c2d4e-1a6b-4c5e-9f7d-0a1b2c3d4e5f",
        ]
        parsed = parse_args(argv)
        self.assertEqual(parsed.action, "kill")
        self.assertEqual(parsed.target_id, "demo@claude")
        self.assertEqual(parsed.user, "alice")
        self.assertTrue(parsed.force)
        self.assertEqual(parsed.audit_correlation_id, "8f3c2d4e-1a6b-4c5e-9f7d-0a1b2c3d4e5f")

    def test_run_cmd_failure_emits_session_kill_outcome_error(self) -> None:
        # When ``tmux kill-session`` exits non-zero (sudo blockage, tmux
        # server gone, busy session), ``do_kill`` must emit
        # ``session.kill outcome=error`` with the command's real rc *before*
        # the failure propagates.
        #
        # Contract note: the kill spawn returns a non-zero
        # ``CompletedProcess`` — it does NOT raise. ``run_cmd(check=True)``
        # would fail via ``fail() -> SystemExit`` (never CalledProcessError),
        # so the failure path runs the command with ``check=False`` and
        # translates the rc itself. The earlier version of this test stubbed
        # ``run_cmd`` to raise ``CalledProcessError`` — an exception the real
        # ``run_cmd`` never produces — which masked a dead ``except`` that
        # silently dropped the error audit.
        cfg = _make_config()
        target = _make_session("uxon-demo@claude")
        args = ParsedArgs(action="kill", target_id="demo@claude", user="u-vz")
        nonzero = mock.Mock(returncode=2, stdout="", stderr="tmux: no server running")
        recorded: list[tuple[str, dict]] = []

        def fake_audit(event: str, *, outcome: str = "ok", **fields: object) -> None:
            recorded.append((event, {"outcome": outcome, **fields}))

        with (
            mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[target]),
            mock.patch("uxon.infra.tmux.configured_tmux_base", return_value=["tmux"]),
            mock.patch("uxon.infra.process.run_cmd", return_value=nonzero),
            mock.patch.object(uxon_audit, "audit", side_effect=fake_audit),
        ):
            with self.assertRaises(SystemExit):
                kill_app.do_kill(args, cfg, "u-vz")

        kill_emits = [e for e in recorded if e[0] == "session.kill"]
        # Exactly one ``session.kill`` emit must fire on this path —
        # the failure-path one with ``outcome=error``.  No spurious
        # ``ok`` emit may slip in before the raise; asserting the full
        # outcome list (rather than ``assertIn("error", …)``) catches a
        # future regression where someone reorders the emit above
        # ``run_cmd`` and ships a phantom success record.
        outcomes = [fields["outcome"] for _, fields in kill_emits]
        self.assertEqual(outcomes, ["error"])
        err_emit = next(fields for _, fields in kill_emits if fields["outcome"] == "error")
        self.assertEqual(err_emit["rc"], 2)
        self.assertEqual(err_emit["session"], "uxon-demo@claude")
        self.assertEqual(err_emit["target_user"], "u-vz")

    def test_session_kill_audit_includes_profile_agent_and_target_user(self) -> None:
        cfg = _make_config()
        target = _make_session("uxon-demo@claude_fast")
        target.profile = "claude_fast"
        target.agent = "claude"
        target.launch_record_verified = True
        args = ParsedArgs(action="kill", target_id="demo@claude_fast", user="u-vz", dry_run=True)
        recorded: list[tuple[str, dict]] = []

        def fake_audit(event: str, *, outcome: str = "ok", **fields: object) -> None:
            recorded.append((event, {"outcome": outcome, **fields}))

        with (
            mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[target]),
            mock.patch("uxon.infra.tmux.configured_tmux_base", return_value=["tmux"]),
            mock.patch.object(uxon_audit, "audit", side_effect=fake_audit),
        ):
            with redirect_stdout(io.StringIO()):
                rc = kill_app.do_kill(args, cfg, "u-vz")

        self.assertEqual(rc, 0)
        [event] = [fields for name, fields in recorded if name == "session.kill"]
        self.assertEqual(event["session"], "uxon-demo@claude_fast")
        self.assertEqual(event["target_user"], "u-vz")
        self.assertEqual(event["profile"], "claude_fast")
        self.assertEqual(event["agent"], "claude")

    def test_post_kill_cleanup_failure_emits_one_terminal_error(self) -> None:
        cfg = _make_config()
        target = _make_session("uxon-demo@claude")
        recorded: list[tuple[str, dict]] = []

        def fake_audit(event: str, *, outcome: str = "ok", **fields: object) -> None:
            recorded.append((event, {"outcome": outcome, **fields}))

        with (
            mock.patch(
                "uxon.app.kill.cleanup_launch_record",
                side_effect=RuntimeError("private cleanup detail"),
            ),
            mock.patch.object(uxon_audit, "audit", side_effect=fake_audit),
            self.assertRaises(SystemExit),
        ):
            kill_app._complete_kill(
                cfg,
                target,
                None,
                audit_event="session.kill",
                target_user="u-vz",
                force=True,
                dry_run=False,
            )

        terminal = [fields for event, fields in recorded if event == "session.kill"]
        self.assertEqual([event["outcome"] for event in terminal], ["error"])
        self.assertEqual(terminal[0]["error"], "post-kill cleanup failed")
        self.assertNotIn("private cleanup detail", str(terminal))


class KillEnvironmentClassificationTests(unittest.TestCase):
    """Transport environment never changes the target operation event."""

    def test_ssh_environment_does_not_reclassify_denied_kill(self) -> None:
        cfg = _make_config()
        args = ParsedArgs(action="kill", target_id="demo@claude", user="alice", force=True)
        caps = SudoCapability(reachable_users=frozenset(), can_root=False)
        recorded: list[tuple[str, dict]] = []

        def fake_audit(event: str, *, outcome: str = "ok", **fields: object) -> None:
            recorded.append((event, {"outcome": outcome, **fields}))

        with (
            mock.patch.dict("os.environ", {"SSH_CONNECTION": "1.2.3.4 22 5.6.7.8 22"}),
            mock.patch("uxon.infra.sudo_probe.probe_sudo_capability", return_value=caps),
            mock.patch.object(uxon_audit, "audit", side_effect=fake_audit),
            mock.patch("sys.stderr", new_callable=io.StringIO),
        ):
            rc = kill_app.do_kill(args, cfg, "u-vz")

        self.assertEqual(rc, 1)
        local_emits = [e for e in recorded if e[0] == "session.kill"]
        self.assertEqual(len(local_emits), 1)
        self.assertEqual(local_emits[0][1]["outcome"], "denied")
        self.assertEqual(local_emits[0][1]["session"], "demo@claude")
        self.assertEqual(local_emits[0][1]["target_user"], "alice")
        self.assertEqual(local_emits[0][1]["force"], True)


class KillCleanupAuditFinalizationTests(unittest.TestCase):
    def _recorded(self) -> tuple[list[tuple[str, dict]], object]:
        recorded: list[tuple[str, dict]] = []

        def fake_audit(event: str, *, outcome: str = "ok", **fields: object) -> None:
            recorded.append((event, {"outcome": outcome, **fields}))

        return recorded, fake_audit

    def test_cli_bulk_cleanup_failure_has_one_terminal_error(self) -> None:
        cfg = _make_config()
        target = _make_session("uxon-demo@claude")
        recorded, fake_audit = self._recorded()
        with (
            mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[target]),
            mock.patch("uxon.infra.tmux.configured_tmux_base", return_value=["tmux"]),
            mock.patch("uxon.infra.process.run_cmd", return_value=mock.Mock(returncode=0)),
            mock.patch("uxon.app.kill.prepare_runtime_teardown", return_value=None),
            mock.patch("uxon.app.kill.finish_killed_session", return_value=False),
            mock.patch.object(uxon_audit, "audit", side_effect=fake_audit),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                kill_app.do_kill_all(ParsedArgs(action="kill-all", force=True), cfg, "u-vz"),
                0,
            )
        terminal = [fields for event, fields in recorded if event == "session.kill_all"]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["outcome"], "error")
        self.assertEqual(terminal[0]["cleanup_failed_count"], 1)
        self.assertEqual(terminal[0]["killed_count"], 1)

    def test_tui_single_cleanup_failure_has_one_terminal_error(self) -> None:
        cfg = _make_config()
        target = _make_session("uxon-demo@claude")
        recorded, fake_audit = self._recorded()
        bridge = TuiBridge(cfg, "operator", "u-vz", "/srv/repos")
        with (
            mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[target]),
            mock.patch("uxon.infra.tmux.configured_tmux_base", return_value=["tmux"]),
            mock.patch("uxon.infra.process.run_cmd", return_value=mock.Mock(returncode=0)),
            mock.patch("uxon.app.kill.prepare_runtime_teardown", return_value=None),
            mock.patch("uxon.app.kill.cleanup_launch_record", side_effect=OSError("private")),
            mock.patch.object(uxon_audit, "audit", side_effect=fake_audit),
            self.assertRaises(SystemExit),
        ):
            bridge.on_kill("u-vz", target.name)
        terminal = [fields for event, fields in recorded if event == "session.kill"]
        self.assertEqual([event["outcome"] for event in terminal], ["error"])
        self.assertNotIn("private", str(terminal))

    def test_tui_bulk_cleanup_failure_has_one_terminal_error(self) -> None:
        cfg = _make_config()
        target = _make_session("uxon-demo@claude")
        recorded, fake_audit = self._recorded()
        bridge = TuiBridge(cfg, "operator", "u-vz", "/srv/repos")
        with (
            mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[target]),
            mock.patch("uxon.infra.tmux.configured_tmux_base", return_value=["tmux"]),
            mock.patch("uxon.infra.process.run_cmd", return_value=mock.Mock(returncode=0)),
            mock.patch("uxon.app.kill.prepare_runtime_teardown", return_value=None),
            mock.patch("uxon.app.kill.finish_killed_session", return_value=False),
            mock.patch.object(uxon_audit, "audit", side_effect=fake_audit),
        ):
            bridge.on_kill_all()
        terminal = [fields for event, fields in recorded if event == "session.kill_all"]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["outcome"], "error")
        self.assertEqual(terminal[0]["cleanup_failed_count"], 1)


class KillHostRemoteTests(unittest.TestCase):
    """``uxon kill --host <alias>`` SSH-routed remote dispatch."""

    def _cfg_with_host(self) -> Config:
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
        args = ParsedArgs(
            action="kill",
            target_id="demo@claude",
            host="box-b",
            dry_run=True,
        )
        with mock.patch.object(kill_app.subprocess, "run") as srun:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = kill_app.do_kill(args, cfg, "u-vz")
        self.assertEqual(rc, 0)
        srun.assert_not_called()
        out = buf.getvalue()
        self.assertIn("dry-run:", out)
        self.assertIn("ssh ", out)
        self.assertIn("ssh-b", out)
        self.assertIn("kill demo@claude --force", out)
        self.assertIn("demo@claude", out)

    def test_host_with_user_appends_user_flag(self) -> None:
        cfg = self._cfg_with_host()
        args = ParsedArgs(
            action="kill",
            target_id="demo@claude",
            host="box-b",
            user="alice",
            dry_run=True,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = kill_app.do_kill(args, cfg, "u-vz")
        self.assertEqual(rc, 0)
        self.assertIn("--user", buf.getvalue())
        self.assertIn("alice", buf.getvalue())

    def test_host_executes_ssh_with_expected_argv(self) -> None:
        cfg = self._cfg_with_host()
        args = ParsedArgs(action="kill", target_id="demo@claude", host="box-b", force=True)
        cp = mock.Mock(returncode=0, stdout="killed: uxon-demo@claude\n", stderr="")
        with mock.patch.object(kill_app.subprocess, "run", return_value=cp) as srun:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = kill_app.do_kill(args, cfg, "u-vz")
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
        args = ParsedArgs(
            action="kill",
            target_id="demo@claude",
            host="box-b",
            user="alice",
            force=True,
        )
        cp = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(kill_app.subprocess, "run", return_value=cp) as srun:
            kill_app.do_kill(args, cfg, "u-vz")
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
        args = ParsedArgs(action="kill", target_id="demo@claude", host="box-b", force=True)
        cp = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(kill_app.subprocess, "run", return_value=cp) as srun:
            kill_app.do_kill(args, cfg, "u-vz")
        argv = srun.call_args[0][0]
        # Bug fix: kill-remote now honours command_template.
        self.assertIn("-J", argv)
        self.assertIn("bastion", argv)

    def test_host_unknown_alias_exits_2_with_hint(self) -> None:
        cfg = self._cfg_with_host()
        args = ParsedArgs(action="kill", target_id="demo@claude", host="bogus", force=True)
        err = io.StringIO()
        with redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                kill_app.do_kill(args, cfg, "u-vz")
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("configured:", err.getvalue())
        self.assertIn("box-b", err.getvalue())

    def test_host_no_remote_hosts_exits_2(self) -> None:
        cfg = _make_config()  # no remote_hosts
        args = ParsedArgs(action="kill", target_id="demo@claude", host="box-b", force=True)
        with self.assertRaises(SystemExit):
            kill_app.do_kill(args, cfg, "u-vz")

    def test_host_peer_nonzero_rc_returns_1_and_forwards_stderr(self) -> None:
        cfg = self._cfg_with_host()
        args = ParsedArgs(action="kill", target_id="demo@claude", host="box-b", force=True)
        cp = mock.Mock(
            returncode=1,
            stdout="",
            stderr="uxon-error: not-reachable (cannot sudo -n -H -u alice; ...)\n",
        )
        with mock.patch.object(kill_app.subprocess, "run", return_value=cp):
            err = io.StringIO()
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = kill_app.do_kill(args, cfg, "u-vz")
        self.assertEqual(rc, 1)
        # Peer's stderr surfaced unwrapped — the error tag must be parseable.
        self.assertIn("uxon-error: not-reachable", err.getvalue())

    def test_host_ssh_timeout_returns_1(self) -> None:
        cfg = self._cfg_with_host()
        args = ParsedArgs(action="kill", target_id="demo@claude", host="box-b", force=True)
        with (
            mock.patch.object(
                kill_app.subprocess,
                "run",
                side_effect=kill_app.subprocess.TimeoutExpired(cmd=["ssh"], timeout=10),
            ),
            # Recovery is best-effort and runs real ssh subprocesses by
            # default; pin it to a no-op here so the test stays
            # isolated from the local ssh setup, and assert that the
            # CLI kill path does invoke it on timeout (mirrors the
            # poller's wedge-recovery contract).
            mock.patch("uxon.infra.remote.master_recovery.recover_wedged_master") as recover,
        ):
            err = io.StringIO()
            with redirect_stderr(err):
                rc = kill_app.do_kill(args, cfg, "u-vz")
        self.assertEqual(rc, 1)
        self.assertIn("ssh timeout", err.getvalue())
        recover.assert_called_once()

    def test_host_json_without_force_or_dry_run_fails(self) -> None:
        cfg = self._cfg_with_host()
        args = ParsedArgs(action="kill", target_id="demo@claude", host="box-b", json_output=True)
        with mock.patch("uxon.infra.identity.is_interactive_tty", return_value=False):
            with self.assertRaises(SystemExit):
                kill_app.do_kill(args, cfg, "u-vz")

    def test_host_dry_run_json_envelope_has_host(self) -> None:
        cfg = self._cfg_with_host()
        args = ParsedArgs(
            action="kill",
            target_id="demo@claude",
            host="box-b",
            user="alice",
            dry_run=True,
            json_output=True,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = kill_app.do_kill(args, cfg, "u-vz")
        self.assertEqual(rc, 0)
        env = json.loads(buf.getvalue())
        self.assertEqual(env["kind"], "kill")
        self.assertEqual(env.get("host"), "box-b")
        self.assertEqual(env["data"]["target_user"], "alice")
        self.assertEqual(env["data"]["action"], "would-kill")


if __name__ == "__main__":
    unittest.main()

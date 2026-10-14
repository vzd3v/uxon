import dataclasses
import io
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import uxon.app.attach as attach_app
import uxon.app.session_handoff as session_handoff
import uxon.cli as uxon_cli
from uxon.cli.main import do_interactive, main
from uxon.domain import session as domain_session
from uxon.domain.args import ParsedArgs
from uxon.domain.config import Config
from uxon.domain.launch_profiles import (
    GitRemotePolicy,
    LaunchConfig,
    ResolvedLaunchProfile,
    builtin_launch_profiles,
)
from uxon.errors import fail
from uxon.infra import (
    config_loader,
    sessions_probe,
    tmux,
)

UXON_PATH = Path(uxon_cli.__file__).resolve()


def _launch_for_test(
    agents,
    *,
    default_profile: str = "claude",
    enabled_profiles: tuple[str, ...] = (),
    git_default: str = "",
    git_allowed: tuple[str, ...] = (),
) -> LaunchConfig:
    profiles = builtin_launch_profiles(agents)
    if default_profile in profiles and (git_default or git_allowed):
        profiles[default_profile] = dataclasses.replace(
            profiles[default_profile],
            git_remote=GitRemotePolicy(
                allowed_profiles=git_allowed or ((git_default,) if git_default else ()),
                default_profile=git_default,
            ),
        )
    return LaunchConfig(
        enabled_profiles=enabled_profiles,
        default_profile=default_profile if default_profile in profiles else "",
        profiles=profiles,
    )


def _resolved_for_test(
    cfg: Config,
    *,
    profile: str = "claude",
    launch_user: str = "dana_agent",
    mode: str = "normal",
    canonical_target: str = "/srv/repos/demo",
) -> ResolvedLaunchProfile:
    return ResolvedLaunchProfile(
        profile=cfg.launch.profiles[profile],
        agent=cfg.agents[cfg.launch.profiles[profile].agent],
        launch_user=launch_user,
        mode_id=mode,
        git_remote=cfg.launch.profiles[profile].git_remote,
        canonical_target=canonical_target,
    )


class _StubsChain:
    """Tiny helper to combine multiple ``mock.patch`` context managers into
    one ``with`` statement for readability in tests."""

    def __init__(self, *patches):
        self._patches = patches
        self._entered = []

    def __enter__(self):
        for p in self._patches:
            self._entered.append(p.__enter__())
        return self

    def __exit__(self, exc_type, exc, tb):
        for p in reversed(self._patches):
            p.__exit__(exc_type, exc, tb)
        return False


class UxonTests(unittest.TestCase):
    def make_config(self, **overrides) -> Config:
        agents = overrides.get("agents")
        if agents is None:
            from uxon.domain.agents import default_agent_catalog

            agents = default_agent_catalog()
        default_profile = overrides.pop("default_profile", "claude")
        enabled_profiles = tuple(overrides.pop("enabled_profiles", ()))
        git_default = str(overrides.pop("profile_git_default", ""))
        git_allowed = tuple(
            profile.name
            for profile in overrides.get("git_remote_profiles", [])  # type: ignore[union-attr]
        )
        launch = overrides.pop(
            "launch",
            _launch_for_test(
                agents,
                default_profile=default_profile,
                enabled_profiles=enabled_profiles,
                git_default=git_default,
                git_allowed=git_allowed,
            ),
        )
        defaults = dict(
            default_launch_user="",
            default_launch_mode="caller",
            enable_all_users_list=False,
            launch_user_by_caller={},
            session_users=[],
            allowed_roots=["/srv/repos"],
            session_prefix="uxon-",
            legacy_session_prefixes=(),
            new_project_root="/srv/repos",
            repeat_noninteractive_mode="fail",
            tmux_socket_template="/tmp/uxon-{user}.sock",
            tui_refresh_interval_seconds=2.0,
            git_create_enabled=False,
            git_remote_profiles=[],
            # Minimal baseline: tmux managed options OFF/empty so launch-argv
            # tests stay focused on the core invocation. The tmux tests opt in
            # explicitly; the on-by-default production value lives in
            # DEFAULT_CONFIG / the Config field defaults.
            tmux_manage_options=False,
            tmux_options={},
            tmux_server_options={},
            tmux_append_server_options={},
            agents=agents,
            launch=launch,
        )
        defaults.update(overrides)
        return Config(**defaults)

    def make_session(
        self,
        name: str,
        path: str,
        *,
        attached: str = "0",
    ) -> domain_session.SessionInfo:
        return domain_session.SessionInfo(
            user="alice",
            name=name,
            attached=attached,
            windows="1",
            created="2026-04-15T06:00:00+00:00",
            last_attached="2026-04-15T06:00:00+00:00",
            pane_pids=(),
            active_pid=1234,
            active_cmd="claude",
            active_path=path,
            launch_record_verified=True,
        )

    def _stub_socket_path(self):
        # Classic tests run as if the process is NOT inside tmux so the
        # build_request helpers stay on the execvp / attach-session /
        # new-session path.
        return _StubsChain(
            mock.patch("uxon.infra.tmux.tmux_socket_path", return_value="/tmp/uxon-test.sock"),
            mock.patch("uxon.infra.tmux.tmux_host_socket", return_value=None),
        )

    def test_attach_session_cli_waits_for_the_interactive_client(self) -> None:
        cfg = self.make_config()
        target = self.make_session("uxon-demo", "/srv/repos/demo")
        with self._stub_socket_path():
            with (
                mock.patch.object(attach_app.tmux, "require_tmux_server"),
                mock.patch.object(
                    tmux, "prepare_diagnostics_attach", side_effect=lambda req: req.cmd
                ),
                mock.patch("uxon.app.session_handoff.subprocess.call", return_value=0) as spawn,
            ):
                attach_app.attach_session(target, cfg, "alice")
        spawn.assert_called_once()
        argv = spawn.call_args[0][0]
        self.assertIn("attach-session", argv)
        self.assertIn("uxon-demo", argv)

    def test_handoff_preparation_error_is_returned_with_its_message(self) -> None:
        req = tmux.LaunchRequest(("tmux", "attach-session"))
        with (
            mock.patch.object(
                tmux,
                "prepare_diagnostics_attach",
                side_effect=lambda _: fail("cannot read terminal settings", 1),
            ),
            mock.patch("uxon.app.session_handoff.subprocess.call") as spawn,
        ):
            result = session_handoff.run_launch_request(req)
        self.assertEqual(
            (result.rc, result.stage, result.warning),
            (1, "prepare", "cannot read terminal settings"),
        )
        spawn.assert_not_called()

    def test_handoff_preserves_unattributed_system_exit(self) -> None:
        req = tmux.LaunchRequest(("tmux", "attach-session"))
        with mock.patch.object(tmux, "prepare_diagnostics_attach", side_effect=SystemExit(0)):
            with self.assertRaises(SystemExit) as caught:
                session_handoff.run_launch_request(req)
        self.assertEqual(caught.exception.code, 0)

    def test_dismissal_failure_returns_to_menu_with_warning(self) -> None:
        from uxon.domain.launch_request import TmuxHandoff

        req = tmux.LaunchRequest(
            ("tmux", "attach-session"), handoff=TmuxHandoff(self.make_config(), "alice")
        )
        with (
            mock.patch("uxon.app.session_handoff.subprocess.call", return_value=125),
            mock.patch.object(
                session_handoff,
                "_dismiss_diagnostics",
                side_effect=lambda _: fail("cannot verify terminal identity"),
            ),
        ):
            result = session_handoff.run_launch_request(req)
        self.assertEqual((result.rc, result.warning), (0, "cannot verify terminal identity"))

    def test_launch_in_tmux_cli_prepares_then_waits_for_client(self) -> None:
        cfg = self.make_config()
        args = ParsedArgs(action="run", agent_args=[])
        resolved = _resolved_for_test(cfg)
        with self._stub_socket_path():
            with mock.patch("uxon.infra.process.run_cmd") as run_cmd:
                with mock.patch("uxon.infra.tmux.prepare_managed_launch") as prepare:
                    with mock.patch(
                        "uxon.app.session_handoff.subprocess.call", return_value=0
                    ) as spawn:
                        session_handoff.launch_in_tmux(
                            "/srv/repos/demo",
                            "uxon-demo",
                            args,
                            cfg,
                            None,
                            resolved_profile=resolved,
                        )
        run_cmd.assert_not_called()
        prepare.assert_called_once()
        spawn.assert_called_once()

    def test_managed_launch_finalization_failure_kills_created_session(self) -> None:
        from uxon.infra import launch_records

        pending = launch_records.PendingLaunchRecord(
            socket_path="/tmp/uxon-test.sock",
            session_name="uxon-demo@claude",
            launch_nonce="nonce",
            launch_profile="claude",
            agent="claude",
            launch_user="dana_agent",
        )
        managed = tmux.ManagedTmuxLaunch(
            create_cmd=("tmux", "new-session", "-d", "-s", pending.session_name),
            query_cmd=("tmux", "display-message", "-p"),
            release_cmd=("tmux", "wait-for", "-S", "uxon-launch-nonce-release"),
            rollback_kill_prefix=("tmux", "kill-session", "-t"),
            record_socket=pending.socket_path,
            record_session=pending.session_name,
            record_nonce=pending.launch_nonce,
            record_dir="/tmp/uxon-launch-records-test",
            launch_profile=pending.launch_profile,
            agent=pending.agent,
            launch_user=pending.launch_user,
        )
        req = tmux.LaunchRequest(
            cmd=("tmux", "attach-session", "-t", pending.session_name), managed=managed
        )

        class CP:
            def __init__(self, returncode=0, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        calls: list[tuple[str, ...]] = []

        def fake_run_cmd(cmd, check=True, **_kwargs):
            calls.append(tuple(cmd))
            if len(calls) in {2, 3}:
                return CP(stdout="$1\t123\tuxon-demo@claude\tnonce\n")
            return CP()

        with (
            mock.patch("uxon.infra.launch_records.create_pending_record"),
            mock.patch(
                "uxon.infra.launch_records.finalize_pending_record",
                side_effect=RuntimeError("boom"),
            ),
            mock.patch("uxon.infra.launch_records.fail_pending_record") as fail_pending,
            mock.patch("uxon.infra.process.run_cmd", fake_run_cmd),
        ):
            with self.assertRaises(RuntimeError):
                tmux.prepare_managed_launch(req, pending)

        self.assertIn((*managed.rollback_kill_prefix, "$1"), calls)
        fail_pending.assert_called_once_with(
            pending, override_dir=Path("/tmp/uxon-launch-records-test"), shared=False
        )

    def test_managed_launch_releases_only_after_record_finalization(self) -> None:
        from uxon.infra import launch_records

        pending = launch_records.PendingLaunchRecord(
            socket_path="/tmp/uxon-test.sock",
            session_name="uxon-demo@claude",
            launch_nonce="abcdefghijklmnop",
            launch_profile="claude",
            agent="claude",
            launch_user="dana_agent",
        )
        managed = tmux.ManagedTmuxLaunch(
            create_cmd=("tmux", "new-session"),
            query_cmd=("tmux", "display-message"),
            release_cmd=("tmux", "wait-for", "-S", "uxon-launch-abcdefghijklmnop-release"),
            rollback_kill_prefix=("tmux", "kill-session", "-t"),
            record_socket=pending.socket_path,
            record_session=pending.session_name,
            record_nonce=pending.launch_nonce,
            record_dir="/tmp/uxon-launch-records-test",
            launch_profile=pending.launch_profile,
            agent=pending.agent,
            launch_user=pending.launch_user,
        )
        req = tmux.LaunchRequest(
            cmd=("tmux", "attach"),
            prelaunch=(("mkdir", "-p", "/tmp/uxon"),),
            managed=managed,
        )
        events: list[str] = []

        def fake_run(cmd, check=True, **kwargs):
            events.append(cmd[1])
            stdout = (
                "$1\t123\tuxon-demo@claude\tabcdefghijklmnop\n"
                if cmd == list(managed.query_cmd)
                else ""
            )
            return subprocess.CompletedProcess(cmd, 0, stdout, "")

        with (
            mock.patch(
                "uxon.infra.launch_records.create_pending_record",
                side_effect=lambda *a, **k: events.append("record-pending"),
            ),
            mock.patch(
                "uxon.infra.launch_records.finalize_pending_record",
                side_effect=lambda *a, **k: events.append("record-finalized"),
            ),
            mock.patch("uxon.infra.process.run_cmd", side_effect=fake_run),
        ):
            tmux.prepare_managed_launch(req, pending)

        self.assertEqual(
            events,
            [
                "record-pending",
                "-p",
                "new-session",
                "display-message",
                "record-finalized",
                "wait-for",
            ],
        )

    def test_managed_launch_name_conflict_fails_pending_record(self) -> None:
        from uxon.infra import launch_records

        pending = launch_records.PendingLaunchRecord(
            socket_path="/tmp/uxon-test.sock",
            session_name="uxon-demo@claude",
            launch_nonce="nonce",
            launch_profile="claude",
            agent="claude",
            launch_user="dana_agent",
        )
        managed = tmux.ManagedTmuxLaunch(
            create_cmd=("tmux", "new-session", "-d", "-s", pending.session_name),
            query_cmd=("tmux", "display-message", "-p"),
            release_cmd=("tmux", "wait-for", "-S", "uxon-launch-nonce-release"),
            rollback_kill_prefix=("tmux", "kill-session", "-t"),
            record_socket=pending.socket_path,
            record_session=pending.session_name,
            record_nonce=pending.launch_nonce,
            record_dir="/tmp/uxon-launch-records-test",
            launch_profile=pending.launch_profile,
            agent=pending.agent,
            launch_user=pending.launch_user,
        )
        req = tmux.LaunchRequest(
            cmd=("tmux", "attach-session", "-t", pending.session_name), managed=managed
        )

        class CP:
            returncode = 1
            stdout = ""
            stderr = "duplicate session"

        with (
            mock.patch("uxon.infra.launch_records.create_pending_record"),
            mock.patch("uxon.infra.launch_records.fail_pending_record") as fail_pending,
            mock.patch("uxon.infra.process.run_cmd", return_value=CP()),
        ):
            with self.assertRaises(SystemExit):
                tmux.prepare_managed_launch(req, pending)
        fail_pending.assert_called_once_with(
            pending, override_dir=Path("/tmp/uxon-launch-records-test"), shared=False
        )

    def test_launch_bootstrap_waits_for_release_before_exec(self) -> None:
        from uxon.infra import launch_bootstrap

        with (
            mock.patch(
                "uxon.infra.launch_bootstrap.run_query",
                side_effect=[
                    subprocess.TimeoutExpired(["tmux"], 0.01),
                    mock.Mock(returncode=0),
                    mock.Mock(returncode=0),
                ],
            ) as run,
            mock.patch.object(launch_bootstrap.os, "execvp") as execvp,
        ):
            rc = launch_bootstrap.wait_then_exec(
                socket_path="/tmp/sock",
                session_name="uxon-demo@claude",
                launch_nonce="abcdefghijklmnop",
                agent_argv=["claude"],
                timeout_seconds=0.01,
            )
        self.assertEqual(rc, 124)
        self.assertEqual(
            run.call_args_list[-1].args[0][-3:], ["kill-session", "-t", "uxon-demo@claude"]
        )
        execvp.assert_not_called()

    def test_launch_bootstrap_timeout_survives_cleanup_failure(self) -> None:
        from uxon.infra import launch_bootstrap

        with (
            mock.patch(
                "uxon.infra.launch_bootstrap.run_query",
                side_effect=[
                    subprocess.TimeoutExpired(["tmux"], 0.01),
                    OSError("tmux disappeared"),
                    OSError("tmux disappeared"),
                ],
            ),
            mock.patch.object(launch_bootstrap.os, "execvp") as execvp,
        ):
            rc = launch_bootstrap.wait_then_exec(
                socket_path="/tmp/sock",
                session_name="uxon-demo@claude",
                launch_nonce="abcdefghijklmnop",
                agent_argv=["claude"],
                timeout_seconds=0.01,
            )
        self.assertEqual(rc, 124)
        execvp.assert_not_called()

    def test_finalized_launch_record_contains_authority_fields(self) -> None:
        from uxon.infra import launch_records

        with tempfile.TemporaryDirectory() as tmp:
            pending = launch_records.PendingLaunchRecord(
                socket_path="/tmp/uxon-test.sock",
                session_name="uxon-demo@claude",
                launch_nonce="nonce",
                launch_profile="claude",
                agent="claude",
                launch_user="dana_agent",
                runtime="box",
                runtime_fingerprint="fp",
                runtime_resource="box-demo",
            )
            meta = launch_records.TmuxSessionMetadata(
                session_id="$1",
                created="123",
                name=pending.session_name,
                launch_nonce=pending.launch_nonce,
            )
            launch_records.create_pending_record(pending, override_dir=Path(tmp) / "records")
            launch_records.finalize_pending_record(
                pending,
                meta,
                runtime_id="cid",
                runtime_cgroup="/x.slice",
                runtime_epoch="1000",
                override_dir=Path(tmp) / "records",
            )
            record_path = launch_records.record_path(pending, override_dir=Path(tmp) / "records")
            self.assertEqual(stat.S_IMODE((Path(tmp) / "records").stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(record_path.stat().st_mode), 0o600)
            payload = launch_records.read_finalized_record(
                pending.socket_path,
                pending.session_name,
                pending.launch_nonce,
                override_dir=Path(tmp) / "records",
            )
        assert payload is not None
        self.assertEqual(payload["profile"], "claude")
        self.assertEqual(payload["agent"], "claude")
        self.assertEqual(payload["launch_user"], "dana_agent")
        self.assertEqual(payload["runtime"], "box")
        self.assertEqual(payload["runtime_fingerprint"], "fp")
        self.assertEqual(payload["runtime_resource"], "box-demo")
        self.assertEqual(payload["runtime_id"], "cid")
        self.assertEqual(payload["runtime_cgroup"], "/x.slice")
        self.assertEqual(payload["runtime_epoch"], "1000")

    def test_launch_record_lookup_does_not_trust_env_markers(self) -> None:
        from uxon.infra import launch_records

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {
                    "UXON_LAUNCH_PROFILE": "claude",
                    "UXON_LAUNCH_NONCE": "env-nonce",
                    "UXON_AGENT": "claude",
                },
                clear=False,
            ):
                payload = launch_records.read_finalized_record(
                    "/tmp/uxon-test.sock",
                    "uxon-demo@claude",
                    "env-nonce",
                    override_dir=Path(tmp) / "records",
                )
        self.assertIsNone(payload)


def _mk_session(
    name: str, path: str = "/srv/repos/x", agent: str = "claude", legacy: bool = False
) -> domain_session.SessionInfo:
    return domain_session.SessionInfo(
        user="u",
        name=name,
        attached="0",
        windows="1",
        created="-",
        last_attached="-",
        pane_pids=(),
        active_pid=None,
        active_cmd="",
        active_path=path,
        agent=agent,
        launch_record_verified=True,
        legacy=legacy,
    )


class SessionNamingTests(unittest.TestCase):
    """Tests for the uxon-<stem>@<profile> session naming scheme."""

    def test_parse_session_name_new(self) -> None:
        self.assertEqual(
            domain_session.parse_session_name("uxon-foo@codex"), ("foo", "codex", 1, False)
        )
        self.assertEqual(
            domain_session.parse_session_name("uxon-foo@codex-3"), ("foo", "codex", 3, False)
        )
        self.assertEqual(
            domain_session.parse_session_name("uxon-my-repo-branch@claude"),
            ("my-repo-branch", "claude", 1, False),
        )

    def test_parse_session_name_legacy_at_prefix(self) -> None:
        # ``ccw-`` sessions still parse when listed in ``legacy_prefixes`` and
        # are flagged ``legacy=True`` (default prefix is ``uxon-``).
        self.assertEqual(
            domain_session.parse_session_name("ccw-foo@codex", legacy_prefixes=("ccw-",)),
            ("foo", "codex", 1, True),
        )
        # When ``uxon-`` is the configured current prefix, the same-prefixed
        # name is not legacy.
        self.assertEqual(
            domain_session.parse_session_name("uxon-foo@codex", prefix="uxon-"),
            ("foo", "codex", 1, False),
        )
        # Without legacy_prefixes, a ccw- name does not match (default prefix
        # is uxon-).
        self.assertIsNone(domain_session.parse_session_name("ccw-foo@codex"))
        # And with a non-matching explicit prefix, uxon- is also unrecognised.
        self.assertIsNone(domain_session.parse_session_name("uxon-foo@codex", prefix="ccw-"))

    def test_parse_session_name_rejects_garbage(self) -> None:
        self.assertIsNone(domain_session.parse_session_name("random-x"))
        self.assertIsNone(domain_session.parse_session_name("uxon-foo"))  # missing @agent
        self.assertIsNone(
            domain_session.parse_session_name("cc-foo")
        )  # ancient format no longer recognised

    def test_candidate_session_name(self) -> None:
        self.assertEqual(
            domain_session.candidate_session_name("foo", 1, "cursor"), "uxon-foo@cursor"
        )
        self.assertEqual(
            domain_session.candidate_session_name("foo", 2, "cursor"), "uxon-foo@cursor-2"
        )

    def test_resolve_full_new(self) -> None:
        sessions = [_mk_session("uxon-foo@claude"), _mk_session("uxon-foo@codex", agent="codex")]
        self.assertEqual(
            sessions_probe.resolve_session("uxon-foo@codex", sessions, "uxon-").name,
            "uxon-foo@codex",
        )

    def test_resolve_suffixed_without_prefix(self) -> None:
        sessions = [_mk_session("uxon-foo@codex", agent="codex")]
        self.assertEqual(
            sessions_probe.resolve_session("foo@codex", sessions, "uxon-").name,
            "uxon-foo@codex",
        )

    def test_resolve_stem_unique(self) -> None:
        sessions = [_mk_session("uxon-foo@codex", agent="codex")]
        self.assertEqual(
            sessions_probe.resolve_session("foo", sessions, "uxon-").name,
            "uxon-foo@codex",
        )

    def test_resolve_stem_ambiguous(self) -> None:
        sessions = [_mk_session("uxon-foo@claude"), _mk_session("uxon-foo@codex", agent="codex")]
        with self.assertRaises(SystemExit):
            sessions_probe.resolve_session("foo", sessions, "uxon-")


class CliPreflightTests(unittest.TestCase):
    """Tests for CLI preflight probe in main()."""

    def test_ssh_environment_does_not_reclassify_denied_list(self) -> None:
        from uxon.infra import audit as uxon_audit

        recorded: list[tuple[str, dict]] = []

        def fake_audit(event: str, *, outcome: str = "ok", **fields: object) -> None:
            recorded.append((event, {"outcome": outcome, **fields}))

        cfg = Config(
            default_launch_user="",
            default_launch_mode="caller",
            enable_all_users_list=False,
            launch_user_by_caller={},
            session_users=[],
            allowed_roots=["/srv/repos"],
            session_prefix="uxon-",
            legacy_session_prefixes=(),
            new_project_root="/srv/repos",
            repeat_noninteractive_mode="fail",
            tmux_socket_template="/tmp/uxon-{user}.sock",
            tui_refresh_interval_seconds=2.0,
            git_create_enabled=False,
            git_remote_profiles=[],
        )

        with mock.patch("uxon.infra.probes.probe_host") as probe:
            mock_report = mock.MagicMock()
            mock_report.tmux.path = "/usr/bin/tmux"
            mock_report.agents = {"claude": mock.MagicMock(path=None)}
            probe.return_value = mock_report
            with (
                mock.patch.dict("os.environ", {"SSH_CONNECTION": "1.2.3.4 22 5.6.7.8 22"}),
                mock.patch("uxon.infra.config_loader.load_config", return_value=cfg),
                mock.patch.object(uxon_audit, "audit", side_effect=fake_audit),
                mock.patch("sys.stderr", new_callable=io.StringIO),
            ):
                with self.assertRaises(SystemExit):
                    main(["list", "--all-users"])

        list_emits = [e for e in recorded if e[0] == "list.peek"]
        self.assertEqual(len(list_emits), 1)
        self.assertEqual(list_emits[0][0], "list.peek")
        self.assertEqual(list_emits[0][1]["outcome"], "denied")


class DoInteractiveTextualMissingTests(unittest.TestCase):
    """With textual unavailable, ``uxon`` (interactive) must print a single
    install hint on stderr, no traceback, and return 1."""

    def test_prints_install_hint_when_textual_missing(self) -> None:
        # Simulate a stripped install where ``uxon.tui`` (and its
        # textual dep) is unavailable. A ``sys.modules`` sentinel alone
        # is insufficient because the package may already be cached as
        # an attribute on ``uxon``; we also clear that attribute and
        # restore it on teardown.
        import uxon as uxon_pkg

        saved_uxon_tui_module = sys.modules.get("uxon.tui")
        saved_uxon_tui_attr = getattr(uxon_pkg, "tui", None)
        sys.modules["uxon.tui"] = None  # type: ignore[assignment]
        if hasattr(uxon_pkg, "tui"):
            delattr(uxon_pkg, "tui")
        try:
            with tempfile.TemporaryDirectory():
                cfg = config_loader.load_config()
                buf_err = io.StringIO()
                buf_out = io.StringIO()
                with (
                    mock.patch.object(sys, "stderr", buf_err),
                    mock.patch.object(sys, "stdout", buf_out),
                ):
                    rc = do_interactive(cfg, "nobody", "nobody")
                self.assertEqual(rc, 1)
                err_text = buf_err.getvalue()
                self.assertIn("requires", err_text)
                self.assertIn("textual", err_text)
                self.assertNotIn("Traceback", err_text)
        finally:
            if saved_uxon_tui_module is None:
                sys.modules.pop("uxon.tui", None)
            else:
                sys.modules["uxon.tui"] = saved_uxon_tui_module
            if saved_uxon_tui_attr is not None:
                uxon_pkg.tui = saved_uxon_tui_attr  # type: ignore[attr-defined]

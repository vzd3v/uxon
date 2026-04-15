import importlib.util
import io
import sys
import tempfile
import textwrap
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


CCW_PATH = Path(__file__).resolve().parents[1] / "bin" / "ccw"
LOADER = SourceFileLoader("ccw_module", str(CCW_PATH))
SPEC = importlib.util.spec_from_loader("ccw_module", LOADER)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"failed to load spec for {CCW_PATH}")
ccw = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ccw
SPEC.loader.exec_module(ccw)


class CcwTests(unittest.TestCase):
    def make_config(self) -> ccw.Config:
        return ccw.Config(
            runtime_user="",
            default_launch_mode="caller",
            enable_all_users_list=False,
            launch_user_by_caller={},
            session_users=[],
            allowed_roots=["/srv/repos"],
            session_prefix="cc-",
            default_claude_args=[],
            new_project_root="/srv/repos",
            repeat_noninteractive_mode="fail",
            tmux_socket_template="/tmp/ccw-{user}.sock",
        )

    def make_session(
        self,
        name: str,
        path: str,
        *,
        attached: str = "0",
    ) -> ccw.SessionInfo:
        return ccw.SessionInfo(
            user="u-vz",
            name=name,
            attached=attached,
            windows="1",
            created="2026-04-15T06:00:00+00:00",
            last_attached="2026-04-15T06:00:00+00:00",
            pane_pids=(),
            active_pid=1234,
            active_cmd="claude",
            active_path=path,
        )

    def test_resolve_caller_user_prefers_current_non_root_user(self) -> None:
        with mock.patch.object(ccw, "process_user", return_value="u-vz"):
            with mock.patch.dict(ccw.os.environ, {"SUDO_USER": "remdepl"}, clear=False):
                self.assertEqual(ccw.resolve_caller_user(), "u-vz")

    def test_parse_args_supports_version_flags(self) -> None:
        parsed_long = ccw.parse_args(["--version"])
        self.assertEqual(parsed_long.action, "version")

        parsed_short = ccw.parse_args(["-V"])
        self.assertEqual(parsed_short.action, "version")

        parsed_subcommand = ccw.parse_args(["version"])
        self.assertEqual(parsed_subcommand.action, "version")

    def test_parse_args_supports_doctor(self) -> None:
        parsed = ccw.parse_args(["doctor"])
        self.assertEqual(parsed.action, "doctor")

    def test_parse_args_supports_kill_all_force(self) -> None:
        parsed = ccw.parse_args(["kill-all", "--force"])
        self.assertEqual(parsed.action, "kill-all")
        self.assertTrue(parsed.force)

    def test_resolve_launch_user_fixed_mode_uses_runtime_user(self) -> None:
        cfg = ccw.Config(
            runtime_user="devagent",
            default_launch_mode="fixed",
            enable_all_users_list=False,
            launch_user_by_caller={},
            session_users=["devagent"],
            allowed_roots=["/srv"],
            session_prefix="cc-",
            default_claude_args=[],
            new_project_root="/srv/agentdev",
            repeat_noninteractive_mode="fail",
            tmux_socket_template="/tmp/ccw-{user}.sock",
        )

        self.assertEqual(ccw.resolve_launch_user(cfg, "remdepl"), "devagent")

    def test_resolve_launch_user_caller_mode_uses_caller(self) -> None:
        cfg = ccw.Config(
            runtime_user="devagent",
            default_launch_mode="caller",
            enable_all_users_list=False,
            launch_user_by_caller={},
            session_users=["devagent", "remdepl"],
            allowed_roots=["/srv"],
            session_prefix="cc-",
            default_claude_args=[],
            new_project_root="/srv/agentdev",
            repeat_noninteractive_mode="fail",
            tmux_socket_template="/tmp/ccw-{user}.sock",
        )

        self.assertEqual(ccw.resolve_launch_user(cfg, "remdepl"), "remdepl")

    def test_resolve_launch_user_mapping_overrides_default(self) -> None:
        cfg = ccw.Config(
            runtime_user="devagent",
            default_launch_mode="caller",
            enable_all_users_list=True,
            launch_user_by_caller={"remdepl": "devagent"},
            session_users=["devagent", "remdepl"],
            allowed_roots=["/srv"],
            session_prefix="cc-",
            default_claude_args=[],
            new_project_root="/srv/agentdev",
            repeat_noninteractive_mode="fail",
            tmux_socket_template="/tmp/ccw-{user}.sock",
        )

        self.assertEqual(ccw.resolve_launch_user(cfg, "remdepl"), "devagent")

    def test_resolve_all_session_users_keeps_current_user_present(self) -> None:
        cfg = ccw.Config(
            runtime_user="devagent",
            default_launch_mode="fixed",
            enable_all_users_list=True,
            launch_user_by_caller={},
            session_users=["devagent"],
            allowed_roots=["/srv"],
            session_prefix="cc-",
            default_claude_args=[],
            new_project_root="/srv/agentdev",
            repeat_noninteractive_mode="fail",
            tmux_socket_template="/tmp/ccw-{user}.sock",
        )

        self.assertEqual(ccw.resolve_all_session_users(cfg, "remdepl"), ["devagent", "remdepl"])

    def test_parse_args_supports_all_users_listing(self) -> None:
        parsed = ccw.parse_args(["list", "--all-users"])
        self.assertEqual(parsed.action, "list")
        self.assertTrue(parsed.all_users)

        parsed_short = ccw.parse_args(["-l", "--all-users"])
        self.assertEqual(parsed_short.action, "list")
        self.assertTrue(parsed_short.all_users)

    def test_parse_args_supports_repeat_mode_flags_for_new(self) -> None:
        parsed_attach = ccw.parse_args(["-n", "demo", "--attach-existing"])
        self.assertEqual(parsed_attach.action, "new")
        self.assertEqual(parsed_attach.repeat_mode, "attach")

        parsed_new = ccw.parse_args(["new", "demo", "--new-session"])
        self.assertEqual(parsed_new.action, "new")
        self.assertEqual(parsed_new.repeat_mode, "new")

    def test_load_config_reads_new_multi_user_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            cwd = tmp_path / "workspace"
            cwd.mkdir()
            repo_cfg = tmp_path / "repo-config.toml"
            repo_cfg.write_text(
                textwrap.dedent(
                    """
                    runtime_user = "devagent"
                    default_launch_mode = "caller"
                    enable_all_users_list = true
                    session_users = ["devagent", "remdepl"]
                    allowed_roots = ["/srv", "/tmp"]
                    session_prefix = "cc-"
                    default_claude_args = ["--model", "sonnet"]
                    repeat_noninteractive_mode = "attach"
                    tmux_socket_template = "/tmp/ccw-{user}-{uid}.sock"

                    [launch_user_by_caller]
                    remdepl = "devagent"
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            def fake_load_toml(path: Path) -> dict[str, object]:
                if path == tmp_path / "config" / "config.toml":
                    with repo_cfg.open("rb") as fh:
                        return ccw.tomllib.load(fh)
                return {}

            with mock.patch.object(ccw, "repo_root", return_value=tmp_path):
                with mock.patch.object(ccw, "find_project_config", return_value=None):
                    with mock.patch.object(ccw, "canonical", side_effect=lambda value: str(value)):
                        with mock.patch.object(ccw, "load_toml", side_effect=fake_load_toml):
                            cfg = ccw.load_config(str(cwd))

        self.assertEqual(cfg.runtime_user, "devagent")
        self.assertEqual(cfg.default_launch_mode, "caller")
        self.assertTrue(cfg.enable_all_users_list)
        self.assertEqual(cfg.session_users, ["devagent", "remdepl"])
        self.assertEqual(cfg.launch_user_by_caller, {"remdepl": "devagent"})
        self.assertEqual(cfg.default_claude_args, ["--model", "sonnet"])
        self.assertEqual(cfg.repeat_noninteractive_mode, "attach")
        self.assertEqual(cfg.tmux_socket_template, "/tmp/ccw-{user}-{uid}.sock")

    def test_format_version_reads_version_file_and_commit(self) -> None:
        with mock.patch.object(ccw, "read_repo_version", return_value="0.2.0"):
            with mock.patch.object(ccw, "read_git_commit_short", return_value="abc1234"):
                with mock.patch.object(ccw, "repo_is_dirty", return_value=False):
                    self.assertEqual(ccw.format_version(), "ccw 0.2.0 (abc1234)")

    def test_format_version_marks_dirty_checkout(self) -> None:
        with mock.patch.object(ccw, "read_repo_version", return_value="0.2.0"):
            with mock.patch.object(ccw, "read_git_commit_short", return_value="abc1234"):
                with mock.patch.object(ccw, "repo_is_dirty", return_value=True):
                    self.assertEqual(ccw.format_version(), "ccw 0.2.0 (abc1234-dirty)")

    def test_do_new_allows_call_from_outside_allowed_roots(self) -> None:
        cfg = self.make_config()
        args = ccw.ParsedArgs(action="new", target_id="demo", dry_run=True, claude_args=[])

        with mock.patch.object(ccw.os, "getcwd", return_value="/home/u-vz"):
            with mock.patch.object(ccw, "canonical", side_effect=lambda value: str(value)):
                with mock.patch.object(ccw, "collect_sessions", return_value=[]):
                    with mock.patch.object(ccw, "allocate_session_name", return_value="cc-demo"):
                        with mock.patch.object(ccw, "launch_in_tmux", return_value=0) as launch:
                            result = ccw.do_new(args, cfg, "u-vz")

        self.assertEqual(result, 0)
        launch.assert_called_once()

    def test_do_new_existing_session_defaults_to_attach_in_tty(self) -> None:
        cfg = self.make_config()
        args = ccw.ParsedArgs(action="new", target_id="demo", claude_args=[])
        existing = [self.make_session("cc-demo", "/srv/repos/demo")]

        with mock.patch.object(ccw, "canonical", side_effect=lambda value: str(value)):
            with mock.patch.object(ccw, "run_cmd") as run_cmd:
                with mock.patch.object(ccw, "collect_sessions", return_value=existing):
                    with mock.patch.object(ccw, "is_interactive_tty", return_value=True):
                        with mock.patch("builtins.input", return_value=""):
                            with mock.patch.object(ccw, "attach_session", return_value=0) as attach:
                                with mock.patch.object(ccw, "launch_in_tmux", return_value=0) as launch:
                                    result = ccw.do_new(args, cfg, "u-vz")

        self.assertEqual(result, 0)
        run_cmd.assert_called_once()
        attach.assert_called_once()
        launch.assert_not_called()

    def test_do_new_existing_session_force_new_bypasses_prompt(self) -> None:
        cfg = self.make_config()
        args = ccw.ParsedArgs(action="new", target_id="demo", repeat_mode="new", claude_args=[])
        existing = [self.make_session("cc-demo", "/srv/repos/demo")]

        with mock.patch.object(ccw, "canonical", side_effect=lambda value: str(value)):
            with mock.patch.object(ccw, "run_cmd") as run_cmd:
                with mock.patch.object(ccw, "collect_sessions", return_value=existing):
                    with mock.patch.object(ccw, "allocate_session_name", return_value="cc-demo-2") as allocate:
                        with mock.patch.object(ccw, "launch_in_tmux", return_value=0) as launch:
                            result = ccw.do_new(args, cfg, "u-vz")

        self.assertEqual(result, 0)
        run_cmd.assert_called_once()
        allocate.assert_called_once()
        launch.assert_called_once()

    def test_do_new_existing_session_without_tty_fails_with_guidance(self) -> None:
        cfg = self.make_config()
        args = ccw.ParsedArgs(action="new", target_id="demo", claude_args=[])
        existing = [self.make_session("cc-demo", "/srv/repos/demo")]

        with mock.patch.object(ccw, "canonical", side_effect=lambda value: str(value)):
            with mock.patch.object(ccw, "run_cmd") as run_cmd:
                with mock.patch.object(ccw, "collect_sessions", return_value=existing):
                    with mock.patch.object(ccw, "is_interactive_tty", return_value=False):
                        with mock.patch.object(ccw, "eprint") as eprint:
                            with self.assertRaises(SystemExit) as ctx:
                                ccw.do_new(args, cfg, "u-vz")

        self.assertEqual(ctx.exception.code, 2)
        run_cmd.assert_called_once()
        eprint.assert_called()
        self.assertIn("--attach-existing", eprint.call_args[0][0])
        self.assertIn("--new-session", eprint.call_args[0][0])

    def test_do_new_existing_worktree_session_defaults_to_attach_in_tty(self) -> None:
        cfg = self.make_config()
        args = ccw.ParsedArgs(action="new", target_id="demo", worktree_branch="feature-x", claude_args=[])
        existing = [self.make_session("cc-demo-feature-x", "/srv/repos/demo")]

        with mock.patch.object(ccw.os.path, "isdir", return_value=True):
            with mock.patch.object(ccw, "git_repo_root_as_user", return_value="/srv/repos/demo"):
                with mock.patch.object(ccw, "collect_sessions", return_value=existing):
                    with mock.patch.object(ccw, "is_interactive_tty", return_value=True):
                        with mock.patch("builtins.input", return_value=""):
                            with mock.patch.object(ccw, "attach_session", return_value=0) as attach:
                                with mock.patch.object(ccw, "launch_in_tmux", return_value=0) as launch:
                                    result = ccw.do_new(args, cfg, "u-vz")

        self.assertEqual(result, 0)
        attach.assert_called_once()
        launch.assert_not_called()

    def test_do_new_existing_worktree_session_uses_configured_noninteractive_new(self) -> None:
        cfg = self.make_config()
        cfg.repeat_noninteractive_mode = "new"
        args = ccw.ParsedArgs(action="new", target_id="demo", worktree_branch="feature-x", claude_args=[])
        existing = [self.make_session("cc-demo-feature-x", "/srv/repos/demo")]

        with mock.patch.object(ccw.os.path, "isdir", return_value=True):
            with mock.patch.object(ccw, "git_repo_root_as_user", return_value="/srv/repos/demo"):
                with mock.patch.object(ccw, "collect_sessions", return_value=existing):
                    with mock.patch.object(ccw, "is_interactive_tty", return_value=False):
                        with mock.patch.object(ccw, "allocate_session_name", return_value="cc-demo-feature-x-2") as allocate:
                            with mock.patch.object(ccw, "launch_in_tmux", return_value=0) as launch:
                                result = ccw.do_new(args, cfg, "u-vz")

        self.assertEqual(result, 0)
        allocate.assert_called_once()
        launch.assert_called_once()

    def test_do_new_legacy_socket_guardrail_fails(self) -> None:
        cfg = self.make_config()
        args = ccw.ParsedArgs(action="new", target_id="demo", claude_args=[])
        legacy = [self.make_session("cc-demo", "/srv/repos/demo")]

        with mock.patch.object(ccw, "canonical", side_effect=lambda value: str(value)):
            with mock.patch.object(ccw, "run_cmd"):
                with mock.patch.object(ccw, "collect_sessions", return_value=[]):
                    with mock.patch.object(ccw, "collect_sessions_for_user", return_value=legacy):
                        with mock.patch.object(ccw, "tmux_socket_path", return_value="/tmp/ccw-u-vz.sock"):
                            with mock.patch.object(ccw, "eprint") as eprint:
                                with self.assertRaises(SystemExit) as ctx:
                                    ccw.do_new(args, cfg, "u-vz")

        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("legacy default tmux socket", eprint.call_args[0][0])

    def test_resolve_repeat_decision_prefers_env_override(self) -> None:
        cfg = self.make_config()
        cfg.repeat_noninteractive_mode = "fail"
        session = self.make_session("cc-demo", "/srv/repos/demo")

        with mock.patch.object(ccw, "is_interactive_tty", return_value=False):
            with mock.patch.dict(ccw.os.environ, {"CCW_REPEAT_NONINTERACTIVE_POLICY": "attach"}, clear=False):
                decision = ccw.resolve_repeat_decision("none" if False else None, cfg, "/srv/repos/demo", session, [session])

        self.assertEqual(decision, "attach")

    def test_tmux_socket_path_expands_template(self) -> None:
        cfg = self.make_config()
        cfg.tmux_socket_template = "/tmp/ccw-{user}-{uid}.sock"

        with mock.patch.object(ccw.pwd, "getpwnam") as getpwnam:
            getpwnam.return_value = mock.Mock(pw_uid=1001)
            path = ccw.tmux_socket_path(cfg, "u-vz")

        self.assertEqual(path, "/tmp/ccw-u-vz-1001.sock")

    def test_doctor_reports_socket_and_config(self) -> None:
        cfg = self.make_config()
        output = io.StringIO()

        with mock.patch.object(ccw, "resolve_config_layers", return_value=({}, [Path("/srv/apps/vz_devagent_cli_tool/config/config.toml")])):
            with mock.patch.object(ccw, "tmux_socket_path", return_value="/tmp/ccw-u-vz.sock"):
                with mock.patch.object(ccw, "command_path_for_user", side_effect=["/usr/bin/tmux", "/usr/local/bin/claude"]):
                    with mock.patch.object(ccw, "collect_sessions", return_value=[self.make_session("cc-demo", "/srv/repos/demo")]):
                        with mock.patch.object(ccw, "collect_sessions_for_user", return_value=[]):
                            with mock.patch.object(ccw, "user_can_write_dir", return_value=True):
                                with mock.patch.object(ccw, "format_version", return_value="ccw 0.4.0 (abc1234)"):
                                    with mock.patch("sys.stdout", output):
                                        rc = ccw.do_doctor(cfg, "remdepl", "u-vz", "/srv/repos/demo")

        self.assertEqual(rc, 0)
        rendered = output.getvalue()
        self.assertIn("ccw doctor", rendered)
        self.assertIn("config_paths=/srv/apps/vz_devagent_cli_tool/config/config.toml", rendered)
        self.assertIn("tmux_socket=/tmp/ccw-u-vz.sock", rendered)
        self.assertIn("claude_path=/usr/local/bin/claude", rendered)

    def test_do_kill_all_requires_force_without_tty(self) -> None:
        cfg = self.make_config()
        args = ccw.ParsedArgs(action="kill-all", force=False)
        sessions = [self.make_session("cc-demo", "/srv/repos/demo")]

        with mock.patch.object(ccw, "collect_sessions", return_value=sessions):
            with mock.patch.object(ccw, "is_interactive_tty", return_value=False):
                with mock.patch.object(ccw, "eprint") as eprint:
                    with self.assertRaises(SystemExit) as ctx:
                        ccw.do_kill_all(args, cfg, "u-vz")

        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("--force", eprint.call_args[0][0])

    def test_find_project_config_ignores_permission_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            allowed = [str(root)]
            target = root / "a" / "b"
            target.mkdir(parents=True)

            def fake_exists(self: Path) -> bool:
                if self == root / "a" / ".ccw.toml":
                    raise PermissionError("denied")
                return False

            with mock.patch.object(Path, "exists", fake_exists):
                self.assertIsNone(ccw.find_project_config(str(target), allowed))


if __name__ == "__main__":
    unittest.main()

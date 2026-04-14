import importlib.util
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
        )

        self.assertEqual(ccw.resolve_all_session_users(cfg, "remdepl"), ["devagent", "remdepl"])

    def test_parse_args_supports_all_users_listing(self) -> None:
        parsed = ccw.parse_args(["list", "--all-users"])
        self.assertEqual(parsed.action, "list")
        self.assertTrue(parsed.all_users)

        parsed_short = ccw.parse_args(["-l", "--all-users"])
        self.assertEqual(parsed_short.action, "list")
        self.assertTrue(parsed_short.all_users)

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
        cfg = ccw.Config(
            runtime_user="",
            default_launch_mode="caller",
            enable_all_users_list=False,
            launch_user_by_caller={},
            session_users=[],
            allowed_roots=["/srv/repos"],
            session_prefix="cc-",
            default_claude_args=[],
            new_project_root="/srv/repos",
        )
        args = ccw.ParsedArgs(action="new", target_id="demo", dry_run=True, claude_args=[])

        with mock.patch.object(ccw.os, "getcwd", return_value="/home/u-vz"):
            with mock.patch.object(ccw, "canonical", side_effect=lambda value: str(value)):
                with mock.patch.object(ccw, "collect_sessions", return_value=[]):
                    with mock.patch.object(ccw, "allocate_session_name", return_value="cc-demo"):
                        with mock.patch.object(ccw, "launch_in_tmux", return_value=0) as launch:
                            result = ccw.do_new(args, cfg, "u-vz")

        self.assertEqual(result, 0)
        launch.assert_called_once()

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

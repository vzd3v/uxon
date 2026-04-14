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
            system_cfg = tmp_path / "system.toml"
            system_cfg.write_text(
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
                if str(path) == "/etc/ccw/config.toml":
                    with system_cfg.open("rb") as fh:
                        return ccw.tomllib.load(fh)
                return {}

            with mock.patch.object(ccw.Path, "home", return_value=tmp_path):
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


if __name__ == "__main__":
    unittest.main()

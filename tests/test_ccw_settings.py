import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import ccw_settings as cs


DEFAULTS = {
    "runtime_user": "",
    "default_launch_mode": "caller",
    "enable_all_users_list": False,
    "launch_user_by_caller": {},
    "session_users": [],
    "allowed_roots": ["/srv/repos"],
    "session_prefix": "cc-",
    "default_claude_args": [],
    "new_project_root": "/srv/repos",
    "repeat_noninteractive_mode": "fail",
    "tmux_socket_template": "/tmp/ccw-{user}.sock",
}


class ResolveSettingEntriesTests(unittest.TestCase):
    def test_default_when_unset(self) -> None:
        entries = cs.resolve_setting_entries({}, {}, None, DEFAULTS)
        by_key = {e.spec.key: e for e in entries}
        self.assertEqual(by_key["runtime_user"].source, "default")
        self.assertEqual(by_key["runtime_user"].value, "")
        self.assertTrue(by_key["runtime_user"].editable)

    def test_repo_override(self) -> None:
        entries = cs.resolve_setting_entries({"runtime_user": "devagent"}, {}, None, DEFAULTS)
        by_key = {e.spec.key: e for e in entries}
        self.assertEqual(by_key["runtime_user"].source, "repo")
        self.assertEqual(by_key["runtime_user"].value, "devagent")
        self.assertTrue(by_key["runtime_user"].editable)

    def test_project_override_is_readonly(self) -> None:
        entries = cs.resolve_setting_entries(
            {"runtime_user": "repoish"},
            {"runtime_user": "projish"},
            Path("/p/.ccw.toml"),
            DEFAULTS,
        )
        by_key = {e.spec.key: e for e in entries}
        self.assertEqual(by_key["runtime_user"].source, "project:/p/.ccw.toml")
        self.assertEqual(by_key["runtime_user"].value, "projish")
        self.assertFalse(by_key["runtime_user"].editable)


class RenderRepoConfigTomlTests(unittest.TestCase):
    def test_round_trip_simple(self) -> None:
        data = {
            "runtime_user": "devagent",
            "default_launch_mode": "fixed",
            "enable_all_users_list": True,
            "session_users": ["devagent", "remdepl"],
            "allowed_roots": ["/srv"],
            "session_prefix": "cc-",
            "default_claude_args": [],
            "new_project_root": "/srv/agentdev",
            "repeat_noninteractive_mode": "fail",
            "tmux_socket_template": "/tmp/ccw-{user}.sock",
        }
        content = cs.render_repo_config_toml(data)
        parsed = tomllib.loads(content)
        # Scalars round-trip
        for key in ("runtime_user", "default_launch_mode", "session_prefix",
                    "new_project_root", "repeat_noninteractive_mode", "tmux_socket_template"):
            self.assertEqual(parsed[key], data[key])
        self.assertTrue(parsed["enable_all_users_list"])
        self.assertEqual(parsed["session_users"], ["devagent", "remdepl"])
        self.assertEqual(parsed["launch_user_by_caller"], {})

    def test_table_with_entries(self) -> None:
        data = {
            "runtime_user": "a",
            "launch_user_by_caller": {"caller1": "devagent", "caller2": "remdepl"},
        }
        content = cs.render_repo_config_toml(data)
        parsed = tomllib.loads(content)
        self.assertEqual(parsed["launch_user_by_caller"], {"caller1": "devagent", "caller2": "remdepl"})

    def test_escapes_quotes_in_strings(self) -> None:
        data = {"runtime_user": 'quote"here'}
        content = cs.render_repo_config_toml(data)
        parsed = tomllib.loads(content)
        self.assertEqual(parsed["runtime_user"], 'quote"here')

    def test_always_emits_launch_user_by_caller_header(self) -> None:
        content = cs.render_repo_config_toml({"runtime_user": "x"})
        self.assertIn("[launch_user_by_caller]", content)


class MutatorTests(unittest.TestCase):
    def test_apply_setting_is_nondestructive(self) -> None:
        orig = {"runtime_user": "a"}
        new = cs.apply_setting(orig, "runtime_user", "b")
        self.assertEqual(orig, {"runtime_user": "a"})
        self.assertEqual(new["runtime_user"], "b")

    def test_apply_setting_rejects_unknown_key(self) -> None:
        with self.assertRaises(KeyError):
            cs.apply_setting({}, "nonsense_key", 1)

    def test_remove_setting_drops_key(self) -> None:
        new = cs.remove_setting({"runtime_user": "x"}, "runtime_user")
        self.assertNotIn("runtime_user", new)

    def test_replace_mapping_requires_table_kind(self) -> None:
        with self.assertRaises(KeyError):
            cs.replace_mapping({}, "runtime_user", {"a": "b"})

    def test_replace_mapping_rejects_non_string_values(self) -> None:
        with self.assertRaises(ValueError):
            cs.replace_mapping({}, "launch_user_by_caller", {"a": 1})


class WriteRepoConfigTomlTests(unittest.TestCase):
    def test_direct_write_when_writable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            cs.write_repo_config_toml("runtime_user = \"x\"\n", path)
            self.assertEqual(path.read_text(encoding="utf-8"), "runtime_user = \"x\"\n")

    def test_falls_back_to_sudo_on_permission_error(self) -> None:
        target = Path("/tmp/ccw_test_dummy_config.toml")

        # First call (tmp.replace) raises PermissionError; sudo path writes.
        real_run = mock.Mock(return_value=mock.Mock(returncode=0, stderr=b""))
        with mock.patch.object(cs, "subprocess", run=real_run):
            with mock.patch.object(Path, "replace", side_effect=PermissionError):
                with mock.patch.object(Path, "write_text"):
                    cs.write_repo_config_toml("data", target)
        self.assertEqual(real_run.call_count, 1)
        cmd = real_run.call_args[0][0]
        self.assertEqual(cmd[:2], ["sudo", "sh"])


if __name__ == "__main__":
    unittest.main()

import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from uxon import settings as cs

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
            Path("/p/.uxon.toml"),
            DEFAULTS,
        )
        by_key = {e.spec.key: e for e in entries}
        self.assertEqual(by_key["runtime_user"].source, "project:/p/.uxon.toml")
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
        for key in (
            "runtime_user",
            "default_launch_mode",
            "session_prefix",
            "new_project_root",
            "repeat_noninteractive_mode",
            "tmux_socket_template",
        ):
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
        self.assertEqual(
            parsed["launch_user_by_caller"], {"caller1": "devagent", "caller2": "remdepl"}
        )

    def test_escapes_quotes_in_strings(self) -> None:
        data = {"runtime_user": 'quote"here'}
        content = cs.render_repo_config_toml(data)
        parsed = tomllib.loads(content)
        self.assertEqual(parsed["runtime_user"], 'quote"here')

    def test_formats_float_values(self) -> None:
        self.assertEqual(cs._format_value(2.5), "2.5")

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


class UpdateRepoConfigTextTests(unittest.TestCase):
    def test_preserves_comments_and_unrelated_keys(self) -> None:
        original = (
            "# top comment\n"
            'runtime_user = "devagent"  # inline comment\n'
            "\n"
            "# section about session_prefix\n"
            'session_prefix = "cc-"\n'
            "\n"
            "[launch_user_by_caller]\n"
            "# who launches what\n"
            'alice = "devagent"\n'
        )
        new = cs.update_repo_config_text(original, {"runtime_user": "remdepl"})
        self.assertIn("# top comment", new)
        self.assertIn("# inline comment", new)
        self.assertIn("# section about session_prefix", new)
        self.assertIn("# who launches what", new)
        self.assertIn('runtime_user = "remdepl"', new)
        # Untouched keys round-trip.
        parsed = tomllib.loads(new)
        self.assertEqual(parsed["runtime_user"], "remdepl")
        self.assertEqual(parsed["session_prefix"], "cc-")
        self.assertEqual(parsed["launch_user_by_caller"], {"alice": "devagent"})

    def test_updates_table_preserving_header_comment(self) -> None:
        original = (
            'runtime_user = "a"\n'
            "\n"
            "# per-caller overrides live here\n"
            "[launch_user_by_caller]\n"
            'alice = "devagent"\n'
        )
        new = cs.update_repo_config_text(original, {"launch_user_by_caller": {"bob": "remdepl"}})
        self.assertIn("# per-caller overrides live here", new)
        parsed = tomllib.loads(new)
        self.assertEqual(parsed["launch_user_by_caller"], {"bob": "remdepl"})

    def test_unknown_key_raises(self) -> None:
        with self.assertRaises(KeyError):
            cs.update_repo_config_text("", {"nonsense": 1})

    def test_table_requires_mapping(self) -> None:
        with self.assertRaises(ValueError):
            cs.update_repo_config_text("", {"launch_user_by_caller": "notadict"})

    def test_fresh_file_emits_only_requested_keys(self) -> None:
        new = cs.update_repo_config_text("", {"runtime_user": "x"})
        parsed = tomllib.loads(new)
        self.assertEqual(parsed, {"runtime_user": "x"})


class PersistRepoConfigUpdatesTests(unittest.TestCase):
    def test_round_trip_on_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                '# hello\nruntime_user = "a"\nsession_prefix = "cc-"\n',
                encoding="utf-8",
            )
            cs.persist_repo_config_updates(path, {"runtime_user": "b"})
            text = path.read_text(encoding="utf-8")
            self.assertIn("# hello", text)
            parsed = tomllib.loads(text)
            self.assertEqual(parsed["runtime_user"], "b")
            self.assertEqual(parsed["session_prefix"], "cc-")

    def test_fresh_file_creates_minimal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            cs.persist_repo_config_updates(path, {"runtime_user": "z"})
            parsed = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(parsed, {"runtime_user": "z"})


class RemoveRepoKeyTests(unittest.TestCase):
    def test_removes_key_preserving_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                '# keep me\nruntime_user = "a"\nsession_prefix = "cc-"\n',
                encoding="utf-8",
            )
            cs.remove_repo_key(path, "runtime_user")
            text = path.read_text(encoding="utf-8")
            self.assertIn("# keep me", text)
            parsed = tomllib.loads(text)
            self.assertNotIn("runtime_user", parsed)
            self.assertEqual(parsed["session_prefix"], "cc-")

    def test_missing_file_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cs.remove_repo_key(Path(tmp) / "nope.toml", "runtime_user")


class WriteRepoConfigTomlTests(unittest.TestCase):
    def test_direct_write_when_writable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            cs.write_repo_config_toml('runtime_user = "x"\n', path)
            self.assertEqual(path.read_text(encoding="utf-8"), 'runtime_user = "x"\n')

    def test_falls_back_to_sudo_tee_on_permission_error(self) -> None:
        target = Path("/tmp/ccw_test_dummy_config.toml")

        # First call (tmp.replace) raises PermissionError; sudo path writes.
        real_run = mock.Mock(return_value=mock.Mock(returncode=0, stderr=b""))
        with mock.patch.object(cs, "subprocess", run=real_run):
            with mock.patch.object(Path, "replace", side_effect=PermissionError):
                with mock.patch.object(Path, "write_text"):
                    cs.write_repo_config_toml("data", target)
        self.assertEqual(real_run.call_count, 1)
        cmd = real_run.call_args[0][0]
        # No shell interpolation: plain sudo tee with destination as a
        # separate argv element.
        self.assertEqual(cmd[:3], ["sudo", "tee", "--"])
        self.assertEqual(cmd[3], str(target))
        # Content goes via stdin, not the command line.
        self.assertEqual(real_run.call_args.kwargs["input"], b"data")


class NestedAgentKeysTests(unittest.TestCase):
    """Round-trip tests for dotted agents.* keys."""

    def _src(self) -> str:
        return (
            "# top comment\n"
            "[agents]\n"
            'enabled = ["claude"]\n'
            'default = "claude"\n'
            "\n"
            "[agents.claude]\n"
            "default_args = []\n"
        )

    def test_round_trip_nested_agent_keys(self) -> None:
        src = self._src()
        new = cs.update_repo_config_text(src, {"agents.claude.default_args": ["--verbose"]})
        parsed = tomllib.loads(new)
        self.assertEqual(parsed["agents"]["claude"]["default_args"], ["--verbose"])
        # Comment survived
        self.assertIn("# top comment", new)
        # Other keys survived
        self.assertEqual(parsed["agents"]["enabled"], ["claude"])
        self.assertEqual(parsed["agents"]["default"], "claude")

    def test_round_trip_agents_default(self) -> None:
        src = self._src()
        new = cs.update_repo_config_text(src, {"agents.default": "codex"})
        parsed = tomllib.loads(new)
        self.assertEqual(parsed["agents"]["default"], "codex")
        self.assertIn("# top comment", new)

    def test_round_trip_agents_enabled_list(self) -> None:
        """List-of-strings under [agents] table writes via persist_repo_config_updates.

        The detected-agents banner relies on this round-trip to add a newly
        discovered agent to ``[agents].enabled`` in repo config. Cover the
        list-write path explicitly since none of the existing TUI write
        call-sites round-trip a list-of-strings under a dotted-key table.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(self._src(), encoding="utf-8")
            cs.persist_repo_config_updates(
                path,
                {"agents.enabled": ["claude", "codex"]},
            )
            text = path.read_text(encoding="utf-8")
            parsed = tomllib.loads(text)
            self.assertEqual(parsed["agents"]["enabled"], ["claude", "codex"])
            # Sibling keys + leading comment survive.
            self.assertEqual(parsed["agents"]["default"], "claude")
            self.assertIn("# top comment", text)
            self.assertEqual(parsed["agents"]["claude"]["default_args"], [])

    def test_resolve_entries_dotted_key_from_repo(self) -> None:
        repo_data = {"agents": {"enabled": ["claude", "cursor"], "default": "cursor"}}
        entries = cs.resolve_setting_entries(repo_data, {}, None, {})
        by_key = {e.spec.key: e for e in entries}
        self.assertEqual(by_key["agents.enabled"].value, ["claude", "cursor"])
        self.assertEqual(by_key["agents.enabled"].source, "repo")
        self.assertEqual(by_key["agents.default"].value, "cursor")

    def test_resolve_entries_dotted_key_default_when_absent(self) -> None:
        entries = cs.resolve_setting_entries({}, {}, None, {})
        by_key = {e.spec.key: e for e in entries}
        self.assertEqual(by_key["agents.enabled"].source, "default")
        self.assertIsNone(by_key["agents.enabled"].value)


class WorktreeSettingsSpecTests(unittest.TestCase):
    def test_worktree_specs_present(self) -> None:
        from uxon.settings import SETTINGS_SPECS

        by_key = {s.key: s for s in SETTINGS_SPECS}
        self.assertIn("worktree_root", by_key)
        self.assertEqual(by_key["worktree_root"].kind, "string")
        self.assertIn("worktree_base", by_key)
        self.assertEqual(by_key["worktree_base"].kind, "enum")
        self.assertEqual(by_key["worktree_base"].choices, ("local", "remote"))

    def test_worktree_base_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            cs.persist_repo_config_updates(path, {"worktree_base": "remote"})
            cs.persist_repo_config_updates(path, {"worktree_root": "/data/wt"})
            text = path.read_text()
        self.assertIn('worktree_base = "remote"', text)
        self.assertIn('worktree_root = "/data/wt"', text)


if __name__ == "__main__":
    unittest.main()

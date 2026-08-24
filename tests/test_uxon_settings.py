import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from uxon.infra import settings as cs
from uxon.infra import settings_toml as ct

DEFAULTS = {
    "default_launch_user": "",
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
        with mock.patch("os.geteuid", return_value=0):
            entries = cs.resolve_setting_entries({}, DEFAULTS)
        by_key = {e.spec.key: e for e in entries}
        self.assertEqual(by_key["default_launch_user"].source, "default")
        self.assertEqual(by_key["default_launch_user"].value, "")
        self.assertTrue(by_key["default_launch_user"].editable)

    def test_operator_override(self) -> None:
        with mock.patch("os.geteuid", return_value=0):
            entries = cs.resolve_setting_entries({"default_launch_user": "dana_agent"}, DEFAULTS)
        by_key = {e.spec.key: e for e in entries}
        self.assertEqual(by_key["default_launch_user"].source, "operator")
        self.assertEqual(by_key["default_launch_user"].value, "dana_agent")
        self.assertTrue(by_key["default_launch_user"].editable)

    def test_only_operator_data_is_resolved(self) -> None:
        with mock.patch("os.geteuid", return_value=0):
            entries = cs.resolve_setting_entries({"default_launch_user": "operator"}, DEFAULTS)
        by_key = {e.spec.key: e for e in entries}
        self.assertEqual(by_key["default_launch_user"].source, "operator")
        self.assertEqual(by_key["default_launch_user"].value, "operator")
        self.assertTrue(by_key["default_launch_user"].editable)

    def test_nonroot_entries_are_read_only(self) -> None:
        with mock.patch("os.geteuid", return_value=1000):
            entries = cs.resolve_setting_entries({}, DEFAULTS)
        self.assertTrue(entries)
        self.assertTrue(all(not entry.editable for entry in entries))


class RenderOperatorConfigTomlTests(unittest.TestCase):
    def test_round_trip_simple(self) -> None:
        data = {
            "default_launch_user": "dana_agent",
            "default_launch_mode": "fixed",
            "enable_all_users_list": True,
            "session_users": ["dana_agent", "erin"],
            "allowed_roots": ["/srv"],
            "session_prefix": "cc-",
            "default_claude_args": [],
            "new_project_root": "/srv/agentdev",
            "repeat_noninteractive_mode": "fail",
            "tmux_socket_template": "/tmp/ccw-{user}.sock",
        }
        content = ct.render_operator_config_toml(
            data, schema_keys=cs.SCHEMA_KEYS, table_keys=cs.TABLE_KEYS
        )
        parsed = tomllib.loads(content)
        # Scalars round-trip
        for key in (
            "default_launch_user",
            "default_launch_mode",
            "session_prefix",
            "new_project_root",
            "repeat_noninteractive_mode",
            "tmux_socket_template",
        ):
            self.assertEqual(parsed[key], data[key])
        self.assertTrue(parsed["enable_all_users_list"])
        self.assertEqual(parsed["session_users"], ["dana_agent", "erin"])
        self.assertEqual(parsed["launch_user_by_caller"], {})

    def test_table_with_entries(self) -> None:
        data = {
            "default_launch_user": "a",
            "launch_user_by_caller": {"caller1": "dana_agent", "caller2": "erin"},
        }
        content = ct.render_operator_config_toml(
            data, schema_keys=cs.SCHEMA_KEYS, table_keys=cs.TABLE_KEYS
        )
        parsed = tomllib.loads(content)
        self.assertEqual(
            parsed["launch_user_by_caller"], {"caller1": "dana_agent", "caller2": "erin"}
        )

    def test_escapes_quotes_in_strings(self) -> None:
        data = {"default_launch_user": 'quote"here'}
        content = ct.render_operator_config_toml(
            data, schema_keys=cs.SCHEMA_KEYS, table_keys=cs.TABLE_KEYS
        )
        parsed = tomllib.loads(content)
        self.assertEqual(parsed["default_launch_user"], 'quote"here')

    def test_formats_float_values(self) -> None:
        self.assertEqual(ct._format_value(2.5), "2.5")

    def test_always_emits_launch_user_by_caller_header(self) -> None:
        content = ct.render_operator_config_toml(
            {"default_launch_user": "x"}, schema_keys=cs.SCHEMA_KEYS, table_keys=cs.TABLE_KEYS
        )
        self.assertIn("[launch_user_by_caller]", content)


class MutatorTests(unittest.TestCase):
    def test_apply_setting_is_nondestructive(self) -> None:
        orig = {"default_launch_user": "a"}
        new = cs.apply_setting(orig, "default_launch_user", "b")
        self.assertEqual(orig, {"default_launch_user": "a"})
        self.assertEqual(new["default_launch_user"], "b")

    def test_apply_setting_rejects_unknown_key(self) -> None:
        with self.assertRaises(KeyError):
            cs.apply_setting({}, "nonsense_key", 1)

    def test_remove_setting_drops_key(self) -> None:
        new = cs.remove_setting({"default_launch_user": "x"}, "default_launch_user")
        self.assertNotIn("default_launch_user", new)

    def test_replace_mapping_requires_table_kind(self) -> None:
        with self.assertRaises(KeyError):
            cs.replace_mapping({}, "default_launch_user", {"a": "b"})

    def test_replace_mapping_rejects_non_string_values(self) -> None:
        with self.assertRaises(ValueError):
            cs.replace_mapping({}, "launch_user_by_caller", {"a": 1})


class UpdateOperatorConfigTextTests(unittest.TestCase):
    def test_preserves_comments_and_unrelated_keys(self) -> None:
        original = (
            "# top comment\n"
            'default_launch_user = "dana_agent"  # inline comment\n'
            "\n"
            "# section about session_prefix\n"
            'session_prefix = "cc-"\n'
            "\n"
            "[launch_user_by_caller]\n"
            "# who launches what\n"
            'alice = "dana_agent"\n'
        )
        new = ct.update_operator_config_text(
            original,
            {"default_launch_user": "erin"},
            schema_keys=cs.SCHEMA_KEYS,
            table_keys=cs.TABLE_KEYS,
        )
        self.assertIn("# top comment", new)
        self.assertIn("# inline comment", new)
        self.assertIn("# section about session_prefix", new)
        self.assertIn("# who launches what", new)
        self.assertIn('default_launch_user = "erin"', new)
        # Untouched keys round-trip.
        parsed = tomllib.loads(new)
        self.assertEqual(parsed["default_launch_user"], "erin")
        self.assertEqual(parsed["session_prefix"], "cc-")
        self.assertEqual(parsed["launch_user_by_caller"], {"alice": "dana_agent"})

    def test_updates_table_preserving_header_comment(self) -> None:
        original = (
            'default_launch_user = "a"\n'
            "\n"
            "# per-caller overrides live here\n"
            "[launch_user_by_caller]\n"
            'alice = "dana_agent"\n'
        )
        new = ct.update_operator_config_text(
            original,
            {"launch_user_by_caller": {"bob": "erin"}},
            schema_keys=cs.SCHEMA_KEYS,
            table_keys=cs.TABLE_KEYS,
        )
        self.assertIn("# per-caller overrides live here", new)
        parsed = tomllib.loads(new)
        self.assertEqual(parsed["launch_user_by_caller"], {"bob": "erin"})

    def test_unknown_key_raises(self) -> None:
        with self.assertRaises(KeyError):
            ct.update_operator_config_text(
                "", {"nonsense": 1}, schema_keys=cs.SCHEMA_KEYS, table_keys=cs.TABLE_KEYS
            )

    def test_table_requires_mapping(self) -> None:
        with self.assertRaises(ValueError):
            ct.update_operator_config_text(
                "",
                {"launch_user_by_caller": "notadict"},
                schema_keys=cs.SCHEMA_KEYS,
                table_keys=cs.TABLE_KEYS,
            )

    def test_fresh_file_emits_only_requested_keys(self) -> None:
        new = ct.update_operator_config_text(
            "", {"default_launch_user": "x"}, schema_keys=cs.SCHEMA_KEYS, table_keys=cs.TABLE_KEYS
        )
        parsed = tomllib.loads(new)
        self.assertEqual(parsed, {"default_launch_user": "x"})


class PersistOperatorConfigUpdatesTests(unittest.TestCase):
    def test_round_trip_on_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                '# hello\ndefault_launch_user = "a"\nsession_prefix = "cc-"\n',
                encoding="utf-8",
            )
            with mock.patch("os.geteuid", return_value=0), mock.patch("os.fchown"):
                cs.persist_operator_config_updates(path, {"default_launch_user": "b"})
            text = path.read_text(encoding="utf-8")
            self.assertIn("# hello", text)
            parsed = tomllib.loads(text)
            self.assertEqual(parsed["default_launch_user"], "b")
            self.assertEqual(parsed["session_prefix"], "cc-")

    def test_fresh_file_creates_minimal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            with mock.patch("os.geteuid", return_value=0), mock.patch("os.fchown"):
                cs.persist_operator_config_updates(path, {"default_launch_user": "z"})
            parsed = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(parsed, {"default_launch_user": "z"})


class RemoveOperatorKeyTests(unittest.TestCase):
    def test_removes_key_preserving_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                '# keep me\ndefault_launch_user = "a"\nsession_prefix = "cc-"\n',
                encoding="utf-8",
            )
            with mock.patch("os.geteuid", return_value=0), mock.patch("os.fchown"):
                cs.remove_operator_key(path, "default_launch_user")
            text = path.read_text(encoding="utf-8")
            self.assertIn("# keep me", text)
            parsed = tomllib.loads(text)
            self.assertNotIn("default_launch_user", parsed)
            self.assertEqual(parsed["session_prefix"], "cc-")

    def test_missing_file_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cs.remove_operator_key(Path(tmp) / "nope.toml", "default_launch_user")


class WriteOperatorConfigTomlTests(unittest.TestCase):
    def test_root_write_is_atomic_and_mode_0644(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            with mock.patch("os.geteuid", return_value=0), mock.patch("os.fchown") as fchown:
                cs.write_operator_config_toml('default_launch_user = "x"\n', path)
            self.assertEqual(path.read_text(encoding="utf-8"), 'default_launch_user = "x"\n')
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            fchown.assert_called_once()

    def test_nonroot_write_is_rejected_without_sudo_helper(self) -> None:
        target = Path("/etc/uxon/config.toml")
        with mock.patch("os.geteuid", return_value=1000):
            with self.assertRaisesRegex(PermissionError, "read-only"):
                cs.write_operator_config_toml("data", target)


class LoadSettingsSourceTests(unittest.TestCase):
    def test_reads_only_canonical_operator_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            operator_cfg = root / "etc" / "uxon" / "config.toml"
            operator_cfg.parent.mkdir(parents=True)
            operator_cfg.write_text('default_launch_user = "operator"\n', encoding="utf-8")
            project = root / "work"
            project.mkdir()
            project_cfg = project / ".uxon.toml"
            project_cfg.write_text('default_launch_user = "project"\n', encoding="utf-8")

            opened: list[Path] = []
            original_open = Path.open

            def spy_open(path_self: Path, *args, **kwargs):
                opened.append(path_self)
                return original_open(path_self, *args, **kwargs)

            with mock.patch("uxon.infra.config_loader.OPERATOR_CONFIG_PATH", operator_cfg):
                with mock.patch.object(Path, "open", spy_open):
                    operator_data = cs.load_settings_source()

        self.assertEqual(operator_data["default_launch_user"], "operator")
        self.assertIn(operator_cfg, opened)
        self.assertNotIn(project_cfg, opened)


class ProfileSettingsSchemaTests(unittest.TestCase):
    def test_launch_and_runtimes_are_file_only(self) -> None:
        self.assertNotIn("agents.enabled", cs.SCHEMA_KEYS)
        self.assertNotIn("agents.default", cs.SCHEMA_KEYS)
        self.assertNotIn("default_git_remote_profile", cs.SCHEMA_KEYS)
        self.assertNotIn("launch.enabled_profiles", cs.SCHEMA_KEYS)
        self.assertNotIn("container.profiles", cs.SCHEMA_KEYS)


class WorktreeSettingsSpecTests(unittest.TestCase):
    def test_worktree_specs_present(self) -> None:
        from uxon.infra.settings import SETTINGS_SPECS

        by_key = {s.key: s for s in SETTINGS_SPECS}
        self.assertIn("worktree_root", by_key)
        self.assertEqual(by_key["worktree_root"].kind, "string")
        self.assertIn("worktree_base", by_key)
        self.assertEqual(by_key["worktree_base"].kind, "enum")
        self.assertEqual(by_key["worktree_base"].choices, ("local", "remote"))

    def test_worktree_base_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            with mock.patch("os.geteuid", return_value=0), mock.patch("os.fchown"):
                cs.persist_operator_config_updates(path, {"worktree_base": "remote"})
                cs.persist_operator_config_updates(path, {"worktree_root": "/data/wt"})
            text = path.read_text()
        self.assertIn('worktree_base = "remote"', text)
        self.assertIn('worktree_root = "/data/wt"', text)


class TmuxManageOptionsSpecTests(unittest.TestCase):
    def test_manage_options_present(self) -> None:
        from uxon.infra.settings import SETTINGS_SPECS

        by_key = {s.key: s for s in SETTINGS_SPECS}
        self.assertIn("tmux.manage_options", by_key)
        spec = by_key["tmux.manage_options"]
        self.assertEqual(spec.kind, "bool")
        # AC8a/D7: description points the operator at config.toml for the lists.
        self.assertIn("config.toml", spec.description)

    def test_manage_options_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            with mock.patch("os.geteuid", return_value=0), mock.patch("os.fchown"):
                cs.persist_operator_config_updates(path, {"tmux.manage_options": True})
            text = path.read_text()
            parsed = tomllib.loads(text)
        self.assertTrue(parsed["tmux"]["manage_options"])


if __name__ == "__main__":
    unittest.main()

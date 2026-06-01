# SPDX-License-Identifier: MIT
"""Regression: ``import uxon.cli`` must stay textual-free.

Non-interactive CLI paths (``uxon version``, ``uxon list --json``, etc.)
must not pull in ``textual``, ``uxon.tui.context``, ``uxon.infra.probes``,
or ``uxon.infra.agents``. The fast paths import only what they need.
"""

from __future__ import annotations

import subprocess
import sys
import unittest


class CliImportSurfaceTests(unittest.TestCase):
    """``import uxon.cli`` does not load textual, tui.context, probes, or agents."""

    def _modules_after_cli_import(self) -> set[str]:
        cp = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys, uxon.cli; print('\\n'.join(sorted(sys.modules)))",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return set(cp.stdout.splitlines())

    def test_cli_does_not_pull_uxon_tui_context(self) -> None:
        modules = self._modules_after_cli_import()
        self.assertNotIn("uxon.tui.context", modules)

    def test_cli_does_not_pull_uxon_tui(self) -> None:
        modules = self._modules_after_cli_import()
        self.assertNotIn("uxon.tui", modules)

    def test_cli_does_not_pull_textual(self) -> None:
        modules = self._modules_after_cli_import()
        textual_mods = {m for m in modules if m == "textual" or m.startswith("textual.")}
        self.assertEqual(set(), textual_mods, f"textual leaked: {textual_mods!r}")

    def test_cli_does_not_pull_uxon_infra_probes(self) -> None:
        modules = self._modules_after_cli_import()
        self.assertNotIn("uxon.infra.probes", modules)

    def test_cli_does_not_pull_uxon_infra_agents(self) -> None:
        modules = self._modules_after_cli_import()
        self.assertNotIn("uxon.infra.agents", modules)

    def _modules_after_dispatch(self, body: str) -> set[str]:
        """Run ``body`` (which exercises a dispatch path) in a fresh
        interpreter after importing ``uxon.cli`` and return the module set."""
        prog = f"import sys, uxon.cli as cli\n{body}\nprint('\\n'.join(sorted(sys.modules)))\n"
        cp = subprocess.run(
            [sys.executable, "-c", prog],
            capture_output=True,
            text=True,
            check=True,
        )
        return set(cp.stdout.splitlines())

    def _assert_no_heavy(self, modules: set[str]) -> None:
        textual_mods = {m for m in modules if m == "textual" or m.startswith("textual.")}
        self.assertEqual(set(), textual_mods, f"textual leaked: {textual_mods!r}")
        self.assertNotIn("uxon.tui", modules)
        self.assertNotIn("uxon.tui.context", modules)
        self.assertNotIn("uxon.infra.probes", modules)
        self.assertNotIn("uxon.infra.agents", modules)

    def test_version_dispatch_stays_lazy(self) -> None:
        # The ``version`` dispatch path: parse + the pure version-string
        # builder composed with the (git/FS) readers. Must not pull
        # textual / tui / probes.
        modules = self._modules_after_dispatch(
            "cli.parse_args(['version'])\nassert cli.format_version().startswith('uxon ')\n"
        )
        self._assert_no_heavy(modules)

    def test_list_dispatch_stays_lazy(self) -> None:
        # The ``list`` dispatch path: parse the subcommand and build the
        # wire-schema records for an empty session set. Must not pull
        # textual / tui / probes.
        modules = self._modules_after_dispatch(
            "args = cli.parse_args(['list'])\n"
            "assert args.action == 'list'\n"
            "from uxon.domain.wire_schema import build_session_records\n"
            "build_session_records([], session_prefix='uxon-')\n"
        )
        self._assert_no_heavy(modules)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

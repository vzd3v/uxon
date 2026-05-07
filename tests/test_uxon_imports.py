# SPDX-License-Identifier: MIT
"""Regression: ``import uxon.cli`` must stay textual-free.

Non-interactive CLI paths (``uxon version``, ``uxon list --json``, etc.)
must not pull in ``textual``, ``uxon.tui.context``, or ``uxon.probes``.
The fast paths import only what they need.
"""

from __future__ import annotations

import subprocess
import sys
import unittest


class CliImportSurfaceTests(unittest.TestCase):
    """``import uxon.cli`` does not load textual, tui.context, or probes."""

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

    def test_cli_does_not_pull_uxon_probes(self) -> None:
        modules = self._modules_after_cli_import()
        self.assertNotIn("uxon.probes", modules)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

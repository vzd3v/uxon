"""Tests for the agent catalog and availability probe."""

from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from uxon import agents as uxon_agents


class CatalogTests(unittest.TestCase):
    def test_catalog_has_three_agents(self) -> None:
        self.assertEqual(set(uxon_agents.CATALOG), {"claude", "codex", "cursor"})

    def test_every_agent_has_yolo_and_normal(self) -> None:
        for agent in uxon_agents.CATALOG.values():
            ids = [m.id for m in agent.permission_modes]
            self.assertIn("normal", ids, agent.id)
            self.assertIn("yolo", ids, agent.id)
            self.assertEqual(ids[0], "normal", agent.id)  # normal first

    def test_cursor_has_no_auto(self) -> None:
        cursor = uxon_agents.CATALOG["cursor"]
        self.assertNotIn("auto", [m.id for m in cursor.permission_modes])

    def test_claude_and_codex_have_auto(self) -> None:
        for aid in ("claude", "codex"):
            ids = [m.id for m in uxon_agents.CATALOG[aid].permission_modes]
            self.assertIn("auto", ids, aid)

    def test_mode_ids_unique_within_agent(self) -> None:
        for agent in uxon_agents.CATALOG.values():
            ids = [m.id for m in agent.permission_modes]
            self.assertEqual(len(ids), len(set(ids)), agent.id)

    def test_session_suffix_matches_id(self) -> None:
        for agent in uxon_agents.CATALOG.values():
            self.assertEqual(agent.session_suffix, f"@{agent.id}")

    def test_yolo_flags(self) -> None:
        self.assertEqual(
            uxon_agents.permission_mode_for(uxon_agents.CATALOG["claude"], "yolo").flags,
            ("--dangerously-skip-permissions",),
        )
        self.assertEqual(
            uxon_agents.permission_mode_for(uxon_agents.CATALOG["codex"], "yolo").flags,
            ("--dangerously-bypass-approvals-and-sandbox",),
        )
        self.assertEqual(
            uxon_agents.permission_mode_for(uxon_agents.CATALOG["cursor"], "yolo").flags,
            ("--yolo",),
        )

    def test_auto_flags(self) -> None:
        self.assertEqual(
            uxon_agents.permission_mode_for(uxon_agents.CATALOG["claude"], "auto").flags,
            ("--permission-mode", "auto"),
        )
        self.assertEqual(
            uxon_agents.permission_mode_for(uxon_agents.CATALOG["codex"], "auto").flags,
            ("--full-auto",),
        )
        self.assertIsNone(uxon_agents.permission_mode_for(uxon_agents.CATALOG["cursor"], "auto"))

    def test_normal_has_no_flags(self) -> None:
        for agent in uxon_agents.CATALOG.values():
            mode = uxon_agents.permission_mode_for(agent, "normal")
            self.assertEqual(mode.flags, ())


class ProbeAgentsTests(unittest.TestCase):
    def test_probe_ok(self) -> None:
        with mock.patch("uxon.agents.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="1.0.1\n", stderr=""
            )
            result = uxon_agents.probe_agents(["claude"], launch_user=None)
        self.assertEqual(result["claude"].status, "ok")
        self.assertEqual(result["claude"].version, "1.0.1")

    def test_probe_missing_filenotfound(self) -> None:
        with mock.patch(
            "uxon.agents.subprocess.run",
            side_effect=FileNotFoundError("no such binary"),
        ):
            result = uxon_agents.probe_agents(["codex"], launch_user=None)
        self.assertEqual(result["codex"].status, "missing")
        self.assertIsNone(result["codex"].version)

    def test_probe_missing_nonzero_exit(self) -> None:
        with mock.patch("uxon.agents.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=127, stdout="", stderr="not found"
            )
            result = uxon_agents.probe_agents(["cursor"], launch_user=None)
        self.assertEqual(result["cursor"].status, "missing")

    def test_probe_timeout(self) -> None:
        with mock.patch(
            "uxon.agents.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=1.5),
        ):
            result = uxon_agents.probe_agents(["claude"], launch_user=None)
        self.assertEqual(result["claude"].status, "timeout")

    def test_probe_uses_sudo_when_launch_user_differs(self) -> None:
        captured: list[list[str]] = []

        def fake_run(cmd, **kw):
            captured.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="v\n", stderr="")

        with mock.patch("uxon.agents.subprocess.run", side_effect=fake_run):
            with mock.patch("uxon.agents._current_user", return_value="root"):
                uxon_agents.probe_agents(["claude"], launch_user="devagent")

        self.assertEqual(len(captured), 1)
        # -iu loads the target user's login env (matches command_prefix_for_user
        # in uxon.cli) so PATH picks up npm-global / nvm / ~/.local/bin.
        self.assertEqual(captured[0][:4], ["sudo", "-niu", "devagent", "--"])
        self.assertIn("claude", captured[0])

    def test_probe_unknown_agent_id_ignored(self) -> None:
        result = uxon_agents.probe_agents(["nosuch"], launch_user=None)
        self.assertNotIn("nosuch", result)


if __name__ == "__main__":
    unittest.main()

"""Tests for the agent catalog and availability probe."""

from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from uxon.domain.agents import DEFAULT_AGENT_CATALOG, permission_mode_for
from uxon.infra import agents as uxon_agents


class CatalogTests(unittest.TestCase):
    def test_catalog_has_three_agents(self) -> None:
        self.assertEqual(set(DEFAULT_AGENT_CATALOG), {"claude", "codex", "cursor"})

    def test_every_agent_has_yolo_and_normal(self) -> None:
        for agent in DEFAULT_AGENT_CATALOG.values():
            ids = [m.id for m in agent.permission_modes]
            self.assertIn("normal", ids, agent.id)
            self.assertIn("yolo", ids, agent.id)
            self.assertEqual(ids[0], "normal", agent.id)  # normal first

    def test_yolo_modes_marked_dangerous(self) -> None:
        for agent in DEFAULT_AGENT_CATALOG.values():
            yolo = permission_mode_for(agent, "yolo")
            self.assertTrue(yolo.dangerous, agent.id)

    def test_cursor_has_no_auto(self) -> None:
        cursor = DEFAULT_AGENT_CATALOG["cursor"]
        self.assertNotIn("auto", [m.id for m in cursor.permission_modes])

    def test_claude_and_codex_have_auto(self) -> None:
        for aid in ("claude", "codex"):
            ids = [m.id for m in DEFAULT_AGENT_CATALOG[aid].permission_modes]
            self.assertIn("auto", ids, aid)

    def test_mode_ids_unique_within_agent(self) -> None:
        for agent in DEFAULT_AGENT_CATALOG.values():
            ids = [m.id for m in agent.permission_modes]
            self.assertEqual(len(ids), len(set(ids)), agent.id)

    def test_yolo_flags(self) -> None:
        self.assertEqual(
            permission_mode_for(DEFAULT_AGENT_CATALOG["claude"], "yolo").flags,
            ("--dangerously-skip-permissions",),
        )
        self.assertEqual(
            permission_mode_for(DEFAULT_AGENT_CATALOG["codex"], "yolo").flags,
            ("--dangerously-bypass-approvals-and-sandbox",),
        )
        self.assertEqual(
            permission_mode_for(DEFAULT_AGENT_CATALOG["cursor"], "yolo").flags,
            ("--yolo",),
        )

    def test_auto_flags(self) -> None:
        self.assertEqual(
            permission_mode_for(DEFAULT_AGENT_CATALOG["claude"], "auto").flags,
            ("--permission-mode", "auto"),
        )
        self.assertEqual(
            permission_mode_for(DEFAULT_AGENT_CATALOG["codex"], "auto").flags,
            ("--full-auto",),
        )
        self.assertIsNone(permission_mode_for(DEFAULT_AGENT_CATALOG["cursor"], "auto"))

    def test_normal_has_no_flags(self) -> None:
        for agent in DEFAULT_AGENT_CATALOG.values():
            mode = permission_mode_for(agent, "normal")
            self.assertEqual(mode.flags, ())


class ProbeOneTests(unittest.TestCase):
    """Tests for the per-binary ``--version`` probe used by ``do_doctor``.

    The parallel multi-agent ``probe_agents`` driver was removed in 0.5.x
    once the host-wide probe in ``uxon.infra.probes`` replaced it.
    """

    def test_probe_ok(self) -> None:
        with mock.patch("uxon.infra.agents.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="1.0.1\n", stderr=""
            )
            result = uxon_agents._probe_one("claude", launch_user=None)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.version, "1.0.1")

    def test_probe_missing_filenotfound(self) -> None:
        with mock.patch(
            "uxon.infra.agents.subprocess.run",
            side_effect=FileNotFoundError("no such binary"),
        ):
            result = uxon_agents._probe_one("codex", launch_user=None)
        self.assertEqual(result.status, "missing")
        self.assertIsNone(result.version)

    def test_probe_missing_nonzero_exit(self) -> None:
        with mock.patch("uxon.infra.agents.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=127, stdout="", stderr="not found"
            )
            result = uxon_agents._probe_one("cursor-agent", launch_user=None)
        self.assertEqual(result.status, "missing")

    def test_probe_timeout(self) -> None:
        with mock.patch(
            "uxon.infra.agents.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=1.5),
        ):
            result = uxon_agents._probe_one("claude", launch_user=None)
        self.assertEqual(result.status, "timeout")

    def test_probe_uses_sudo_when_launch_user_differs(self) -> None:
        captured: list[list[str]] = []

        def fake_run(cmd, **kw):
            captured.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="v\n", stderr="")

        with mock.patch("uxon.infra.agents.subprocess.run", side_effect=fake_run):
            with mock.patch("uxon.infra.agents._current_user", return_value="root"):
                uxon_agents._probe_one("claude", launch_user="dana_agent")

        self.assertEqual(len(captured), 1)
        # -iu loads the target user's login env (matches command_prefix_for_user
        # in uxon.cli) so PATH picks up npm-global / nvm / ~/.local/bin.
        self.assertEqual(captured[0][:4], ["sudo", "-niu", "dana_agent", "--"])
        self.assertIn("claude", captured[0])


if __name__ == "__main__":
    unittest.main()

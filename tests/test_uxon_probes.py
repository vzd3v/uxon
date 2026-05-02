"""Tests for probes.py host binary detection."""

from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from uxon import probes


class ResolvPathsLocalTests(unittest.TestCase):
    """Tests for _resolve_paths_local (same-user probe)."""

    def test_resolve_empty_list(self) -> None:
        result = probes._resolve_paths_local([])
        self.assertEqual(result, {})

    def test_resolve_single_found(self) -> None:
        with mock.patch("uxon.probes.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="tmux\t/usr/bin/tmux\n", stderr=""
            )
            result = probes._resolve_paths_local(["tmux"])
        self.assertEqual(result, {"tmux": "/usr/bin/tmux"})

    def test_resolve_single_not_found(self) -> None:
        with mock.patch("uxon.probes.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="nosuchbin\t\n", stderr=""
            )
            result = probes._resolve_paths_local(["nosuchbin"])
        self.assertEqual(result, {"nosuchbin": None})

    def test_resolve_multiple_mixed(self) -> None:
        with mock.patch("uxon.probes.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="tmux\t/usr/bin/tmux\nclaude\t\ncodex\t/home/user/.npm/codex\n",
                stderr="",
            )
            result = probes._resolve_paths_local(["tmux", "claude", "codex"])
        self.assertEqual(
            result,
            {
                "tmux": "/usr/bin/tmux",
                "claude": None,
                "codex": "/home/user/.npm/codex",
            },
        )

    def test_resolve_timeout(self) -> None:
        with mock.patch("uxon.probes.subprocess.run") as run:
            run.side_effect = subprocess.TimeoutExpired(cmd=["sh"], timeout=2.0)
            result = probes._resolve_paths_local(["tmux", "claude"])
        self.assertEqual(result, {"tmux": None, "claude": None})

    def test_resolve_sh_not_found(self) -> None:
        with mock.patch("uxon.probes.subprocess.run") as run:
            run.side_effect = FileNotFoundError("no sh")
            result = probes._resolve_paths_local(["tmux"])
        self.assertEqual(result, {"tmux": None})

    def test_resolve_nonzero_exit_treated_as_missing(self) -> None:
        with mock.patch("uxon.probes.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="error"
            )
            result = probes._resolve_paths_local(["tmux", "claude"])
        # When exit code != 0, we don't parse the output, so all are None.
        self.assertEqual(result, {"tmux": None, "claude": None})


class ResolvePathsRemoteTests(unittest.TestCase):
    """Tests for _resolve_paths_remote (cross-user probe via sudo)."""

    def test_resolve_empty_list(self) -> None:
        result = probes._resolve_paths_remote([], "user")
        self.assertEqual(result, {})

    def test_resolve_sudo_success(self) -> None:
        with mock.patch("uxon.probes.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="tmux\t/usr/bin/tmux\nclaude\t/home/user/.npm/claude\n",
                stderr="",
            )
            result = probes._resolve_paths_remote(["tmux", "claude"], "otheruser")
        self.assertEqual(
            result,
            {"tmux": "/usr/bin/tmux", "claude": "/home/user/.npm/claude"},
        )
        # Verify the sudo call.
        args = run.call_args[0][0]
        self.assertIn("sudo", args)
        self.assertIn("-n", args)
        self.assertIn("-iu", args)
        self.assertIn("otheruser", args)

    def test_resolve_sudo_nonzero_exit(self) -> None:
        with mock.patch("uxon.probes.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="sudo: no NOPASSWD"
            )
            result = probes._resolve_paths_remote(["tmux", "claude"], "otheruser")
        # Non-zero exit (e.g., no NOPASSWD) → all are None.
        self.assertEqual(result, {"tmux": None, "claude": None})

    def test_resolve_sudo_timeout(self) -> None:
        with mock.patch("uxon.probes.subprocess.run") as run:
            run.side_effect = subprocess.TimeoutExpired(cmd=["sudo"], timeout=2.0)
            result = probes._resolve_paths_remote(["tmux"], "otheruser")
        self.assertEqual(result, {"tmux": None})

    def test_resolve_sudo_not_found(self) -> None:
        with mock.patch("uxon.probes.subprocess.run") as run:
            run.side_effect = FileNotFoundError("no sudo")
            result = probes._resolve_paths_remote(["tmux"], "otheruser")
        self.assertEqual(result, {"tmux": None})


class BinaryStatusTests(unittest.TestCase):
    """Tests for BinaryStatus dataclass."""

    def test_binary_status_frozen(self) -> None:
        bs = probes.BinaryStatus(name="tmux", path="/usr/bin/tmux", install_hint="apt install")
        with self.assertRaises(AttributeError):
            bs.path = "/new/path"  # type: ignore

    def test_binary_status_creation(self) -> None:
        bs = probes.BinaryStatus(name="claude", path=None, install_hint="npm i -g @anthropic-ai/claude-code")
        self.assertEqual(bs.name, "claude")
        self.assertIsNone(bs.path)
        self.assertIn("claude-code", bs.install_hint)


class HostReportTests(unittest.TestCase):
    """Tests for HostReport dataclass."""

    def test_host_report_creation(self) -> None:
        report = probes.HostReport(
            tmux=probes.BinaryStatus("tmux", "/usr/bin/tmux", "apt install"),
            enabled={"claude": probes.BinaryStatus("claude", "/home/u/.npm/claude", "npm i")},
            detected={"codex": probes.BinaryStatus("codex", "/home/u/.npm/codex", "npm i")},
            launch_user="devuser",
        )
        self.assertEqual(report.launch_user, "devuser")
        self.assertEqual(report.tmux.path, "/usr/bin/tmux")
        self.assertIn("claude", report.enabled)
        self.assertIn("codex", report.detected)


class ProbeHostTests(unittest.TestCase):
    """Integration tests for probe_host."""

    def test_probe_host_same_user(self) -> None:
        # Mock the config and _resolve_paths_local.
        mock_cfg = mock.MagicMock()
        mock_cfg.enabled_agents = ["claude"]

        with mock.patch("uxon.probes._resolve_paths_local") as resolve:
            resolve.return_value = {
                "tmux": "/usr/bin/tmux",
                "claude": "/home/u/.npm/claude",
                "codex": None,
                "cursor-agent": None,
            }
            with mock.patch("uxon.probes._current_user", return_value="devuser"):
                report = probes.probe_host(mock_cfg, "devuser")

        self.assertEqual(report.launch_user, "devuser")
        self.assertEqual(report.tmux.path, "/usr/bin/tmux")
        self.assertIn("claude", report.enabled)
        self.assertEqual(report.enabled["claude"].path, "/home/u/.npm/claude")
        # codex is in CATALOG but not enabled and not detected (path is None).
        self.assertNotIn("codex", report.detected)

    def test_probe_host_different_user(self) -> None:
        mock_cfg = mock.MagicMock()
        mock_cfg.enabled_agents = ["claude", "codex"]

        with mock.patch("uxon.probes._resolve_paths_remote") as resolve:
            resolve.return_value = {
                "tmux": "/usr/bin/tmux",
                "claude": "/home/otheruser/.npm/claude",
                "codex": None,
                "cursor-agent": "/home/otheruser/.cursor/cursor-agent",
            }
            with mock.patch("uxon.probes._current_user", return_value="devuser"):
                report = probes.probe_host(mock_cfg, "otheruser")

        self.assertEqual(report.launch_user, "otheruser")
        self.assertEqual(report.tmux.path, "/usr/bin/tmux")
        self.assertIn("claude", report.enabled)
        self.assertIn("codex", report.enabled)
        self.assertEqual(report.enabled["claude"].path, "/home/otheruser/.npm/claude")
        self.assertIsNone(report.enabled["codex"].path)
        # cursor is detected (installed but not enabled).
        self.assertIn("cursor", report.detected)
        self.assertEqual(report.detected["cursor"].path, "/home/otheruser/.cursor/cursor-agent")

    def test_probe_host_empty_enabled(self) -> None:
        mock_cfg = mock.MagicMock()
        mock_cfg.enabled_agents = []

        with mock.patch("uxon.probes._resolve_paths_local") as resolve:
            resolve.return_value = {
                "tmux": "/usr/bin/tmux",
                "claude": "/home/u/.npm/claude",
                "codex": None,
                "cursor-agent": None,
            }
            with mock.patch("uxon.probes._current_user", return_value="devuser"):
                report = probes.probe_host(mock_cfg, "devuser")

        self.assertEqual(report.enabled, {})
        self.assertIn("claude", report.detected)
        self.assertNotIn("codex", report.detected)
        self.assertNotIn("cursor", report.detected)

    def test_install_hints_present(self) -> None:
        """Verify that install hints are set for all binaries."""
        bs_tmux = probes.BinaryStatus("tmux", None, probes._INSTALL_HINTS["tmux"])
        self.assertIn("apt", bs_tmux.install_hint)
        self.assertIn("dnf", bs_tmux.install_hint)

        bs_claude = probes.BinaryStatus("claude", None, probes._INSTALL_HINTS["claude"])
        self.assertIn("npm", bs_claude.install_hint)
        self.assertIn("claude-code", bs_claude.install_hint)

        bs_codex = probes.BinaryStatus("codex", None, probes._INSTALL_HINTS["codex"])
        self.assertIn("npm", bs_codex.install_hint)
        self.assertIn("codex", bs_codex.install_hint)

        bs_cursor = probes.BinaryStatus("cursor-agent", None, probes._INSTALL_HINTS["cursor-agent"])
        self.assertIn("curl", bs_cursor.install_hint)

# SPDX-License-Identifier: MIT
"""Tests for ``do_doctor`` parallel agent probes.

Probes run in a ThreadPoolExecutor with ``timeout_override=2.0``;
output order follows ``cfg.enabled_agents`` regardless of arrival order.
"""

from __future__ import annotations

import io
import os
import pwd
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

_USER = pwd.getpwuid(os.getuid()).pw_name


class DoctorParallelProbeTests(unittest.TestCase):
    """``do_doctor`` parallelises ``_probe_one`` across ``cfg.enabled_agents``."""

    def _stub_cfg(self):
        from uxon.cli import Config

        return Config(
            runtime_user="",
            default_launch_mode="caller",
            enable_all_users_list=False,
            launch_user_by_caller={},
            session_users=[],
            allowed_roots=[],
            session_prefix="uxon-",
            legacy_session_prefixes=(),
            enabled_agents=("claude", "codex", "cursor"),
            default_agent="claude",
            agent_default_args={},
            new_project_root="/tmp",
            repeat_noninteractive_mode="fail",
            tmux_socket_template="/tmp/uxon-{user}.sock",
            tui_refresh_interval_seconds=2.0,
            git_create_enabled=False,
            default_git_remote_profile="",
            git_remote_profiles=[],
        )

    def _stub_probe_host(self, *, present: tuple[str, ...] = ("claude", "codex", "cursor")):
        from uxon import probes

        enabled = {
            aid: probes.BinaryStatus(name=aid, path=f"/fake/{aid}", install_hint="")
            for aid in present
        }
        return probes.HostReport(
            tmux=probes.BinaryStatus(name="tmux", path="/usr/bin/tmux", install_hint=""),
            enabled=enabled,
            detected={},
            launch_user=_USER,
        )

    def test_probes_run_in_parallel_with_2s_timeout(self) -> None:
        """All three probes must run concurrently — proven by a Barrier rendezvous.

        If the executor is sequential the Barrier deadlocks (only one thread
        reaches it at a time) and ``barrier.wait(timeout=2.0)`` raises
        ``BrokenBarrierError``. Robust to scheduler jitter and parallel test
        workers because it does not depend on wall clock.
        """
        import threading

        from uxon import agents as uxon_agents
        from uxon import cli

        barrier = threading.Barrier(3, timeout=2.0)
        call_args: list[dict] = []

        def fake_probe_one(binary, launch_user, *, timeout_override=None):
            call_args.append({"binary": binary, "timeout_override": timeout_override})
            barrier.wait()
            return uxon_agents.AgentAvailability(status="ok", version=f"{binary}-1.0")

        with (
            patch("uxon.probes.probe_host", return_value=self._stub_probe_host()),
            patch.object(uxon_agents, "_probe_one", side_effect=fake_probe_one),
            patch.object(cli, "collect_sessions", return_value=[]),
            patch.object(cli, "collect_sessions_for_user", return_value=[]),
            patch.object(cli, "resolve_config_layers", return_value=({}, [])),
            redirect_stdout(io.StringIO()),
        ):
            cli.do_doctor(self._stub_cfg(), caller_user=_USER, launch_user=_USER, cwd="/tmp")

        # All three were called with the 2 s override (not the default 10 s).
        timeouts = sorted(arg["timeout_override"] for arg in call_args)
        self.assertEqual(timeouts, [2.0, 2.0, 2.0])

    def test_missing_path_skips_probe(self) -> None:
        """Agents not in ``agent_paths`` are reported missing without calling _probe_one."""
        from uxon import agents as uxon_agents
        from uxon import cli

        report = self._stub_probe_host(present=("claude",))

        called: list[str] = []

        def fake_probe_one(binary, launch_user, *, timeout_override=None):
            called.append(binary)
            return uxon_agents.AgentAvailability(status="ok", version="x")

        with (
            patch("uxon.probes.probe_host", return_value=report),
            patch.object(uxon_agents, "_probe_one", side_effect=fake_probe_one),
            patch.object(cli, "collect_sessions", return_value=[]),
            patch.object(cli, "collect_sessions_for_user", return_value=[]),
            patch.object(cli, "resolve_config_layers", return_value=({}, [])),
            redirect_stdout(io.StringIO()) as captured,
        ):
            cli.do_doctor(self._stub_cfg(), caller_user=_USER, launch_user=_USER, cwd="/tmp")

        # _probe_one called only for the present binary (CATALOG name, not path).
        self.assertEqual(called, ["claude"])
        # Output mentions all three agents in cfg order.
        out = captured.getvalue()
        claude_pos = out.index("claude:")
        codex_pos = out.index("codex:")
        cursor_pos = out.index("cursor:")
        self.assertLess(claude_pos, codex_pos)
        self.assertLess(codex_pos, cursor_pos)
        # codex and cursor render MISSING.
        self.assertIn("codex:  -  MISSING", out)
        self.assertIn("cursor:  -  MISSING", out)


class ProbeOneTimeoutOverrideTests(unittest.TestCase):
    """``_probe_one`` honours ``timeout_override`` keyword-only arg."""

    def test_default_timeout_unchanged(self) -> None:
        from uxon import agents as uxon_agents

        captured: dict = {}

        class FakeCP:
            returncode = 0
            stdout = "fake 1.0\n"
            stderr = ""

        def fake_run(cmd, **kwargs):
            captured["timeout"] = kwargs["timeout"]
            return FakeCP()

        with patch.object(uxon_agents.subprocess, "run", side_effect=fake_run):
            uxon_agents._probe_one("/usr/bin/true", None)
        self.assertEqual(captured["timeout"], uxon_agents.PROBE_TIMEOUT_SEC)

    def test_override_replaces_default(self) -> None:
        from uxon import agents as uxon_agents

        captured: dict = {}

        class FakeCP:
            returncode = 0
            stdout = "fake 1.0\n"
            stderr = ""

        def fake_run(cmd, **kwargs):
            captured["timeout"] = kwargs["timeout"]
            return FakeCP()

        with patch.object(uxon_agents.subprocess, "run", side_effect=fake_run):
            uxon_agents._probe_one("/usr/bin/true", None, timeout_override=2.0)
        self.assertEqual(captured["timeout"], 2.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

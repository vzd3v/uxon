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

        agents = {}
        for aid in ("claude", "codex", "cursor"):
            agents[aid] = probes.BinaryStatus(
                name=aid,
                path=f"/fake/{aid}" if aid in present else None,
                install_hint="",
            )
        return probes.HostReport(
            tmux=probes.BinaryStatus(name="tmux", path="/usr/bin/tmux", install_hint=""),
            agents=agents,
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


class DoctorRemoteFlagTests(unittest.TestCase):
    """Stage 10c — opt-in ``uxon doctor --remote`` probes peers.

    Default ``uxon doctor`` stays local-only (the AGENTS.md walk-back
    is gated on the explicit flag). The flag triggers one
    ``fetch_remote_snapshot`` call per configured peer.
    """

    def _stub_cfg(self, remote_hosts=None):
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
            enabled_agents=("claude",),
            default_agent="claude",
            agent_default_args={},
            new_project_root="/tmp",
            repeat_noninteractive_mode="fail",
            tmux_socket_template="/tmp/uxon-{user}.sock",
            tui_refresh_interval_seconds=2.0,
            git_create_enabled=False,
            default_git_remote_profile="",
            git_remote_profiles=[],
            remote_hosts=remote_hosts or [],
        )

    def _stub_probe_host(self):
        from uxon import probes

        return probes.HostReport(
            tmux=probes.BinaryStatus(name="tmux", path="/usr/bin/tmux", install_hint=""),
            agents={
                "claude": probes.BinaryStatus(
                    name="claude", path="/fake/claude", install_hint=""
                ),
                "codex": probes.BinaryStatus(name="codex", path=None, install_hint=""),
                "cursor": probes.BinaryStatus(name="cursor-agent", path=None, install_hint=""),
            },
            launch_user=_USER,
        )

    def _patches(self):
        from contextlib import ExitStack

        from uxon import agents as uxon_agents
        from uxon import cli

        stack = ExitStack()
        stack.enter_context(patch("uxon.probes.probe_host", return_value=self._stub_probe_host()))
        stack.enter_context(
            patch.object(
                uxon_agents,
                "_probe_one",
                return_value=uxon_agents.AgentAvailability(status="ok", version="x"),
            )
        )
        stack.enter_context(patch.object(cli, "collect_sessions", return_value=[]))
        stack.enter_context(patch.object(cli, "collect_sessions_for_user", return_value=[]))
        stack.enter_context(patch.object(cli, "resolve_config_layers", return_value=({}, [])))
        return stack

    def test_no_flag_no_ssh_attempt(self) -> None:
        """Default doctor (no ``--remote``) never calls the collector."""
        from uxon import cli
        from uxon.remote_hosts import RemoteHost

        hosts = [RemoteHost(name="prod", ssh_alias="prod", description="", remote_uxon="uxon")]

        # ``_doctor_remote_rows`` imports ``fetch_remote_snapshot``
        # lazily at call time, so we patch the source module
        # ``uxon.remote_collector.fetch_remote_snapshot`` and assert
        # zero invocations under the default doctor path.
        from uxon import remote_collector

        with self._patches() as stack:
            stack.enter_context(redirect_stdout(io.StringIO()))
            collector_mock = stack.enter_context(
                patch.object(remote_collector, "fetch_remote_snapshot")
            )
            cli.do_doctor(
                self._stub_cfg(remote_hosts=hosts),
                caller_user=_USER,
                launch_user=_USER,
                cwd="/tmp",
                probe_remote=False,
            )
        self.assertEqual(collector_mock.call_count, 0)

    def test_flag_calls_collector_once_per_host(self) -> None:
        from uxon import cli, remote_collector
        from uxon.remote_collector import RemoteSnapshot
        from uxon.remote_hosts import RemoteHost

        hosts = [
            RemoteHost(name="prod", ssh_alias="prod", description="", remote_uxon="uxon"),
            RemoteHost(name="stage", ssh_alias="stage", description="", remote_uxon="uxon"),
        ]

        def _fake_fetch(host, *, ssh_multiplex="auto", **kwargs):
            return RemoteSnapshot(
                host_name=host.name,
                fetched_at_epoch=1.0,
                from_cache=False,
                error=None,
                sessions=[{"user": "u", "name": "uxon-s@claude"}],
                cached_at_epoch=1.0,
            )

        with self._patches() as stack:
            captured = stack.enter_context(redirect_stdout(io.StringIO()))
            mock_fetch = stack.enter_context(
                patch.object(remote_collector, "fetch_remote_snapshot", side_effect=_fake_fetch)
            )
            cli.do_doctor(
                self._stub_cfg(remote_hosts=hosts),
                caller_user=_USER,
                launch_user=_USER,
                cwd="/tmp",
                probe_remote=True,
            )

        self.assertEqual(mock_fetch.call_count, 2)
        out = captured.getvalue()
        self.assertIn("remote_hosts=2:", out)
        self.assertIn("prod  ok", out)
        self.assertIn("stage  ok", out)

    def test_flag_with_no_hosts_reports_cleanly(self) -> None:
        from uxon import cli

        with self._patches() as stack:
            captured = stack.enter_context(redirect_stdout(io.StringIO()))
            cli.do_doctor(
                self._stub_cfg(remote_hosts=[]),
                caller_user=_USER,
                launch_user=_USER,
                cwd="/tmp",
                probe_remote=True,
            )
        self.assertIn("remote_hosts: no remote hosts configured", captured.getvalue())

    def test_json_round_trip_with_remote(self) -> None:
        import json as _json

        from uxon import cli, remote_collector
        from uxon.remote_collector import RemoteSnapshot
        from uxon.remote_hosts import RemoteHost

        hosts = [RemoteHost(name="prod", ssh_alias="prod", description="", remote_uxon="uxon")]

        def _fake_fetch(host, *, ssh_multiplex="auto", **kwargs):
            return RemoteSnapshot(
                host_name=host.name,
                fetched_at_epoch=1.0,
                from_cache=False,
                error=None,
                sessions=[],
                cached_at_epoch=1.0,
            )

        with self._patches() as stack:
            captured = stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(
                patch.object(remote_collector, "fetch_remote_snapshot", side_effect=_fake_fetch)
            )
            cli.do_doctor(
                self._stub_cfg(remote_hosts=hosts),
                caller_user=_USER,
                launch_user=_USER,
                cwd="/tmp",
                json_output=True,
                probe_remote=True,
            )
        env = _json.loads(captured.getvalue())
        self.assertEqual(env["kind"], "doctor")
        self.assertIn("remote_hosts", env["data"])
        self.assertEqual(len(env["data"]["remote_hosts"]), 1)
        row = env["data"]["remote_hosts"][0]
        self.assertEqual(row["name"], "prod")
        self.assertTrue(row["ok"])
        self.assertIsNone(row["error"])

    def test_json_default_omits_remote_hosts(self) -> None:
        """Without ``--remote``, the JSON envelope must not carry the new key."""
        import json as _json

        from uxon import cli

        with self._patches() as stack:
            captured = stack.enter_context(redirect_stdout(io.StringIO()))
            cli.do_doctor(
                self._stub_cfg(),
                caller_user=_USER,
                launch_user=_USER,
                cwd="/tmp",
                json_output=True,
                probe_remote=False,
            )
        env = _json.loads(captured.getvalue())
        self.assertNotIn("remote_hosts", env["data"])


class DoctorAuditLineTests(unittest.TestCase):
    """``uxon doctor`` reports audit-channel status (Bug 2)."""

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
            enabled_agents=("claude",),
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

    def _stub_probe_host(self):
        from uxon import probes

        return probes.HostReport(
            tmux=probes.BinaryStatus(name="tmux", path="/usr/bin/tmux", install_hint=""),
            agents={
                "claude": probes.BinaryStatus(
                    name="claude", path="/fake/claude", install_hint=""
                ),
                "codex": probes.BinaryStatus(name="codex", path=None, install_hint=""),
                "cursor": probes.BinaryStatus(name="cursor-agent", path=None, install_hint=""),
            },
            launch_user=_USER,
        )

    def _patches(self):
        from contextlib import ExitStack

        from uxon import agents as uxon_agents
        from uxon import cli

        stack = ExitStack()
        stack.enter_context(patch("uxon.probes.probe_host", return_value=self._stub_probe_host()))
        stack.enter_context(
            patch.object(
                uxon_agents,
                "_probe_one",
                return_value=uxon_agents.AgentAvailability(status="ok", version="x"),
            )
        )
        stack.enter_context(patch.object(cli, "collect_sessions", return_value=[]))
        stack.enter_context(patch.object(cli, "collect_sessions_for_user", return_value=[]))
        stack.enter_context(patch.object(cli, "resolve_config_layers", return_value=({}, [])))
        return stack

    def test_human_readable_has_audit_line(self) -> None:
        from uxon import audit as au
        from uxon import cli

        with self._patches() as stack:
            captured = stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(patch.object(au, "_detect_sink", return_value="syslog"))
            stack.enter_context(patch.object(au, "_open_sink_socket", return_value=None))
            au.enabled = True
            au._initialized = False
            cli.do_doctor(self._stub_cfg(), caller_user=_USER, launch_user=_USER, cwd="/tmp")
        out = captured.getvalue()
        self.assertIn("audit:", out)
        self.assertIn("sink=syslog", out)

    def test_json_output_has_audit_block(self) -> None:
        import json as _json

        from uxon import audit as au
        from uxon import cli

        with self._patches() as stack:
            captured = stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(patch.object(au, "_detect_sink", return_value="journal"))
            stack.enter_context(patch.object(au, "_open_sink_socket", return_value=None))
            au.enabled = True
            au._initialized = False
            cli.do_doctor(
                self._stub_cfg(),
                caller_user=_USER,
                launch_user=_USER,
                cwd="/tmp",
                json_output=True,
            )
        env = _json.loads(captured.getvalue())
        self.assertIn("audit", env["data"])
        self.assertEqual(env["data"]["audit"]["sink"], "journal")
        self.assertIn(env["data"]["audit"]["sink"], {"journal", "syslog", "none"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

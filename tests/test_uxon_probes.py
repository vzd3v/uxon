"""Host availability through one bounded, argv-preserving command."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from unittest import mock

import pytest
from helpers import make_config

from uxon.app.doctor import do_doctor
from uxon.app.launch_profile import resolve_launch_profile
from uxon.domain.agents import AgentSpec, PermissionMode
from uxon.domain.execution import ExecutionBackendSpec, ExecutionConfig
from uxon.domain.launch_profiles import LaunchConfig, LaunchProfile
from uxon.infra.identity import process_user
from uxon.infra.probes import PROBE_TIMEOUT_SEC, probe_host

pytestmark = pytest.mark.usefixtures("real_execution", "real_host_probes")


def test_public_host_probe_batches_catalog_and_normalizes_failures(subtests):
    catalog = {
        aid: AgentSpec(
            id=aid,
            binary=binary,
            install_hint=f"install {aid}",
            permission_modes=(PermissionMode("normal", "Normal", ()),),
        )
        for aid, binary in (
            ("custom", "custom tool;literal"),
            ("alias", "custom tool;literal"),
            ("missing", "missing"),
            ("omitted", "omitted"),
        )
    }
    output = "tmux\t/usr/bin/tmux\ncustom tool;literal\t/opt/custom\nmissing\t\nmalformed\n\n"
    for backend in ("local", "command"):
        cfg = make_config()
        if backend == "command":
            cfg.execution = ExecutionConfig(
                default_backend="box",
                backends={
                    "box": ExecutionBackendSpec(
                        id="box", kind="command", command_prefix=("boundary", "{user}", "--")
                    ),
                },
            )
        for user in ("alice", "bob"):
            prefix = (
                ["boundary", user, "--"]
                if backend == "command"
                else []
                if user == "alice"
                else ["/usr/bin/sudo", "-n", "-H", "-u", user, "--"]
            )
            for outcome in ("mixed", "nonzero", "timeout", "missing-shell"):
                with subtests.test(backend=backend, user=user, outcome=outcome):
                    calls = []

                    def run(
                        argv,
                        *,
                        timeout,
                        backend=backend,
                        calls=calls,
                        outcome=outcome,
                        prefix=prefix,
                        user=user,
                    ):
                        calls.append(argv)
                        assert timeout == PROBE_TIMEOUT_SEC
                        assert argv[: len(prefix)] == prefix
                        assert argv[len(prefix) : len(prefix) + 2] == ["sh", "-c"]
                        assert "'custom tool;literal'" in argv[-1]
                        assert "omitted" in argv[-1] and "tmux" in argv[-1]
                        if outcome == "timeout":
                            raise subprocess.TimeoutExpired(argv, timeout)
                        if outcome == "missing-shell":
                            raise FileNotFoundError("shell unavailable")
                        return subprocess.CompletedProcess(
                            argv, int(outcome == "nonzero"), output, ""
                        )

                    with (
                        mock.patch("uxon.infra.execution.process_user", return_value="alice"),
                        mock.patch("uxon.infra.probes.run_query", side_effect=run),
                    ):
                        report = probe_host(cfg, user, catalog)
                    assert len(calls) == 1
                    assert report.launch_user == user
                    assert report.tmux.path == ("/usr/bin/tmux" if outcome == "mixed" else None)
                    assert {
                        aid: (status.name, status.path, status.install_hint)
                        for aid, status in report.agents.items()
                    } == {
                        aid: (
                            aid,
                            "/opt/custom"
                            if outcome == "mixed" and aid in {"custom", "alias"}
                            else None,
                            spec.install_hint,
                        )
                        for aid, spec in catalog.items()
                    }

    with (
        mock.patch("uxon.infra.execution.process_user", return_value="alice"),
        mock.patch(
            "uxon.infra.probes.run_query",
            return_value=subprocess.CompletedProcess([], 0, "tmux\t/usr/bin/tmux\n", ""),
        ) as run,
    ):
        empty = probe_host(make_config(), "alice", {})
    assert empty.agents == {} and empty.tmux.path == "/usr/bin/tmux"
    assert run.call_count == 1


def test_broken_executable_shim_is_reported_and_rechecked_after_repair(tmp_path, capsys):
    shim = tmp_path / "agent"
    missing = tmp_path / "missing-executable"
    shim.write_text(f'#!/bin/sh\nexec "{missing}" "$@"\n')
    shim.chmod(0o755)
    cfg = make_config(
        allowed_roots=[str(tmp_path)],
        new_project_root=str(tmp_path),
        tmux_socket_template=str(tmp_path / "tmux.sock"),
    )
    cfg.agents = {"claude": replace(cfg.agents["claude"], binary=str(shim))}
    cfg.launch = LaunchConfig(
        default_profile="claude",
        enabled_profiles=("claude",),
        profiles={"claude": LaunchProfile(id="claude", agent="claude")},
    )
    user = process_user()
    report = probe_host(cfg, user, cfg.agents)
    status = report.agents["claude"]
    assert status.path == str(shim) and not status.launchable
    assert status.availability.status == "error"
    assert status.availability.returncode == 127
    assert str(missing) in status.availability.error
    with pytest.raises(SystemExit):
        resolve_launch_profile(cfg, user, "claude", str(tmp_path), "normal")
    assert str(missing) in capsys.readouterr().err
    do_doctor(cfg, user, user, str(tmp_path), json_output=True)
    result = json.loads(capsys.readouterr().out)["data"]
    assert result["agents"]["claude"]["status"] == "error"
    assert any(str(missing) in issue for issue in result["issues"])
    shim.write_text("#!/bin/sh\nprintf 'test-agent 1.0\\n'\n")
    resolved = resolve_launch_profile(cfg, user, "claude", str(tmp_path), "normal")
    assert resolved.agent.binary == str(shim)
    shim.write_text(f'#!/bin/sh\nexec "{missing}" "$@"\n')
    cfg.agents["claude"] = replace(cfg.agents["claude"], version_args=())
    assert probe_host(cfg, user, cfg.agents).agents["claude"].launchable

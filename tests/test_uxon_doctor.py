"""Doctor exercises real local/peer probes through strict command responses."""

import io
import json
import os
import pwd
import socket
import subprocess
import threading
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

from uxon.app import doctor
from uxon.cli.main import main
from uxon.infra import agents, audit, config_loader, execution, path_probe, probes, tmux
from uxon.infra.config_renderer import render_config
from uxon.infra.remote import collector
from uxon.infra.remote_hosts import RemoteHost

_REAL_PROBE_HOST = probes.probe_host


@pytest.fixture
def doctor_protocol(tmp_path, monkeypatch):
    process_account = pwd.getpwuid(os.getuid())
    user = "dana_agent"
    account = pwd.struct_passwd((user, "x", os.getuid(), os.getgid(), "", str(tmp_path), "/bin/sh"))
    original_account = pwd.getpwnam
    monkeypatch.setattr(
        pwd, "getpwnam", lambda name: account if name == user else original_account(name)
    )
    groups = sorted(os.getgrouplist(process_account.pw_name, os.getgid()))
    original_groups = os.getgrouplist
    monkeypatch.setattr(
        os, "getgrouplist", lambda name, gid: groups if name == user else original_groups(name, gid)
    )
    prefix = ["/usr/bin/sudo", "-n", "-H", "-u", user, "--"]
    operator = tmp_path / "config.toml"
    operator.write_text('default_launch_mode = "caller"\nallowed_roots = []\n')
    cwd = tmp_path / "project" / "sub"
    cwd.mkdir(parents=True)
    (cwd / ".uxon.toml").write_text("hostile = true\n")
    (cwd.parent / ".uxon.toml").write_text('[agents]\ndefault = "unknown"\n')
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(config_loader, "OPERATOR_CONFIG_PATH", operator)
    operator.write_text(
        render_config(
            {
                "default_launch_mode": "fixed",
                "default_launch_user": user,
                "session_users": [user],
                "remote_hosts": [{"name": "prod", "ssh_alias": "prod", "remote_uxon": "uxon"}],
                "allowed_roots": [],
                "new_project_root": str(tmp_path),
                "tmux_socket_template": str(
                    tmp_path / "socket-dir" / "uxon-{execution_backend}.sock"
                ),
                "ssh_multiplex": "off",
                "repeat_noninteractive_mode": "new",
                "launch": {"enabled_profiles": ["cursor"], "default_profile": "cursor"},
                "audit": {"enabled": True},
                "agents": {
                    "codex": {"version_args": ["-V"]},
                    "gone": {"binary": "gone", "mode": [{"id": "normal"}]},
                    "failed": {"binary": "failed", "mode": [{"id": "normal"}]},
                    "noversion": {
                        "binary": "noversion",
                        "version_args": [],
                        "mode": [{"id": "normal"}],
                    },
                },
            }
        )
    )
    cfg = config_loader.load_config()
    original_read = Path.read_text

    def read_text(path, *args, **kwargs):
        return (
            str(os.getuid())
            if str(path) == "/proc/self/loginuid"
            else original_read(path, *args, **kwargs)
        )

    monkeypatch.setattr(Path, "read_text", read_text)
    sink = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sink.bind(str(tmp_path / "audit.socket"))
    monkeypatch.setattr(audit, "_JOURNAL_SOCKET_PATH", str(tmp_path / "audit.socket"))
    monkeypatch.setattr(audit, "_DEV_LOG_PATH", str(tmp_path / "absent-syslog.socket"))
    audit.configure(enabled=True, syslog_facility="user", subcmd="doctor")
    calls = []
    barrier = threading.Barrier(2, timeout=10)
    shell = 'for c in tmux claude codex cursor-agent gone failed noversion; do\n    printf "%s\\t%s\\n" "$c" "$(command -v "$c" 2>/dev/null)"\ndone'

    def discovery(argv, *, timeout):
        assert argv == prefix + ["sh", "-c", shell]
        assert timeout == 2.0
        calls.append(("discovery", tuple(argv)))
        return subprocess.CompletedProcess(
            argv,
            0,
            "tmux\t/usr/bin/tmux\nclaude\t/fake/claude\ncodex\t/fake/codex\ncursor-agent\t\ngone\t/fake/gone\nfailed\t/fake/failed\nnoversion\t/fake/noversion\n",
            "",
        )

    def version(argv, *, timeout):
        assert argv[:6] == prefix
        argv = argv[6:]
        assert timeout == 2.0
        assert argv in [
            ["claude", "--version"],
            ["codex", "-V"],
            ["gone", "--version"],
            ["failed", "--version"],
        ]
        calls.append(("version", tuple(argv)))
        if argv[0] in ("claude", "codex"):
            barrier.wait()
        if argv[0] == "codex":
            raise subprocess.TimeoutExpired(argv, timeout)
        if argv[0] == "gone":
            raise FileNotFoundError("vanished-sentinel")
        if argv[0] == "failed":
            return subprocess.CompletedProcess(argv, 9, "", "failure-sentinel")
        return subprocess.CompletedProcess(argv, 0, "claude 1.2.3\nsecond line\n", "")

    def server(argv, *, timeout):
        assert argv[:6] == prefix
        argv = argv[6:]
        assert argv[:3] == [os.sys.executable, "-m", "uxon.infra.tmux_server_probe"]
        assert argv[3:] in [
            ["--socket", str(tmp_path / "socket-dir" / "uxon-local.sock")],
            ["--default-socket"],
        ]
        assert timeout == cfg.execution.backend_for_user(user).probe_timeout_seconds
        calls.append(("server", tuple(argv)))
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"state": "absent", "sessions": [], "error": ""}), ""
        )

    def target(argv, *, timeout):
        assert argv[:6] == prefix
        argv = argv[6:]
        assert timeout == cfg.execution.backend_for_user(user).probe_timeout_seconds
        if argv == [os.sys.executable, "-m", "uxon.infra.execution_probe"]:
            calls.append(("identity", tuple(argv)))
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps({"euid": account.pw_uid, "egid": account.pw_gid, "groups": groups}),
                "",
            )
        assert argv[:5] == [os.sys.executable, "-m", "uxon.infra.path_probe", "--mode", "inspect"]
        assert argv[5:] == ["--path", str(tmp_path / "socket-dir")]
        calls.append(("path", tuple(argv)))
        return subprocess.CompletedProcess(argv, 0, json.dumps(path_probe.inspect(argv[6])), "")

    with ExitStack() as stack:
        stack.enter_context(patch.object(execution, "run_query", side_effect=target))
        stack.enter_context(patch.object(probes, "probe_host", _REAL_PROBE_HOST))
        stack.enter_context(patch.object(probes, "run_query", side_effect=discovery))
        stack.enter_context(patch.object(agents, "run_query", side_effect=version))
        stack.enter_context(patch.object(tmux, "run_query", side_effect=server))
        try:
            yield cfg, user, operator, cwd, calls
        finally:
            sink.close()


def invoke(fixture, *, json_output=False, remote=False, public_main=False):
    cfg, user, _, cwd, _ = fixture
    output = io.StringIO()
    with redirect_stdout(output):
        if public_main:
            assert main(["doctor", "--json"] + (["--remote"] if remote else [])) == 0
        else:
            assert (
                doctor.do_doctor(
                    cfg, user, user, str(cwd), json_output=json_output, probe_remote=remote
                )
                == 0
            )
    return json.loads(output.getvalue())["data"] if json_output else output.getvalue()


def test_default_json_mixed_statuses_are_parallel_bounded_ordered_and_canonical(doctor_protocol):
    cfg, user, operator, cwd, calls = doctor_protocol
    cfg.remote_hosts = [RemoteHost("prod", "prod", "", "uxon")]
    with patch.object(collector, "run_query", side_effect=AssertionError("unrequested SSH")):
        data = invoke(doctor_protocol, json_output=True, public_main=True)
    assert list(data["agents"]) == ["claude", "codex", "cursor", "gone", "failed", "noversion"]
    assert data["agents"] == {
        "claude": {
            "path": "/fake/claude",
            "status": "ok",
            "version": "claude 1.2.3",
            "error": None,
        },
        "codex": {"path": "/fake/codex", "status": "timeout", "version": None, "error": None},
        "cursor": {"path": None, "status": "missing", "version": None, "error": "not on PATH"},
        "gone": {
            "path": "/fake/gone",
            "status": "missing",
            "version": None,
            "error": "vanished-sentinel",
        },
        "failed": {
            "path": "/fake/failed",
            "status": "missing",
            "version": None,
            "error": "failure-sentinel",
        },
        "noversion": {"path": "/fake/noversion", "status": "ok", "version": None, "error": None},
    }
    assert data["config_paths"] == [str(operator)]
    assert data["cwd"] == str(cwd)
    assert (data["caller_user"], data["launch_user"]) == (pwd.getpwuid(os.getuid()).pw_name, user)
    assert data["tmux"]["socket"] == str(operator.parent / "socket-dir" / "uxon-local.sock")
    assert data["current_socket_sessions"] == data["legacy_default_socket_sessions"] == []
    assert data["issues"] == [f"'cursor' agent binary is not resolvable for {user}"]
    assert data["audit"] == {"enabled": True, "sink": "journal"}
    assert data["repeat_noninteractive_mode"] == "new"
    assert [row["id"] for row in data["launch_profiles"]] == ["cursor"]
    assert "remote_hosts" not in data
    assert len([call for call in calls if call[0] == "version"]) == 4


def test_human_mixed_statuses_socket_config_and_empty_fleet(doctor_protocol):
    cfg, user, operator, _, _ = doctor_protocol
    cfg.remote_hosts = []
    (operator.parent / "socket-dir").write_text("occupied-parent")
    output = invoke(doctor_protocol, remote=True)
    assert f"config_paths={operator}" in output
    assert f"tmux_socket={operator.parent}/socket-dir/uxon-local.sock" in output
    assert "claude:  /fake/claude  ok (claude 1.2.3)" in output
    assert "codex:  /fake/codex  TIMEOUT (>2.0s)" in output
    assert "cursor:  -  MISSING" in output
    assert [output.index(name + ":") for name in ("claude", "codex", "cursor")] == sorted(
        output.index(name + ":") for name in ("claude", "codex", "cursor")
    )
    assert "audit:    enabled, sink=journald-native" in output
    assert f"- 'cursor' agent binary is not resolvable for {user}" in output
    assert "remote_hosts: no remote hosts configured" in output
    assert f"tmux socket parent is not a directory: {operator.parent}/socket-dir" in output


def test_explicit_remote_collects_actual_success_and_failed_peer_once(doctor_protocol):
    cfg, _, _, _, _ = doctor_protocol
    cfg.remote_hosts = [
        RemoteHost("prod", "prod-alias", "", "/opt/uxon", total_timeout=7, connect_timeout=3),
        RemoteHost("stage", "stage-alias", "", "uxon"),
    ]
    calls = []

    def ssh(argv, *, timeout):
        alias = argv[-2]
        assert alias in {"prod-alias", "stage-alias"}
        assert argv[:-2] == [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            "-o",
            f"ConnectTimeout={3 if alias == 'prod-alias' else 5}",
        ]
        command = shlex.split(argv[-1])
        assert command[:4] == (
            ["/opt/uxon", "list", "--all-users", "--json"]
            if alias == "prod-alias"
            else ["uxon", "list", "--all-users", "--json"]
        )
        assert command[4] == "--audit-correlation-id" and len(command) == 6
        assert timeout == (7 if alias == "prod-alias" else 10)
        calls.append(alias)
        envelope = {
            "schema_version": "3",
            "uxon_version": "4.0.0",
            "kind": "list",
            "data": {
                "scope_users": ["alice"],
                "sessions": [
                    {"user": "alice", "name": "uxon-example@claude", "short_id": "example@claude"}
                ],
            },
        }
        return subprocess.CompletedProcess(
            argv,
            0 if alias == "prod-alias" else 255,
            json.dumps(envelope),
            "" if alias == "prod-alias" else "peer-down-sentinel",
        )

    import shlex

    with patch.object(collector, "run_query", side_effect=ssh):
        data = invoke(doctor_protocol, json_output=True, remote=True)
    assert calls == ["prod-alias", "stage-alias"]
    rows = data["remote_hosts"]
    assert [(r["name"], r["ok"], r["sessions"], r["from_cache"]) for r in rows] == [
        ("prod", True, 1, False),
        ("stage", False, 0, False),
    ]
    assert rows[0]["error"] is None
    assert "peer-down-sentinel" in rows[1]["error"]
    assert all(isinstance(row["latency_ms"], int) and row["latency_ms"] >= 0 for row in rows)

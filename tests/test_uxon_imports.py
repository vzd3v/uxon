"""Import and real noninteractive dispatch stay free of heavyweight UI modules."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from uxon import __version__
from uxon.infra.remote.envelope import decode_envelope


def test_cli_import_rejects_all_heavy_namespaces_in_one_fresh_process() -> None:
    env = dict(os.environ)
    env.pop("UXON_DEBUG", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, sys, uxon.cli; print(json.dumps(sorted(sys.modules)))",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
        env=env,
    )
    modules = json.loads(result.stdout)
    forbidden = ("textual", "uxon.tui", "uxon.infra.probes", "uxon.infra.agents", "structlog")
    offenders = [
        m for m in modules if any(m == prefix or m.startswith(prefix + ".") for prefix in forbidden)
    ]
    assert offenders == [], offenders


_DISPATCH = textwrap.dedent("""
    import contextlib, io, json, os, pwd, subprocess, sys
    from pathlib import Path
    from unittest import mock
    from uxon.cli.main import main
    from uxon.infra import config_loader
    action, directory = sys.argv[1:]
    directory = Path(directory)
    config_loader.OPERATOR_CONFIG_PATH = directory / "config.toml"
    config_loader.OPERATOR_CONFIG_PATH.write_text("tmux_socket_template = " + json.dumps(str(directory / "socket" / "uxon.sock")) + "\\n[audit]\\nenabled=false\\n")
    calls = []
    def query(argv, **kwargs):
        argv = list(argv)
        assert kwargs.get("start_new_session") is True
        if argv[0] == "git":
            if argv[-3:] == ["rev-parse", "--short", "HEAD"]:
                calls.append("git-revision")
                return subprocess.CompletedProcess(argv, 0, "abc123\\n", "")
            if argv[-3:] == ["update-index", "-q", "--refresh"]:
                calls.append("git-refresh")
                return subprocess.CompletedProcess(argv, 0, "", "")
            if argv[-3:] == ["status", "--porcelain", "--untracked-files=no"]:
                calls.append("git-status")
                return subprocess.CompletedProcess(argv, 0, "", "")
        if action == "list" and argv[:2] == ["sh", "-lc"] and "command -v" in argv[2]:
            assert argv[2] == 'for c in tmux claude codex cursor-agent; do\\n    printf "%s\\\\t%s\\\\n" "$c" "$(command -v "$c" 2>/dev/null)"\\ndone'
            assert kwargs["timeout"] == 2.0
            calls.append("host-binaries")
            return subprocess.CompletedProcess(argv, 0, "tmux\\t/usr/bin/tmux\\nclaude\\t/usr/bin/claude\\ncodex\\t/usr/bin/codex\\ncursor-agent\\t/usr/bin/cursor-agent\\n", "")
        raise AssertionError("unexpected external command: " + repr(argv))
    user = "alice"
    account = pwd.struct_passwd((user, "x", os.getuid(), os.getgid(), "", str(directory), "/bin/sh"))
    if action == "list":
        demo = directory / "demo"
        demo.mkdir()
        os.environ["UXON_DEMO_HOSTS"] = str(demo)
        from uxon.domain.wire_schema import WIRE_SCHEMA_VERSION
        (demo / "_local.json").write_text(json.dumps({"schema_version": WIRE_SCHEMA_VERSION,
            "data": {"sessions": [{"user": user, "name": "uxon-example@claude",
                "attached": True, "windows": "2", "active_cmd": "claude", "active_path": "/work/example",
                "cpu_pct": 12.5, "rss_kib": 4096}]}}))
    output = io.StringIO()
    with mock.patch("subprocess.run", side_effect=query), mock.patch("pwd.getpwuid", return_value=account), mock.patch("pwd.getpwnam", return_value=account), contextlib.redirect_stdout(output):
        status = main([action, "--json"])
    print(json.dumps({"status": status, "envelope": json.loads(output.getvalue()),
        "modules": sorted(sys.modules), "calls": calls}))
""")


def _dispatch(action: str, directory: Path) -> dict:
    env = dict(os.environ)
    for name in ("UXON_DEBUG", "UXON_DEMO_HOSTS", "SUDO_USER", "SUDO_UID"):
        env.pop(name, None)
    result = subprocess.run(
        [sys.executable, "-c", _DISPATCH, action, str(directory)],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
        env=env,
    )
    data = json.loads(result.stdout)
    forbidden = ("textual", "uxon.tui", "structlog")
    offenders = [
        m
        for m in data["modules"]
        if any(m == prefix or m.startswith(prefix + ".") for prefix in forbidden)
    ]
    assert offenders == [], offenders
    assert data["status"] == 0
    assert data["envelope"]["uxon_version"] == __version__
    assert data["envelope"]["kind"] == action
    return data


def test_version_dispatch_produces_json_without_host_probes(tmp_path: Path) -> None:
    result = _dispatch("version", tmp_path)
    assert result["calls"] == ["git-revision", "git-refresh", "git-status"]
    assert result["envelope"]["data"] == {
        "uxon_version": __version__,
        "commit": "abc123",
        "commit_dirty": False,
    }
    assert not any(
        m == "uxon.infra.probes" or m.startswith("uxon.infra.probes.") or m == "uxon.infra.agents"
        for m in result["modules"]
    )


def test_list_dispatch_produces_decodable_sessions_without_ui_imports(tmp_path: Path) -> None:
    result = _dispatch("list", tmp_path)
    assert result["calls"] == ["host-binaries"]
    body = result["envelope"]["data"]
    assert body["all_users"] is False and body["session_prefix"] == "uxon-"
    assert len(body["scope_users"]) == 1
    decoded = decode_envelope(json.dumps(result["envelope"]))
    assert decoded.error is None
    assert decoded.sessions is not None and len(decoded.sessions) == 1
    row = decoded.sessions[0]
    assert row["user"] == body["scope_users"][0]
    assert {
        key: row[key]
        for key in (
            "name",
            "short_id",
            "attached",
            "windows",
            "active_cmd",
            "active_path",
            "cpu_pct",
            "rss_kib",
        )
    } == {
        "name": "uxon-example@claude",
        "short_id": "example@claude",
        "attached": True,
        "windows": "2",
        "active_cmd": "claude",
        "active_path": "/work/example",
        "cpu_pct": 12.5,
        "rss_kib": 4096,
    }

"""Real engine launch, nonce attribution, exact workload stop and down state."""

import json
import subprocess
import time

import pytest
from conftest import (
    BASE_IMAGE,
    COMPOSE_TEMPLATE,
    PROBE_TIMEOUT_SEC,
    STOP_HELPER,
    STUB_AGENT_SELECTABLE,
    Runtime,
    operator_runtime_table,
)
from helpers import make_config

from uxon.app import kill, launch
from uxon.app.launch_profile import resolve_launch_profile
from uxon.domain.args import ParsedArgs
from uxon.domain.launch_profiles import LaunchConfig, LaunchProfile
from uxon.domain.runtime import runtime_pidfile
from uxon.domain.session import to_tui_session
from uxon.infra import config_loader, launch_records, sessions_probe, tmux
from uxon.infra.identity import process_user
from uxon.tui.dashboard.columns import format_cpu
from uxon.tui.dashboard.row import from_tui_session

pytestmark = [pytest.mark.container, pytest.mark.usefixtures("real_execution", "real_host_probes")]


def _engine(rt, *args):
    result = subprocess.run(
        [rt.binary, *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=PROBE_TIMEOUT_SEC,
        check=False,
    )
    assert result.returncode == 0, f"{args}: rc={result.returncode}: {result.stderr}"
    return result.stdout


def _in_runtime_pids(rt):
    output = _engine(rt, "top", rt.runtime_name)
    assert output.strip(), "engine top must return the live container process table"
    return output


def _running_container(rt):
    [info] = json.loads(_engine(rt, "inspect", rt.runtime_name))
    assert (
        info["State"]["Running"]
        and info["State"]["Pid"] > 0
        and info["Id"]
        and info["State"]["StartedAt"]
    )
    assert (
        _engine(
            rt,
            "exec",
            rt.runtime_name,
            "python3",
            "-c",
            "import os; os.kill(1,0); print('pid1-alive')",
        ).strip()
        == "pid1-alive"
    )
    assert "tail" in _in_runtime_pids(rt)
    return info


def _workload(rt, nonce, *, previous_pid=None):
    code = """
import json, os, pathlib, stat, sys
path=pathlib.Path(sys.argv[1]); previous=int(sys.argv[2]); exists=path.exists()
pid,ticks=map(int,path.read_text().split()) if exists else (previous,0)
proc=pathlib.Path('/proc')/str(pid)
alive=(proc/'stat').exists()
live_ticks=int((proc/'stat').read_text().rsplit(') ',1)[1].split()[19]) if alive else 0
env=(proc/'environ').read_bytes().split(bytes([0])) if alive else []
marker=next((e.split(b'=',1)[1].decode() for e in env if e.startswith(b'UXON_LAUNCH_NONCE=')), '')
print(json.dumps(dict(record=exists,pid=pid,ticks=ticks,alive=alive,live_ticks=live_ticks,nonce=marker,
                     mode=stat.S_IMODE(path.stat().st_mode) if exists else 0)))
"""
    return json.loads(
        _engine(
            rt,
            "exec",
            rt.runtime_name,
            "python3",
            "-c",
            code,
            runtime_pidfile(nonce),
            str(previous_pid or 0),
        )
    )


def test_managed_runtime_lifecycle_reaps_exact_nonce_preserves_sibling_and_reports_down(
    runtime: Runtime, tmp_path, monkeypatch, request
):
    monkeypatch.delenv("TMUX", raising=False)
    target = tmp_path / "repo"
    target.mkdir()
    socket = tmp_path / "tmux.sock"

    def cleanup_tmux():
        result = subprocess.run(
            ["tmux", "-S", str(socket), "kill-server"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=PROBE_TIMEOUT_SEC,
            check=False,
        )
        request.node.add_report_section(
            "teardown", "owned tmux cleanup", f"rc={result.returncode}: {result.stderr.strip()}"
        )

    request.addfinalizer(cleanup_tmux)
    stub = target / "claude-stub"
    stub.write_text(STUB_AGENT_SELECTABLE)
    stub.chmod(0o755)
    (target / "compose.yml").write_text(
        COMPOSE_TEMPLATE.format(
            image=BASE_IMAGE,
            name=runtime.runtime_name,
            uid=runtime.container_uid,
            gid=runtime.container_gid,
            project_dir=target,
            stub_path=stub,
            stopper_path=STOP_HELPER,
        )
    )
    cfg = make_config(
        allowed_roots=[str(target)],
        tmux_socket_template=str(socket),
        launch=LaunchConfig(
            default_profile="claude",
            profiles={"claude": LaunchProfile(id="claude", agent="claude", runtime="it")},
        ),
        runtimes=config_loader.build_runtimes({"it": operator_runtime_table(runtime, target)}),
    )
    cfg.tmux_options["remain-on-exit"] = "on"
    user = process_user()
    resolved = resolve_launch_profile(cfg, user, "claude", str(target), "normal")
    launch.ensure_runtime_ready(cfg, str(target), resolved)
    initial = _running_container(runtime)
    for index, agent_args in enumerate((("busy",), ()) if runtime.binary == "docker" else ((), ())):
        managed_request, _ = tmux.build_managed_tmux_launch_request(
            str(target),
            f"uxon-it@claude-{index + 1}",
            ParsedArgs(action="run", agent_args=list(agent_args)),
            cfg,
            None,
            resolved_profile=resolved,
            server_running=index > 0,
        )
        tmux.prepare_managed_launch(managed_request)
    deadline = time.monotonic() + PROBE_TIMEOUT_SEC
    sessions = []
    while time.monotonic() < deadline:
        sessions = sessions_probe.collect_sessions([user], cfg)
        if len(sessions) == 2 and all(
            s.launch_record_verified and s.runtime_id and s.runtime_epoch for s in sessions
        ):
            busy, idle = sorted(sessions, key=lambda session: session.name)
            if runtime.binary != "docker" or (
                busy.cpu_pct > 50 and idle.cpu_pct < 50 and all(s.rss_kib > 0 for s in sessions)
            ):
                break
        time.sleep(0.2)
    assert len(sessions) == 2 and (target / ".agent-ran").exists()
    busy, idle = sorted(sessions, key=lambda s: s.name)
    assert (
        busy.launch_nonce != idle.launch_nonce
        and len(busy.launch_nonce) == len(idle.launch_nonce) == 32
    )
    records = []
    for session in (busy, idle):
        assert session.launch_record_verified and session.runtime_resource == runtime.runtime_name
        assert (
            session.runtime_id == initial["Id"]
            and session.runtime_epoch == initial["State"]["StartedAt"]
        )
        assert (
            session.runtime_cgroup
            and session.runtime_dir == "/work"
            and session.project_slug == "repo"
        )
        metadata = launch_records.TmuxSessionMetadata(
            session.tmux_session_id,
            session.tmux_session_created,
            session.name,
            session.launch_nonce,
        )
        payload = launch_records.read_verified_record(str(socket), metadata)
        assert payload is not None and payload["runtime_cgroup"] == session.runtime_cgroup
        records.append(metadata)
    first = _workload(runtime, busy.launch_nonce)
    second = _workload(runtime, idle.launch_nonce)
    for session, workload in ((busy, first), (idle, second)):
        assert workload["record"] and workload["mode"] == 0o600 and workload["alive"]
        assert (
            workload["ticks"] == workload["live_ticks"] > 0
            and workload["nonce"] == session.launch_nonce
        )
    assert first["pid"] != second["pid"] and first["pid"] > 1 and second["pid"] > 1
    assert "sleep" in _in_runtime_pids(runtime)
    if runtime.binary == "docker":
        assert busy.cpu_pct > 50 and idle.cpu_pct < 50 and busy.rss_kib > 0 and idle.rss_kib > 0
        assert "red" in str(
            format_cpu(from_tui_session(to_tui_session(busy, cfg.session_prefix))).style
        )
        assert "red" not in str(
            format_cpu(from_tui_session(to_tui_session(idle, cfg.session_prefix))).style
        )
    assert to_tui_session(busy, cfg.session_prefix).cmd == "claude"
    assert (
        kill.do_kill(
            ParsedArgs(action="kill", target_id=busy.name, force=True, json_output=True), cfg, user
        )
        == 0
    )
    after = _workload(runtime, busy.launch_nonce, previous_pid=first["pid"])
    assert not after["record"] and (not after["alive"] or after["live_ticks"] != first["ticks"])
    survivor = _workload(runtime, idle.launch_nonce)
    assert survivor == second
    assert _running_container(runtime)["State"]["Pid"] == initial["State"]["Pid"]
    assert launch_records.read_verified_record(str(socket), records[0]) is None
    assert launch_records.read_verified_record(str(socket), records[1]) is not None
    [remaining] = sessions_probe.collect_sessions([user], cfg)
    assert (
        remaining.name == idle.name
        and remaining.launch_nonce == idle.launch_nonce
        and not remaining.runtime_down
    )
    if runtime.binary == "docker":
        _engine(runtime, "stop", runtime.runtime_name)
        [stopped] = json.loads(_engine(runtime, "inspect", runtime.runtime_name))
        assert not stopped["State"]["Running"] and stopped["State"]["Pid"] == 0
        deadline = time.monotonic() + PROBE_TIMEOUT_SEC
        while time.monotonic() < deadline:
            rows = sessions_probe.collect_sessions([user], cfg)
            if rows and rows[0].runtime_down:
                break
            time.sleep(0.1)
        assert len(rows) == 1 and rows[0].name == idle.name and rows[0].launch_record_verified
        assert (
            rows[0].runtime_down
            and rows[0].cpu_pct == 0
            and rows[0].rss_kib == 0
            and rows[0].exited
        )
        assert launch_records.read_verified_record(str(socket), records[1]) is not None
    else:
        assert (
            kill.do_kill(ParsedArgs(action="kill", target_id=idle.name, force=True), cfg, user) == 0
        )
        _running_container(runtime)
        assert launch_records.read_verified_record(str(socket), records[1]) is None

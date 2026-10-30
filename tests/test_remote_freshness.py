"""Remote failures exclude unconfirmed data and preserve retry diagnostics."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from helpers import make_config
from textual.worker import WorkerState

from uxon.domain.args import ParsedArgs
from uxon.domain.wire_schema import WIRE_SCHEMA_VERSION, RemoteSnapshot
from uxon.infra.remote.envelope import decode_envelope
from uxon.infra.remote_hosts import RemoteHost
from uxon.tui.dashboard.buckets import select_fleet_summary, select_host_status_block
from uxon.tui.dashboard.model import select_dashboard_model
from uxon.tui.dashboard.ui_state import MainScreenUiState
from uxon.tui.messages import _RefreshSourceLanded
from uxon.tui.refresh import SourceSpec, run_source
from uxon.tui.source_dispatch import handle_remote_snapshot
from uxon.tui.tui_state import TuiState
from uxon.tui.workers import WorkerCoordinator


def _envelope(schema=WIRE_SCHEMA_VERSION, **data):
    return json.dumps(
        {
            "schema_version": schema,
            "uxon_version": "0.9.0",
            "kind": "list",
            "data": {"sessions": [], **data},
        }
    )


def _snapshot(host="build-box", **changes):
    snap = RemoteSnapshot(
        host,
        1,
        None,
        sessions=[{"user": "alice", "name": "uxon-job@claude", "cpu_pct": 20}],
        host_stats={"mem_used_kib": 90, "mem_total_kib": 100},
    )
    return replace(snap, **changes)


@pytest.mark.parametrize("duplicate", [False, True])
def test_first_failure_removes_ram_rows_metrics_and_foreign_user_discovery(duplicate):
    import subprocess

    from uxon.infra.remote.collector import fetch_remote_snapshot

    app = SimpleNamespace(state=TuiState(), main_ui=MainScreenUiState(), _render=Mock())
    app.main_ui.seen_users.add("alice")
    healthy = _snapshot()
    sibling = _snapshot("test-box")
    for snap in (healthy, sibling):
        handle_remote_snapshot(app, _RefreshSourceLanded(f"remote:{snap.host_name}", snap))
    cfg = SimpleNamespace(remote_hosts=[SimpleNamespace(name=n) for n in ("build-box", "test-box")])
    ui = app.main_ui.ui
    assert len(select_dashboard_model(app.state, cfg, ui)) == 2
    rows = [{"name": "stale", "user": "bob", "cpu_pct": {}}]
    if duplicate:
        rows = [{"name": "stale", "user": "bob"}] * 2
    with (
        patch(
            "uxon.infra.remote.collector.run_query",
            return_value=subprocess.CompletedProcess([], 0, _envelope(sessions=rows), ""),
        ),
        patch("uxon.infra.audit.audit") as audit,
    ):
        failed = fetch_remote_snapshot(
            RemoteHost("build-box", "edge", "", "uxon"), ssh_multiplex="off"
        )
    assert failed.error_code == "invalid_response" and "sessions[" in failed.error
    assert audit.call_args.kwargs["error"] == failed.error[:256]
    result = run_source(SourceSpec("remote:build-box", lambda: failed))
    assert result.error == failed.error
    handle_remote_snapshot(app, _RefreshSourceLanded(result.name, result.value, result.error or ""))
    assert [r.host for r in select_dashboard_model(app.state, cfg, ui)] == ["test-box"]
    assert "bob" not in app.main_ui.seen_users
    assert app.state.remote["build-box"].consecutive_failures == 1
    # Selectors fail closed even if an old producer retained historical rows.
    app.state.remote["build-box"] = replace(app.state.remote["build-box"], value=failed)
    lines = select_host_status_block((), app.state, None, cfg)
    line = lines[1]
    assert line.session_count == line.mem_total_kib == line.cpu_pct_sum == 0
    assert "invalid response" in line.failure and "\n" not in line.failure
    assert "duplicate identity" in line.failure if duplicate else "cpu_pct" in line.failure
    assert len(line.failure) < 300
    assert select_fleet_summary(lines).alerts == ("test-box mem 90%",)
    handle_remote_snapshot(app, _RefreshSourceLanded("remote:build-box", healthy))
    assert len(select_dashboard_model(app.state, cfg, ui)) == 2


def _coordinator():
    app = SimpleNamespace(
        cfg=SimpleNamespace(),
        ctx=SimpleNamespace(refresh_sources=[]),
        run_worker=Mock(),
        post_message=Mock(return_value=True),
        _instance_epoch=1,
    )
    workers = []
    callbacks = []

    def spawn(callback, **_kw):
        worker = SimpleNamespace(state=WorkerState.RUNNING)
        workers.append(worker)
        callbacks.append(callback)
        return worker

    app.run_worker.side_effect = spawn
    return app, WorkerCoordinator(app), workers, callbacks


def test_cooldown_survives_context_replacement_manual_bypasses_once_and_landing_gate(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("uxon.domain.host_breaker.time.monotonic", lambda: now[0])
    monkeypatch.setattr(
        "uxon.domain.host_breaker.random.Random.uniform", lambda _, a, b: (a + b) / 2
    )
    app, coord, workers, callbacks = _coordinator()
    spec = SourceSpec(
        "remote:build-box", lambda: _snapshot(error="mismatch", error_code="update_remote")
    )
    coord.kick_source(spec)
    callbacks[0]()
    workers[0].state = WorkerState.SUCCESS
    # Completion is not landing: even manual requests remain single-flight.
    coord.kick_source(spec, manual=True)
    assert len(workers) == 1
    event = app.post_message.call_args.args[0]
    assert event.value.error_code == "update_remote" and event.value.host_name == "build-box"
    assert coord.source_landed(event)
    coord.kick_source(spec)
    assert len(workers) == 1
    replacement = replace(spec, fetch=lambda: _snapshot())
    app.ctx = SimpleNamespace(refresh_sources=[replacement])
    coord.kick_refresh()
    assert len(workers) == 1
    coord.kick_refresh(manual=True)
    coord.kick_refresh(manual=True)
    assert len(workers) == 2
    callbacks[1]()
    assert app.post_message.call_args.args[0].value.sessions == _snapshot().sessions
    workers[1].state = WorkerState.SUCCESS
    assert coord.source_landed(app.post_message.call_args.args[0])
    coord.kick_source(spec)
    assert len(workers) == 3


def test_cancelled_pending_attempt_cannot_land_over_replacement():
    app, coord, workers, callbacks = _coordinator()
    old = _snapshot(sessions=[{"name": "old"}])
    new = _snapshot(sessions=[{"name": "replacement"}])
    spec = SourceSpec("remote:build-box", lambda: old)
    replacement = replace(spec, fetch=lambda: new)
    coord.kick_source(spec)
    workers[0].state = WorkerState.CANCELLED
    coord.kick_source(replacement)
    assert len(workers) == 2
    callbacks[0]()
    app.post_message.assert_not_called()
    assert not coord.source_landed(_RefreshSourceLanded(spec.name, _snapshot(), attempt_id=1))
    coord.kick_source(spec, manual=True)
    assert len(workers) == 2
    callbacks[1]()
    assert app.post_message.call_args.args[0].value.sessions == [{"name": "replacement"}]
    workers[1].state = WorkerState.SUCCESS
    assert coord.source_landed(app.post_message.call_args.args[0])


def test_cancelled_started_attempt_remains_reserved_until_landing():
    app, coord, workers, callbacks = _coordinator()
    spec = SourceSpec("remote:build-box", lambda: _snapshot())
    coord.kick_source(spec)
    callbacks[0]()
    workers[0].state = WorkerState.CANCELLED
    coord.kick_source(spec, manual=True)
    assert len(workers) == 1
    assert coord.source_landed(app.post_message.call_args.args[0])
    coord.kick_source(spec)
    assert len(workers) == 2


def test_worker_control_exception_keeps_propagation_and_allows_retry():
    app, coord, workers, callbacks = _coordinator()

    def interrupted():
        raise SystemExit(2)

    spec = SourceSpec("remote:build-box", interrupted)
    coord.kick_source(spec)
    with pytest.raises(SystemExit):
        callbacks[0]()
    workers[0].state = WorkerState.ERROR
    coord.kick_source(spec, manual=True)
    assert len(workers) == 2
    app.post_message.assert_not_called()


def test_context_refresh_reuses_fleet_semaphore_across_old_and_new_fetchers(tmp_path, monkeypatch):
    import os
    import pwd
    import subprocess

    from uxon.infra.config_loader import OPERATOR_CONFIG_PATH, load_config
    from uxon.tui.context_builder import build_tui_context

    peers = ["build-box", "test-box", "spare-box"]
    OPERATOR_CONFIG_PATH.write_text(
        f'fetch_concurrency = 2\nnew_project_root = "{tmp_path}"\n'
        + "".join(f'[[remote_hosts]]\nname = "{name}"\nssh_alias = "{name}"\n' for name in peers)
    )
    monkeypatch.delenv("UXON_DEMO_HOSTS", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    user = pwd.getpwuid(os.geteuid()).pw_name
    cfg = load_config()
    old = build_tui_context(cfg, user, user, str(tmp_path), skeleton=True)

    def absent_tmux(argv, *, timeout):
        assert "uxon.infra.tmux_server_probe" in argv and timeout > 0
        return subprocess.CompletedProcess(
            argv, 0, '{"state":"absent","sessions":[],"error":""}', ""
        )

    with patch("uxon.infra.tmux.run_query", side_effect=absent_tmux):
        new = old.on_refresh()
    fetchers = [
        spec
        for ctx in (old, new)
        for spec in ctx.refresh_sources
        if spec.name.startswith("remote:")
    ]
    assert len(fetchers) == 6
    attempted, cap_entered, release = threading.Event(), threading.Event(), threading.Event()
    lock = threading.Lock()
    admissions, active, peak, outcomes, acquisition_ids = [], [0], [0], [], []
    semaphores = []
    real_acquire = threading.Semaphore.acquire

    def acquire(sem, *args, **kwargs):
        with lock:
            acquisition_ids.append(id(sem))
            semaphores.append(sem)
            if len(acquisition_ids) == len(fetchers):
                attempted.set()
        return real_acquire(sem, *args, **kwargs)

    def query(argv, *, timeout):
        assert argv[0] == "ssh" and argv[-2] in peers and timeout == 10
        with lock:
            admissions.append(argv[-2])
            ordinal = len(admissions)
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            if ordinal == 2:
                cap_entered.set()
        try:
            assert release.wait(5)
            if ordinal == 1:
                raise PermissionError("external fetch failed")
            return subprocess.CompletedProcess(
                argv, 0, _envelope(sessions=[{"name": argv[-2]}]), ""
            )
        finally:
            with lock:
                active[0] -= 1

    def fetch(spec):
        outcomes.append(spec.fetch())

    threads = [threading.Thread(target=fetch, args=(spec,)) for spec in fetchers]
    with (
        patch("threading.Semaphore.__enter__", acquire),
        patch("uxon.infra.remote.collector.run_query", side_effect=query),
    ):
        try:
            for thread in threads:
                thread.start()
            assert attempted.wait(5) and cap_entered.wait(5)
            with lock:
                assert len(admissions) == active[0] == peak[0] == 2
                assert len(set(acquisition_ids)) == 1
        finally:
            release.set()
            for thread in threads:
                thread.join(5)
    assert all(not thread.is_alive() for thread in threads)
    assert len(admissions) == len(outcomes) == 6 and peak[0] == 2 and active[0] == 0
    assert sum(s.error_code == "fetch_error" for s in outcomes) == 1
    assert sorted(s.sessions[0]["name"] for s in outcomes if s.error is None) == sorted(
        admissions[1:]
    )
    for exception in (KeyboardInterrupt, SystemExit):
        with patch("uxon.infra.remote.collector.run_query", side_effect=exception):
            with pytest.raises(exception):
                fetchers[0].fetch()
    # Both real permits remain available after user-control failures.
    semaphore = semaphores[0]
    acquired = []
    try:
        for _ in range(2):
            acquired.append(real_acquire(semaphore, blocking=False))
            assert acquired[-1]
    finally:
        for held in acquired:
            if held:
                semaphore.release()


@pytest.mark.parametrize("json_output", [False, True])
def test_cli_partial_results_hide_failed_rows_and_exit_nonzero(
    json_output, capsys, tmp_path, monkeypatch
):
    import os
    import pwd
    import shlex
    import subprocess
    import sys

    from uxon.app.listing import do_list

    user = pwd.getpwuid(os.getuid()).pw_name
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("UXON_DEMO_HOSTS", raising=False)
    cfg = make_config(tmux_socket_template=str(tmp_path / "uxon.sock"), ssh_multiplex="off")
    cfg.remote_hosts = [RemoteHost(n, n, "", "uxon") for n in ("build-box", "test-box")]
    calls = []

    def ssh(argv, *, timeout):
        assert argv[:-2] == [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            "-o",
            "ConnectTimeout=5",
        ]
        assert timeout == 10 and argv[-2] in {"build-box", "test-box"}
        command = shlex.split(argv[-1])
        assert (
            command[:5] == ["uxon", "list", "--all-users", "--json", "--audit-correlation-id"]
            and len(command) == 6
        )
        calls.append(argv[-2])
        failed = argv[-2] == "test-box"
        payload = _envelope(
            sessions=[{"user": "bob", "name": "stale"}] if failed else _snapshot().sessions,
            **(
                {
                    "error": {"code": "update_remote", "message": "mismatch"},
                    "peer": {"uxon_version": "0.8.0", "schema_version": "1"},
                }
                if failed
                else {}
            ),
        )
        return subprocess.CompletedProcess(argv, int(failed), payload, "failed" if failed else "")

    def absent(argv, *, timeout):
        assert argv == [
            sys.executable,
            "-m",
            "uxon.infra.tmux_server_probe",
            "--socket",
            str(tmp_path / "uxon.sock"),
        ]
        assert timeout == cfg.execution.backend_for_user(user).probe_timeout_seconds
        return subprocess.CompletedProcess(
            argv, 0, '{"state":"absent","sessions":[],"error":""}', ""
        )

    with (
        patch("uxon.infra.tmux.run_query", side_effect=absent),
        patch("uxon.infra.remote.collector.run_query", side_effect=ssh),
    ):
        rc = do_list(ParsedArgs(action="list", all_hosts=True, json_output=json_output), cfg, user)
    output = capsys.readouterr()
    assert calls == ["build-box", "test-box"]
    assert rc == 1 and "stale" not in output.out
    assert "test-box" in output.err
    if json_output:
        local, healthy, error = [json.loads(line) for line in output.out.splitlines()]
        assert "error" not in local["data"] and "peer" not in local["data"]
        assert len(healthy["data"]["sessions"]) == 1
        assert error["data"]["sessions"] == []
        assert error["data"]["peer"]["uxon_version"] == "0.8.0"
        assert decode_envelope(json.dumps(error)).error_code == "update_remote"
    else:
        assert "job@claude" in output.out

"""Remote failures exclude unconfirmed data and preserve retry diagnostics."""

from __future__ import annotations

import json
import random
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from helpers import make_config
from textual.worker import WorkerState

from uxon.domain.args import ParsedArgs
from uxon.domain.host_breaker import BreakerSpec, HostBreaker
from uxon.domain.wire_schema import WIRE_SCHEMA_VERSION, RemoteSnapshot
from uxon.infra.remote.collector import fetch_remote_snapshot
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


@pytest.mark.parametrize(
    "schema,code",
    [
        ("1", "update_remote"),
        ("9", "update_local"),
        (None, "incompatible"),
        ("future", "incompatible"),
        ("03", "incompatible"),
        ("9" * 5000, "incompatible"),
    ],
)
def test_incompatible_metadata_survives_decoding(schema, code):
    result = decode_envelope(_envelope(schema))
    assert result.error_code == code
    assert result.sessions is None
    assert result.peer["uxon_version"] == "0.9.0"


def test_package_version_is_diagnostic_and_relayed_errors_are_failures():
    assert decode_envelope(_envelope()).error is None
    result = decode_envelope(
        _envelope(
            error={"code": "update_remote", "message": "schema mismatch"},
            peer={"uxon_version": "0.8.0", "schema_version": "1"},
            sessions=[{"name": "stale"}],
        )
    )
    assert result.error_code == "update_remote"
    assert result.peer["uxon_version"] == "0.8.0"
    assert result.sessions is None
    assert (
        decode_envelope(_envelope(error={"code": [], "message": "failed"})).error_code
        == "remote_error"
    )
    assert decode_envelope(_envelope(sessions=[None])).error_code == "invalid_response"


def test_nonzero_remote_response_keeps_structured_peer_metadata(tmp_path):
    payload = _envelope(
        error={"code": "timeout", "message": "upstream timeout"},
        peer={"uxon_version": "0.8.0", "schema_version": "3"},
    )
    with patch(
        "uxon.infra.remote.collector.run_query",
        return_value=SimpleNamespace(
            returncode=1,
            stdout=payload,
            stderr="failed",
        ),
    ):
        snap = fetch_remote_snapshot(
            RemoteHost("build-box", "build-box", "", "uxon"), override_state_dir=tmp_path
        )
    assert snap.peer["uxon_version"] == "0.8.0"
    assert snap.error_code == "timeout"
    assert snap.sessions == [] and snap.host_stats is None


def _snapshot(host="build-box", **changes):
    snap = RemoteSnapshot(
        host,
        1,
        False,
        None,
        sessions=[{"user": "alice", "name": "uxon-job@claude", "cpu_pct": 20}],
        host_stats={"mem_used_kib": 90, "mem_total_kib": 100},
    )
    return replace(snap, **changes)


def test_first_failure_removes_ram_rows_metrics_and_foreign_user_discovery():
    app = SimpleNamespace(state=TuiState(), main_ui=MainScreenUiState(), _render=Mock())
    app.main_ui.seen_users.add("alice")
    healthy = _snapshot()
    sibling = _snapshot("test-box")
    for snap in (healthy, sibling):
        handle_remote_snapshot(app, _RefreshSourceLanded(f"remote:{snap.host_name}", snap))
    cfg = SimpleNamespace(remote_hosts=[SimpleNamespace(name=n) for n in ("build-box", "test-box")])
    ui = app.main_ui.ui
    assert len(select_dashboard_model(app.state, cfg, ui)) == 2
    failed = _snapshot(
        error="schema mismatch",
        error_code="update_remote",
        peer={"uxon_version": "[bold]\n" + "x" * 10000},
        from_cache=True,
        sessions=[{"name": "stale", "user": "bob"}],
    )
    result = run_source(SourceSpec("remote:build-box", lambda: failed))
    assert result.error == "schema mismatch"
    handle_remote_snapshot(app, _RefreshSourceLanded(result.name, result.value, result.error or ""))
    assert [r.host for r in select_dashboard_model(app.state, cfg, ui)] == ["test-box"]
    assert "bob" not in app.main_ui.seen_users
    assert app.state.remote["build-box"].consecutive_failures == 1
    # Selectors fail closed even if an old producer retained historical rows.
    app.state.remote["build-box"] = replace(app.state.remote["build-box"], value=failed)
    lines = select_host_status_block((), app.state, None, cfg)
    line = lines[1]
    assert line.session_count == line.mem_total_kib == line.cpu_pct_sum == 0
    assert "update remote" in line.failure and "\n" not in line.failure
    assert len(line.failure) < 100
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


def test_cooldown_survives_context_replacement_manual_bypasses_once_and_landing_gate():
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
    workers[1].state = WorkerState.SUCCESS
    assert coord.source_landed(app.post_message.call_args.args[0])
    coord.kick_source(spec)
    assert len(workers) == 3


def test_cancelled_pending_attempt_cannot_land_over_replacement():
    app, coord, workers, callbacks = _coordinator()
    spec = SourceSpec("remote:build-box", lambda: _snapshot())
    coord.kick_source(spec)
    workers[0].state = WorkerState.CANCELLED
    coord.kick_source(spec)
    assert len(workers) == 2
    callbacks[0]()
    app.post_message.assert_not_called()
    assert not coord.source_landed(_RefreshSourceLanded(spec.name, _snapshot(), attempt_id=1))
    coord.kick_source(spec, manual=True)
    assert len(workers) == 2
    callbacks[1]()
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


def test_compatibility_cooldown_is_fixed_jittered_and_resets_on_success():
    now = [100.0]
    breaker = HostBreaker(BreakerSpec(), clock=lambda: now[0], rng=random.Random(4))
    for _ in range(3):
        breaker.on_failure(incompatible=True)
        assert 270 <= breaker.next_attempt_at - now[0] <= 330
        assert not breaker.should_attempt()
        now[0] = breaker.next_attempt_at
        assert breaker.should_attempt()
    breaker.on_success()
    assert breaker.should_attempt() and breaker.state == "closed"


def test_context_refresh_reuses_fleet_semaphore_across_old_and_new_fetchers():
    from uxon.tui.context_builder import build_tui_context

    cfg = make_config()
    cfg.fetch_concurrency = 1
    cfg.remote_hosts = [RemoteHost("build-box", "build-box", "", "uxon")]

    def rebuild(*args, **kwargs):
        return build_tui_context(*args, skeleton=True, **kwargs)

    started = threading.Event()
    second_entered = threading.Event()
    release = threading.Event()
    calls = []

    def fetch(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 2:
            second_entered.set()
        started.set()
        assert release.wait(3)
        return _snapshot()

    with patch("uxon.infra.remote.collector.fetch_remote_snapshot", side_effect=fetch):
        first = build_tui_context(cfg, "alice", "alice", "/tmp", skeleton=True)
        with (
            patch("uxon.tui.bridge.config_loader.load_config", return_value=cfg),
            patch(
                "uxon.tui.context_builder.build_tui_context",
                side_effect=rebuild,
            ),
        ):
            second = first.on_refresh()
        threads = [threading.Thread(target=ctx.refresh_sources[1].fetch) for ctx in (first, second)]
        threads[0].start()
        assert started.wait(2)
        threads[1].start()
        try:
            assert not second_entered.wait(0.05)
        finally:
            release.set()
            for thread in threads:
                thread.join(3)
        assert len(calls) == 2


@pytest.mark.parametrize("json_output", [False, True])
def test_cli_partial_results_hide_failed_rows_and_exit_nonzero(json_output, capsys):
    from uxon.app.listing import _do_list_all_hosts

    cfg = make_config()
    cfg.remote_hosts = [RemoteHost(n, n, "", "uxon") for n in ("build-box", "test-box")]
    failed = _snapshot(
        "test-box",
        error="mismatch",
        error_code="update_remote",
        from_cache=True,
        peer={"uxon_version": "0.8.0", "schema_version": "1"},
        sessions=[{"user": "bob", "name": "stale"}],
    )
    with (
        patch("uxon.infra.sessions_probe.collect_sessions", return_value=[]),
        patch(
            "uxon.infra.remote.collector.fetch_remote_snapshot",
            side_effect=[_snapshot(), failed],
        ),
    ):
        rc = _do_list_all_hosts(
            ParsedArgs(action="list", all_hosts=True, json_output=json_output), cfg, "alice"
        )
    output = capsys.readouterr()
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


def test_explicit_refresh_binding_requests_manual_bypass():
    from uxon.tui.screens.main import MainScreen

    app = Mock()
    MainScreen.action_refresh(SimpleNamespace(app=app))
    app.kick_refresh.assert_called_once_with(manual=True)

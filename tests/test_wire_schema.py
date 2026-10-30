"""Complete independent session JSON expectations."""

from __future__ import annotations

import json
from dataclasses import replace

from uxon.domain.session import SessionInfo
from uxon.domain.wire_schema import build_session_records, make_envelope
from uxon.infra.remote.envelope import decode_envelope


def test_full_sparse_and_legacy_wire_records_round_trip_with_literal_envelope():
    full = SessionInfo(
        user="alice",
        name="uxon-foo@fast",
        attached="1",
        windows="2",
        created="2026-05-03T12:00:00+00:00",
        last_attached="2026-05-03T12:30:00+00:00",
        pane_pids=(111, 222),
        active_pid=111,
        active_cmd="codex",
        active_path="/srv/work/project",
        cpu_pct=1.5,
        rss_kib=4096,
        agent="codex",
        profile="fast",
        runtime="sandbox",
        runtime_kind="docker",
        runtime_resource="box",
        execution_backend="worker",
        runtime_down=True,
        exited=True,
        active_exit_status=7,
        has_diagnostics=True,
        tmux_session_id="$1",
        tmux_session_created="1777809600",
        socket_path="/run/user/1001/uxon/local.sock",
        execution_fingerprint="a" * 64,
        launch_nonce="abcdefghijklmnopqrstuvwx",
    )
    sparse = replace(
        full,
        name="uxon_baz@claude",
        attached="0",
        windows="0",
        created="",
        last_attached="",
        pane_pids=(),
        active_pid=None,
        active_cmd="",
        active_path="",
        cpu_pct=0,
        rss_kib=0,
        profile="claude",
        agent="claude",
        runtime="direct",
        runtime_kind="direct",
        runtime_resource="",
        execution_backend="local",
        tmux_session_id="$2",
        launch_nonce="",
        runtime_down=False,
        exited=False,
        active_exit_status=None,
        has_diagnostics=False,
        legacy=True,
    )
    expected = [
        {
            "user": "alice",
            "name": "uxon-foo@fast",
            "short_id": "foo@fast",
            "profile": "fast",
            "agent": "codex",
            "runtime": "sandbox",
            "runtime_kind": "docker",
            "runtime_resource": "box",
            "execution_backend": "worker",
            "attached": True,
            "windows": "2",
            "created": "2026-05-03T12:00:00+00:00",
            "last_attached": "2026-05-03T12:30:00+00:00",
            "pane_pids": [111, 222],
            "active_pid": 111,
            "active_cmd": "codex",
            "active_path": "/srv/work/project",
            "cpu_pct": 1.5,
            "rss_kib": 4096,
            "runtime_down": True,
            "exited": True,
            "active_exit_status": 7,
            "has_diagnostics": True,
            "legacy": False,
            "identity": {
                "user": "alice",
                "name": "uxon-foo@fast",
                "session_id": "$1",
                "created": "1777809600",
                "launch_nonce": "abcdefghijklmnopqrstuvwx",
                "socket_path": "/run/user/1001/uxon/local.sock",
                "execution_backend": "worker",
                "execution_fingerprint": "a" * 64,
            },
        },
        {
            "user": "alice",
            "name": "uxon_baz@claude",
            "short_id": "uxon_baz@claude",
            "profile": "claude",
            "agent": "claude",
            "runtime": "direct",
            "runtime_kind": "direct",
            "runtime_resource": "",
            "execution_backend": "local",
            "attached": False,
            "windows": "0",
            "created": "",
            "last_attached": "",
            "pane_pids": [],
            "active_pid": None,
            "active_cmd": "",
            "active_path": "",
            "cpu_pct": 0,
            "rss_kib": 0,
            "runtime_down": False,
            "exited": False,
            "active_exit_status": None,
            "has_diagnostics": False,
            "legacy": True,
            "identity": {
                "user": "alice",
                "name": "uxon_baz@claude",
                "session_id": "$2",
                "created": "1777809600",
                "launch_nonce": "",
                "socket_path": "/run/user/1001/uxon/local.sock",
                "execution_backend": "local",
                "execution_fingerprint": "a" * 64,
            },
        },
    ]
    records = build_session_records([full, sparse], session_prefix="uxon-")
    assert json.loads(json.dumps(records)) == expected
    data = {
        "all_users": True,
        "scope_users": ["alice", "bob"],
        "scope_skipped": ["bob"],
        "session_prefix": "uxon-",
        "sessions": records,
    }
    stats = {
        "cpu_pct": 0,
        "mem_used_kib": 0,
        "mem_total_kib": 100,
        "loadavg_1m": 0,
        "uptime_s": 0,
        "kernel": "fixture",
    }
    wire = json.dumps(
        make_envelope("list", data, uxon_version="4.0.0", host="peer", host_stats=stats)
    )
    assert json.loads(wire) == {
        "schema_version": "3",
        "uxon_version": "4.0.0",
        "kind": "list",
        "host": "peer",
        "host_stats": stats,
        "data": {
            "all_users": True,
            "scope_users": ["alice", "bob"],
            "scope_skipped": ["bob"],
            "session_prefix": "uxon-",
            "sessions": expected,
        },
    }
    parsed = decode_envelope(wire)
    assert parsed.sessions == expected and parsed.peer == {
        "uxon_version": "4.0.0",
        "schema_version": "3",
    }
    assert parsed.scope_skipped == ["bob"] and parsed.host_stats == stats and parsed.error is None
    assert make_envelope("list", {"sessions": []}, uxon_version="4.0.0") == {
        "schema_version": "3",
        "uxon_version": "4.0.0",
        "kind": "list",
        "data": {"sessions": []},
    }
    assert build_session_records([], session_prefix="uxon-") == []
    assert [
        r["attached"]
        for r in build_session_records(
            [replace(sparse, attached=v) for v in ("0", "", "true", "yes", "2")],
            session_prefix="uxon-",
        )
    ] == [False] * 5

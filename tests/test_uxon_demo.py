"""One synthetic fleet forest, real decoders and observed external routing."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from unittest.mock import patch

from helpers import make_config

from uxon.domain.session import SessionInfo
from uxon.infra import demo, sessions_probe
from uxon.infra.remote.collector import fetch_remote_snapshot
from uxon.infra.remote_hosts import RemoteHost


def envelope(records, **extra):
    return {
        "schema_version": "3",
        "uxon_version": "demo",
        "kind": "list",
        "data": {"sessions": records, "scope_users": ["alice", "bob"], "scope_skipped": []},
        **extra,
    }


def test_demo_forest_activation_order_remote_routing_and_complete_local_conversion(
    tmp_path, monkeypatch
):
    record = {
        "user": "alice",
        "name": "uxon-a@claude",
        "attached": True,
        "windows": "3",
        "created": "2026-05-01T10:00:00+00:00",
        "last_attached": "2026-05-20T14:00:00+00:00",
        "pane_pids": [1234, 1235],
        "active_pid": 1234,
        "active_cmd": "claude",
        "active_path": "/srv/work/project",
        "cpu_pct": 12.5,
        "rss_kib": 524288,
        "agent": "claude",
        "legacy": False,
    }
    (tmp_path / "alpha.json").write_text(
        json.dumps(envelope([record], demo_color="magenta", demo_order=2))
    )
    (tmp_path / "bravo.json").write_text(json.dumps(envelope([], demo_order=2)))
    (tmp_path / "zulu.json").write_text(json.dumps(envelope([], demo_order=1)))
    (tmp_path / "last.json").write_text(json.dumps(envelope([])))
    (tmp_path / "_scratch.json").write_text("{}")
    (tmp_path / "README.md").write_text("note")
    (tmp_path / "rendered.yaml").write_text("x: 1")
    local = tmp_path / "_local.json"
    local.write_text(
        json.dumps(
            envelope(
                [
                    record,
                    {**record, "user": "bob", "name": "uxon-b@claude", "attached": False},
                    {**record, "name": "uxon-c@claude", "attached": False},
                ]
            )
        )
    )
    for raw in (None, "   ", str(tmp_path / "missing"), str(tmp_path / "README.md"), str(tmp_path)):
        if raw is None:
            monkeypatch.delenv(demo.DEMO_ENV_VAR, raising=False)
        else:
            monkeypatch.setenv(demo.DEMO_ENV_VAR, raw)
        assert demo.demo_hosts_dir() == (tmp_path if raw == str(tmp_path) else None)
    hosts = demo.synthesize_remote_hosts(tmp_path)
    assert [(h.name, h.ssh_alias, h.color) for h in hosts] == [
        ("zulu", "__uxon_demo__:zulu", None),
        ("alpha", "__uxon_demo__:alpha", "magenta"),
        ("bravo", "__uxon_demo__:bravo", None),
        ("last", "__uxon_demo__:last", None),
    ]
    with (
        patch("uxon.infra.remote.collector.run_query") as remote,
        patch("uxon.infra.tmux.run_query") as tmux,
        patch("uxon.infra.sessions_probe.run_query") as local_query,
    ):
        snap = fetch_remote_snapshot(hosts[1])
        assert snap.host_name == "alpha" and snap.fetched_at_epoch > 0
        assert (
            snap.sessions == [record]
            and snap.peer == {"uxon_version": "demo", "schema_version": "3"}
            and snap.error is None
        )
        expected = SessionInfo(
            user="alice",
            name="uxon-a@claude",
            attached="1",
            windows="3",
            created="2026-05-01T10:00:00+00:00",
            last_attached="2026-05-20T14:00:00+00:00",
            pane_pids=(1234, 1235),
            active_pid=1234,
            active_cmd="claude",
            active_path="/srv/work/project",
            cpu_pct=12.5,
            rss_kib=524288,
            agent="claude",
            legacy=False,
        )
        assert sessions_probe.collect_sessions_for_user(make_config(), "alice", "uxon-", None) == [
            expected,
            replace(expected, name="uxon-c@claude", attached="0"),
        ]
        assert sessions_probe.collect_sessions_for_user(make_config(), "bob", "uxon-", None) == [
            replace(expected, user="bob", name="uxon-b@claude", attached="0")
        ]
        assert sessions_probe.collect_sessions_for_user(make_config(), "carol", "uxon-", None) == []
    remote.assert_not_called()
    tmux.assert_not_called()
    local_query.assert_not_called()
    with patch(
        "uxon.infra.remote.collector.run_query",
        return_value=subprocess.CompletedProcess([], 0, json.dumps(envelope([record])), ""),
    ) as query:
        snap = fetch_remote_snapshot(
            RemoteHost("real", "real-alias", "", "uxon"), ssh_multiplex="off"
        )
        assert snap.sessions == [record]
    query.assert_called_once()
    assert query.call_args.args[0][-2] == "real-alias"
    monkeypatch.delenv(demo.DEMO_ENV_VAR)
    with patch("uxon.infra.remote.collector.run_query") as query:
        assert fetch_remote_snapshot(hosts[1]).sessions == []
        assert "unset" in fetch_remote_snapshot(hosts[1]).error
    query.assert_not_called()
    attempts = []

    def absent_tmux(argv, *, timeout):
        assert (
            "uxon.infra.tmux_server_probe" in argv
            and argv[-1] == "--default-socket"
            and timeout > 0
        )
        attempts.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, '{"state":"absent","sessions":[],"error":""}', ""
        )

    with patch("uxon.infra.tmux.run_query", side_effect=absent_tmux):
        assert sessions_probe.collect_sessions_for_user(make_config(), "alice", "uxon-", None) == []
    assert len(attempts) == 1


def test_remote_and_independent_local_demo_validation_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv(demo.DEMO_ENV_VAR, str(tmp_path))
    host = RemoteHost("peer", "__uxon_demo__:peer", "", "uxon")
    peer, local = tmp_path / "peer.json", tmp_path / "_local.json"
    with patch("uxon.infra.remote.collector.run_query") as query:
        snap = fetch_remote_snapshot(host)
        assert snap.sessions == [] and "not found" in snap.error
        assert sessions_probe.collect_sessions_for_user(make_config(), "alice", "uxon-", None) == []
        for payload, code in [
            ("not json {", "invalid_response"),
            (json.dumps({**envelope([]), "schema_version": "999"}), "update_local"),
            ("[]", "invalid_response"),
            (json.dumps({"schema_version": "3"}), "invalid_response"),
            (json.dumps({"schema_version": "3", "data": {"sessions": {}}}), "invalid_response"),
        ]:
            peer.write_text(payload)
            local.write_text(payload)
            snap = fetch_remote_snapshot(host)
            assert snap.sessions == [] and snap.host_stats is None and snap.error_code == code
            assert (
                sessions_probe.collect_sessions_for_user(make_config(), "alice", "uxon-", None)
                == []
            )
    query.assert_not_called()

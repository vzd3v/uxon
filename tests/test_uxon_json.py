"""Public JSON listing and bulk teardown with protected launch records."""

import io
import json
from contextlib import redirect_stdout
from unittest import mock

import pytest
from harness.session_commands import SessionCommands, session_config

from uxon.app import kill, listing
from uxon.domain.args import ParsedArgs
from uxon.infra import sessions_probe

pytestmark = pytest.mark.usefixtures("real_execution", "real_host_probes")


def test_public_list_uses_verified_records_and_reports_actual_reachable_scope(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = tmp_path / "repo"
    target.mkdir()
    cfg = session_config(
        target, enable_all_users_list=True, session_users=["alice", "bob", "carol", "bob"]
    )
    external = SessionCommands(denied=["carol"])
    with external.installed_boundary():
        fast = external.add(cfg, target, profile="fast")
        safe = external.add(cfg, target, profile="safe")
        box = external.add(cfg, target, profile="boxed")
        spoof = external.add(cfg, target, profile="boxed", stem="spoof", managed=False)
        peer = external.add(cfg, target, user="bob", profile="safe", stem="peer")
        with redirect_stdout(io.StringIO()) as output:
            assert (
                listing.do_list(
                    ParsedArgs(action="list", all_users=True, json_output=True), cfg, "alice"
                )
                == 0
            )
        envelope = json.loads(output.getvalue())
        data = envelope["data"]
        assert envelope["kind"] == "list" and envelope["schema_version"] == "3"
        assert (
            data["scope_users"] == ["alice", "bob"]
            and data["scope_skipped"] == ["carol"]
            and data["all_users"]
        )
        rows = data["sessions"]
        assert [
            (row["name"], row["profile"], row["agent"], row["runtime_resource"]) for row in rows
        ] == [
            (fast.name, "fast", "claude", ""),
            (safe.name, "safe", "claude", ""),
            (box.name, "boxed", "claude", "box-repo"),
            (spoof.name, "boxed", "", ""),
            (peer.name, "safe", "claude", ""),
        ]
        assert rows[2]["runtime"] == "box" and rows[2]["execution_backend"] == "boundary"
        assert rows[0]["rss_kib"] == 4096 and rows[0]["cpu_pct"] == 3
        captured = sessions_probe.collect_sessions(["alice"], cfg)
        managed, unverified = captured[2:]
        assert (
            managed.launch_record_verified
            and managed.runtime_id == "cid-1"
            and managed.runtime_epoch == "epoch-1"
        )
        assert managed.runtime_cgroup == "/box.scope" and managed.runtime_marker == "spoofed-box"
        assert managed.runtime_dir == str(target) and managed.active_path == "/mutable-pane-dir"
        assert (
            not unverified.launch_record_verified
            and not unverified.runtime_id
            and not unverified.launch_user
        )
        with pytest.raises(SystemExit) as failure:
            kill.do_kill(ParsedArgs(action="kill", target_id=spoof.name, force=True), cfg, "alice")
        assert (
            failure.value.code == 2 and "no authoritative launch record" in failure.value.uxon_msg
        )
        assert not external.mutations and not external.stops
    probes = [
        user for user, cmd, _ in external.calls if cmd[-2:] == ["-m", "uxon.infra.execution_probe"]
    ]
    assert probes.count("carol") == 1 and probes.count("bob") == 2
    assert all(user != "carol" for user, cmd, _ in external.calls if "list-sessions" in cmd)


def test_public_bulk_json_preserves_mixed_kill_stop_and_record_results(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = tmp_path / "repo"
    target.mkdir()
    cfg = session_config(target)
    external = SessionCommands()
    with external.installed_boundary(), mock.patch("uxon.infra.audit.audit") as audit:
        rows = [
            external.add(cfg, target, stem=label, failure=fault)
            for label, fault in (
                ("failed-tmux", "tmux"),
                ("failed-stop", "stop"),
                ("failed-record", "record"),
                ("success", ""),
            )
        ]
        snapshots = [row.path.read_bytes() for row in rows]
        with redirect_stdout(io.StringIO()) as output:
            assert (
                kill.do_kill_all(
                    ParsedArgs(action="kill-all", force=True, json_output=True), cfg, "alice"
                )
                == 0
            )
    results = json.loads(output.getvalue())["data"]["sessions"]
    assert results == [
        {"name": row.name, "action": action, "cleanup_outcome": cleanup}
        for row, action, cleanup in zip(
            rows,
            ("failed", "killed", "killed", "killed"),
            ("not_run", "error", "error", "ok"),
            strict=True,
        )
    ]
    assert external.mutations == [("alice", row.name) for row in rows]
    assert external.stops == [row.name for row in rows[1:]]
    assert [row.visible for row in rows] == [True, False, False, False]
    assert [row.path.exists() for row in rows] == [True, True, True, False]
    assert [row.path.read_bytes() for row in rows[:3]] == snapshots[:3]
    terminal = [call.kwargs for call in audit.call_args_list if call.args == ("session.kill_all",)]
    assert terminal == [
        {
            "outcome": "error",
            "target_users": ["alice"],
            "attempted_count": 4,
            "killed_count": 3,
            "failed_count": 1,
            "cleanup_failed_count": 2,
            "dry_run": False,
        }
    ]
    stop_events = [
        call.kwargs for call in audit.call_args_list if call.args == ("runtime.session_stop",)
    ]
    assert [(event["session"], event["outcome"]) for event in stop_events] == [
        (rows[1].name, "error"),
        (rows[2].name, "ok"),
        (rows[3].name, "ok"),
    ]

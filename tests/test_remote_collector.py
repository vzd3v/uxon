"""Peer commands, live-only collection and bounded master recovery."""

from __future__ import annotations

import json
import shlex
import socket
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from uxon.cli.parsing import parse_args
from uxon.infra.config_loader import parse_config
from uxon.infra.remote.collector import fetch_remote_snapshot
from uxon.infra.remote.envelope import ALL_USERS_DISABLED_MARKER, decode_envelope
from uxon.infra.remote.master_recovery import recover_wedged_master
from uxon.infra.remote.ssh_argv import build_peer_ssh_argv
from uxon.infra.remote_hosts import RemoteHost

CID = "12345678-1234-1234-1234-123456789abc"
ROW = {"user": "alice", "name": "uxon-job@codex", "cpu_pct": 0}
STATS = {"cpu_pct": 0, "mem_total_kib": 100, "mem_used_kib": 0}


def envelope(**data):
    stats = data.pop("host_stats", STATS)
    return json.dumps(
        {
            "schema_version": "3",
            "uxon_version": "4.0.0",
            "kind": "list",
            "host_stats": stats,
            "data": {"sessions": [ROW], "scope_skipped": ["bob"], **data},
        }
    )


def host(**changes):
    return RemoteHost(
        **{
            "name": "build-box",
            "ssh_alias": "edge",
            "description": "",
            "remote_uxon": "/opt/u 'xon/uxon",
            **changes,
        }
    )


def hardening(connect=5):
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        f"ConnectTimeout={connect}",
    ]


@pytest.mark.parametrize(
    "interactive,multiplex,template",
    [
        (False, "auto", None),
        (True, "auto", None),
        (True, "off", None),
        (False, "off", None),
        (
            True,
            "off",
            ("ssh", "-J", "gate", "-o", "ControlMaster=yes", "{ssh_alias}", "{remote_command}"),
        ),
        (False, "auto", ("ssh", "{ssh_alias}", "{remote_command}")),
        (True, "auto", ("kubectl", "exec", "pod", "--", "sh", "-c", "{remote_command}")),
    ],
)
def test_peer_builder_command_ownership(tmp_path, monkeypatch, interactive, multiplex, template):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    h = host(command_template=template, extra_ssh_options=("-o", "ProxyJump=bastion"))
    command = "uxon attach 'job name@codex' --user alice"
    argv = build_peer_ssh_argv(
        command_template=h.command_template,
        extra_ssh_options=h.extra_ssh_options,
        ssh_alias=h.ssh_alias,
        remote_uxon=h.remote_uxon,
        remote_command=command,
        allocate_tty=interactive,
        connect_timeout=7,
        ssh_multiplex=multiplex,
        ssh_control_persist_seconds=91,
    )
    if template:
        expected = [
            t.replace("{ssh_alias}", "edge").replace("{remote_command}", command) for t in template
        ]
    else:
        expected = hardening(7)
        if multiplex == "auto":
            expected += [
                "-o",
                "ControlMaster=auto",
                "-o",
                f"ControlPath={tmp_path}/uxon/ssh-%C",
                "-o",
                "ControlPersist=91s",
            ]
        expected += ["-o", "ProxyJump=bastion", "edge", command]
    if interactive and expected[0] == "ssh":
        expected.insert(1 if template else expected.index("edge"), "-tt")
    assert argv == expected
    assert all("{" not in t for t in argv)
    assert (tmp_path / "uxon").exists() == (template is None and multiplex == "auto")
    if (tmp_path / "uxon").exists():
        assert (tmp_path / "uxon").stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("options", [("-T",), ("-qT",), ("-o", "RequestTTY=no")])
def test_default_attach_forces_native_pty_after_extra_option_overrides(options):
    from uxon.infra.run import run_query

    argv = build_peer_ssh_argv(
        command_template=None,
        extra_ssh_options=("-F", "/dev/null", *options),
        ssh_alias="peer.invalid",
        remote_uxon="uxon",
        remote_command="uxon attach job --user alice",
        allocate_tty=True,
        connect_timeout=2,
        ssh_multiplex="off",
    )
    assert argv[-3] == "-tt"
    # Native option expansion reads no user config and opens no connection.
    result = run_query([argv[0], "-G", *argv[1:]], timeout=2)
    assert result.returncode == 0 and "requesttty force" in result.stdout.splitlines()


@pytest.mark.parametrize(
    "template,connect,total,kwargs,expected_connect,expected_total",
    [
        (None, None, None, {}, 5, 10),
        (None, 2, 8, {"connect_timeout": 9, "total_timeout": 30}, 2, 8),
        (None, 0.5, 0.5, {}, 1, 1),
        (("ssh", "-J", "gate", "{ssh_alias}", "{remote_command}"), None, None, {}, 5, 10),
        (("kubectl", "exec", "pod", "--", "sh", "-c", "{remote_command}"), None, None, {}, 5, 10),
    ],
)
def test_live_poll_consumes_command_deadlines_and_never_persists_rows(
    tmp_path, monkeypatch, template, connect, total, kwargs, expected_connect, expected_total
):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    attempts = []
    raw = {
        "name": "build-box",
        "ssh_alias": "edge",
        "remote_uxon": "/opt/u 'xon/uxon",
        "extra_ssh_options": ["-o", "ProxyJump=bastion"],
    }
    if template is not None:
        raw["command_template"] = list(template)
    if connect is not None:
        raw["connect_timeout"] = connect
    if total is not None:
        raw["total_timeout"] = total
    [h] = parse_config({"remote_hosts": [raw]}).remote_hosts

    def query(argv, *, timeout):
        command = shlex.split(argv[-1])
        assert command[0] == h.remote_uxon
        args = parse_args(command[1:])
        assert (args.action, args.all_users, args.json_output, args.audit_correlation_id) == (
            "list",
            True,
            True,
            CID,
        )
        expected = hardening(expected_connect) + [
            "-o",
            "ControlMaster=auto",
            "-o",
            f"ControlPath={tmp_path}/uxon/ssh-%C",
            "-o",
            "ControlPersist=91s",
            "-o",
            "ProxyJump=bastion",
            "edge",
            argv[-1],
        ]
        if template:
            expected = [
                t.replace("{ssh_alias}", "edge").replace("{remote_command}", argv[-1])
                for t in template
            ]
        assert argv == expected and timeout == expected_total
        attempts.append((argv, timeout))
        return subprocess.CompletedProcess(argv, 0, envelope(), "")

    with (
        patch("uxon.infra.remote.collector.run_query", side_effect=query),
        patch("uuid.uuid4", return_value=CID),
        patch("uxon.infra.audit.audit") as audit,
    ):
        snap = fetch_remote_snapshot(h, ssh_control_persist_seconds=91, **kwargs)
    assert len(attempts) == 1
    assert snap.host_name == "build-box" and snap.fetched_at_epoch > 0
    assert (snap.sessions, snap.host_stats, snap.scope_skipped, snap.error, snap.peer) == (
        [ROW],
        STATS,
        ["bob"],
        None,
        {"uxon_version": "4.0.0", "schema_version": "3"},
    )
    audit.assert_called_once_with(
        "list.remote.out",
        outcome="ok",
        peer_name="build-box",
        ssh_alias="edge",
        scope="all-users",
        from_cache=False,
        correlation_id=CID,
        rc=0,
    )
    assert list(tmp_path.rglob("*")) == ([tmp_path / "uxon"] if template is None else [])


@pytest.mark.parametrize(
    "case,rc,stdout,stderr,code,detail",
    [
        ("nonzero", 255, envelope(), "first\nsecond", "ssh_error", "ssh exited 255: first"),
        ("invalid-json", 0, "not json", "", "invalid_response", "invalid JSON"),
        (
            "structured-peer",
            1,
            envelope(
                error={"code": "timeout", "message": "upstream timeout"},
                peer={"uxon_version": "0.8.0", "schema_version": "3"},
            ),
            "failed",
            "timeout",
            "upstream timeout",
        ),
        ("timeout", 124, None, "", "timeout", "ssh timeout after 10s"),
        ("missing", 127, None, "", "ssh_error", "ssh not installed"),
        ("external-os", None, None, "", "fetch_error", "PermissionError"),
    ],
)
def test_failed_live_outcomes_keep_only_diagnostics(
    tmp_path, monkeypatch, case, rc, stdout, stderr, code, detail
):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    attempts = []

    def query(argv, *, timeout):
        assert argv[: len(hardening())] == hardening() and argv[-2] == "edge" and timeout == 10
        attempts.append(argv)
        if case == "timeout":
            raise subprocess.TimeoutExpired(argv, timeout)
        if case == "missing":
            raise FileNotFoundError("ssh")
        if case == "external-os":
            raise PermissionError("denied")
        return subprocess.CompletedProcess(argv, rc, stdout, stderr)

    with (
        patch("uxon.infra.remote.collector.run_query", side_effect=query),
        patch("uxon.infra.audit.audit") as audit,
    ):
        snap = fetch_remote_snapshot(host(), ssh_multiplex="off")
    assert len(attempts) == 1
    assert snap.sessions == snap.scope_skipped == [] and snap.host_stats is None
    assert snap.error_code == code and detail in snap.error
    assert snap.peer == (
        {"uxon_version": "0.8.0", "schema_version": "3"}
        if case == "structured-peer"
        else {"uxon_version": "4.0.0", "schema_version": "3"}
        if case == "nonzero"
        else {}
    )
    assert audit.call_count == 1
    event = audit.call_args.kwargs
    assert (
        event["outcome"] == "error"
        and event["scope"] == "all-users"
        and event["from_cache"] is False
    )
    assert event.get("rc") == rc and event["error"] == snap.error[:256]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("final_rc", [0, 9])
def test_only_all_users_marker_retries_once_with_final_scope_and_event(
    tmp_path, monkeypatch, final_rc
):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    attempts = []

    def query(argv, *, timeout):
        attempts.append(argv)
        assert len(attempts) <= 2 and timeout == 10
        parsed = parse_args(shlex.split(argv[-1])[1:])
        assert parsed.audit_correlation_id == CID and parsed.json_output
        assert parsed.all_users == (len(attempts) == 1)
        return subprocess.CompletedProcess(
            argv,
            1 if len(attempts) == 1 else final_rc,
            envelope(),
            ALL_USERS_DISABLED_MARKER if len(attempts) == 1 else "second failed",
        )

    with (
        patch("uxon.infra.remote.collector.run_query", side_effect=query),
        patch("uuid.uuid4", return_value=CID),
        patch("uxon.infra.audit.audit") as audit,
    ):
        snap = fetch_remote_snapshot(host(), ssh_multiplex="off")
    assert len(attempts) == 2 and snap.scope_limited
    assert snap.sessions == ([ROW] if final_rc == 0 else [])
    assert snap.host_stats == (STATS if final_rc == 0 else None)
    assert audit.call_count == 1
    assert audit.call_args.kwargs["scope"] == "own" and audit.call_args.kwargs["rc"] == final_rc
    assert audit.call_args.kwargs["correlation_id"] == CID
    assert audit.call_args.kwargs["outcome"] == ("ok" if final_rc == 0 else "error")


def test_regular_file_cache_parent_fails_closed_without_process_or_mutation(tmp_path, monkeypatch):
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"unchanged")
    monkeypatch.setenv("XDG_CACHE_HOME", str(sentinel / "cache"))
    with (
        patch("uxon.infra.remote.collector.run_query") as poll,
        patch("uxon.infra.remote.master_recovery.run_query") as recovery,
        patch("os.kill") as kill,
        patch("uxon.infra.audit.audit") as audit,
    ):
        snap = fetch_remote_snapshot(host())
        recover_wedged_master(host())
    poll.assert_not_called()
    recovery.assert_not_called()
    kill.assert_not_called()
    assert sentinel.read_bytes() == b"unchanged" and list(tmp_path.iterdir()) == [sentinel]
    assert snap.sessions == snap.scope_skipped == [] and snap.host_stats is None
    assert snap.error_code == "fetch_error" and "NotADirectoryError" in snap.error
    assert audit.call_count == 1 and audit.call_args.kwargs["outcome"] == "error"
    assert "rc" not in audit.call_args.kwargs
    with patch(
        "uxon.infra.remote.collector.run_query",
        return_value=subprocess.CompletedProcess([], 0, envelope(), ""),
    ) as query:
        assert fetch_remote_snapshot(host(), ssh_multiplex="off").sessions == [ROW]
        assert fetch_remote_snapshot(
            host(command_template=("kubectl", "{remote_command}"))
        ).sessions == [ROW]
    assert query.call_count == 2 and sentinel.read_bytes() == b"unchanged"


@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit])
def test_control_exceptions_propagate_from_poll_and_recovery(tmp_path, monkeypatch, exception):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    with (
        patch("uxon.infra.remote.collector.run_query", side_effect=exception),
        patch("uxon.infra.audit.audit") as audit,
    ):
        with pytest.raises(exception):
            fetch_remote_snapshot(host())
    audit.assert_not_called()
    with patch("uxon.infra.remote.master_recovery.run_query", side_effect=exception):
        with pytest.raises(exception):
            recover_wedged_master(host())
    with (
        patch.object(Path, "mkdir", side_effect=exception),
        patch("uxon.infra.remote.collector.run_query") as poll,
        patch("uxon.infra.remote.master_recovery.run_query") as recovery,
    ):
        with pytest.raises(exception):
            fetch_remote_snapshot(host())
        with pytest.raises(exception):
            recover_wedged_master(host())
    poll.assert_not_called()
    recovery.assert_not_called()


def test_decoder_normalizes_optional_fields_and_retains_compatibility_diagnostics():
    for skipped, stats, expected_skipped, expected_stats in [
        (["alice", "bob"], STATS, ["alice", "bob"], STATS),
        ([], {}, [], {}),
        ([], None, [], None),
    ]:
        result = decode_envelope(
            json.dumps(
                {
                    "schema_version": "3",
                    "uxon_version": "0.9.0",
                    "kind": "list",
                    "data": {"sessions": [ROW], "scope_skipped": skipped},
                    "host_stats": stats,
                }
            )
        )
        assert (result.sessions, result.scope_skipped, result.host_stats, result.error) == (
            [ROW],
            expected_skipped,
            expected_stats,
            None,
        )
    for schema, code in [
        ("1", "update_remote"),
        ("9", "update_local"),
        (None, "incompatible"),
        ("future", "incompatible"),
        ("03", "incompatible"),
        ("9" * 5000, "incompatible"),
    ]:
        result = decode_envelope(
            json.dumps(
                {
                    "schema_version": schema,
                    "uxon_version": "0.9.0",
                    "kind": "list",
                    "data": {"sessions": [ROW]},
                }
            )
        )
        assert result.sessions is None and result.error_code == code
        assert result.peer == (
            {"uxon_version": "0.9.0", "schema_version": schema}
            if isinstance(schema, str)
            else {"uxon_version": "0.9.0"}
        )
    vectors = [
        ("not-json", "invalid JSON"),
        ("[]", "not a JSON object"),
        (json.dumps({"schema_version": "3", "kind": "version"}), "unexpected envelope kind"),
        (json.dumps({"schema_version": "3", "kind": "list"}), "data is not an object"),
        (envelope(sessions={}), "sessions is not a list"),
        (envelope(sessions=[None]), "non-object record"),
        (envelope(error="failure"), "not a remote error"),
    ]
    for payload, message in vectors:
        result = decode_envelope(payload)
        assert result.sessions is None and result.host_stats is None and result.scope_skipped == []
        assert result.error_code == "invalid_response" and message in result.error
    for code, expected in [("update_remote", "update_remote"), ([], "remote_error")]:
        result = decode_envelope(
            envelope(
                error={"code": code, "message": "failed"},
                peer={"uxon_version": "0.8.0", "schema_version": "1"},
            )
        )
        assert (
            result.sessions is None and result.error == "failed" and result.error_code == expected
        )
        assert result.peer == {"uxon_version": "0.8.0", "schema_version": "1"}
    assert decode_envelope(envelope(sessions=[])).sessions == []


def test_decoder_rejects_malformed_known_fields_and_duplicate_rows_without_coercion():
    identity = {
        "user": "alice",
        "name": "uxon-job@claude",
        "session_id": "$1",
        "created": "123456",
        "launch_nonce": "abcdefghijklmnopqrstuvwx",
        "socket_path": "/run/user/1001/uxon/local.sock",
        "execution_backend": "local",
        "execution_fingerprint": "a" * 64,
    }
    valid = {"user": "alice", "name": "uxon-job@claude", "identity": identity}
    for field, value in [
        ("cpu_pct", {}),
        ("cpu_pct", "1.0"),
        ("cpu_pct", -1),
        ("rss_kib", True),
        ("rss_kib", -1),
        ("attached", "false"),
        ("active_pid", 0),
        ("pane_pids", [1, False]),
        ("pane_pids", [-1]),
        ("identity", None),
        ("identity", {**identity, "created": 123456}),
        ("identity", {**identity, "name": "another"}),
        ("identity", {**identity, "launch_nonce": "invalid"}),
    ]:
        result = decode_envelope(envelope(sessions=[{**valid, field: value}]))
        assert result.sessions is None and result.error_code == "invalid_response"
        assert f"sessions[0].{field}" in result.error
    for payload, field in [
        (envelope(sessions=[valid, valid]), "duplicate identity"),
        (envelope(sessions=[ROW, ROW]), "duplicate identity"),
        (envelope(scope_skipped=["alice", 7]), "scope_skipped"),
        (envelope(scope_users=False), "scope_users"),
        (envelope(host_stats=[]), "host_stats"),
        (envelope(host_stats={"mem_total_kib": -1}), "mem_total_kib"),
        (envelope(host_stats={"cpu_pct": "high"}), "cpu_pct"),
        (
            envelope(sessions=[{**valid, "cpu_pct": 1}]).replace(
                '"cpu_pct": 1', '"cpu_pct": 1e999'
            ),
            "Number out of range",
        ),
    ]:
        result = decode_envelope(payload)
        assert result.sessions is None and result.host_stats is None
        assert result.error_code == "invalid_response" and field in result.error
    additive = {**valid, "future": {"arbitrary": True}, "identity": {**identity, "future": 1}}
    result = decode_envelope(envelope(sessions=[additive, {"name": "sparse"}]))
    assert result.error is None and result.sessions == [additive, {"name": "sparse"}]


@pytest.mark.parametrize(
    "template,diagnostic",
    [
        (["ssh", "{bad}"], "unknown placeholder"),
        (["ssh", "{remote_uxon}", "{remote_command}"], "mutually exclusive"),
        ([], "non-empty"),
    ],
)
def test_config_rejects_invalid_transport_templates(template, diagnostic):
    with pytest.raises((ValueError, SystemExit), match=diagnostic):
        parse_config(
            {"remote_hosts": [{"name": "peer", "ssh_alias": "edge", "command_template": template}]}
        )


@pytest.mark.parametrize(
    "case",
    ["ok", "options", "nonzero", "timeout", "oserror", "custom"],
)
def test_recovery_only_uses_bounded_native_ssh_control(tmp_path, monkeypatch, case):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    socket_path = tmp_path / "socket"
    control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        control.bind(str(socket_path))
    finally:
        control.close()
    original_inode = socket_path.stat().st_ino
    attempts = []
    options = ("-p", "2222", "-l", "bob") if case == "options" else ()

    def query(argv, *, timeout):
        attempts.append(argv)
        option = f"ControlPath={tmp_path}/cache/uxon/ssh-%C"
        assert len(attempts) == 1 and timeout == 2
        assert argv == ["ssh", "-O", "exit", "-o", option, *options, "edge"]
        if case == "timeout":
            raise subprocess.TimeoutExpired(argv, timeout)
        if case == "oserror":
            raise OSError("control request failed")
        return subprocess.CompletedProcess(argv, 255 if case == "nonzero" else 0, "", "")

    with (
        patch("uxon.infra.remote.master_recovery.run_query", side_effect=query),
        patch("os.kill") as kill,
        patch("os.unlink") as unlink,
    ):
        recover_wedged_master(
            host(
                command_template=("kubectl", "{remote_command}") if case == "custom" else None,
                extra_ssh_options=options,
            ),
        )
    assert len(attempts) == (0 if case == "custom" else 1)
    kill.assert_not_called()
    unlink.assert_not_called()
    assert socket_path.stat().st_ino == original_inode


@pytest.mark.parametrize(
    "multiplex,template,recovery_count",
    [("auto", None, 1), ("off", None, 0), ("auto", ("kubectl", "{remote_command}"), 0)],
)
def test_poll_timeout_recovery_obeys_transport_ownership(
    tmp_path, monkeypatch, multiplex, template, recovery_count
):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    attempts = []

    def query(argv, *, timeout):
        attempts.append(argv)
        if len(attempts) == 1:
            assert timeout == 10
            raise subprocess.TimeoutExpired(argv, timeout)
        assert len(attempts) == 2 and timeout == 2
        assert argv == ["ssh", "-O", "exit", "-o", f"ControlPath={tmp_path}/uxon/ssh-%C", "edge"]
        raise subprocess.TimeoutExpired(argv, timeout)

    with (
        patch("uxon.infra.remote.collector.run_query", side_effect=query),
        patch("uxon.infra.remote.master_recovery.run_query", side_effect=query),
        patch("os.kill") as kill,
        patch("os.unlink") as unlink,
    ):
        snap = fetch_remote_snapshot(host(command_template=template), ssh_multiplex=multiplex)
    assert snap.error_code == "timeout" and snap.sessions == []
    assert snap.error == "ssh timeout after 10s"
    assert len(attempts) == 1 + recovery_count
    kill.assert_not_called()
    unlink.assert_not_called()

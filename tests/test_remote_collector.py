"""Peer commands, live-only collection and bounded master recovery."""

from __future__ import annotations

import builtins
import json
import shlex
import signal
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
    return json.dumps(
        {
            "schema_version": "3",
            "uxon_version": "4.0.0",
            "kind": "list",
            "host_stats": STATS,
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
        expected.insert(1, "-tt")
    assert argv == expected
    assert all("{" not in t for t in argv)
    assert (tmp_path / "uxon").exists() == (template is None and multiplex == "auto")
    if (tmp_path / "uxon").exists():
        assert (tmp_path / "uxon").stat().st_mode & 0o777 == 0o700


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
        (["alice", 7, "bob"], STATS, ["alice", "bob"], STATS),
        (False, [], [], None),
        ([], {}, [], {}),
        (None, None, [], None),
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
    [
        "residual",
        "removed",
        "nonzero",
        "timeout",
        "missing-exec",
        "missing-path",
        "no-pid",
        "kill-error",
        "stat-error",
        "custom",
        "graceful-timeout",
        "graceful-oserror",
    ],
)
def test_recovery_uses_bounded_ssh_resolver_exact_proc_identity_and_safe_effects(
    tmp_path, monkeypatch, case
):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    socket_path = tmp_path / "socket"
    control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        control.bind(str(socket_path))
    finally:
        control.close()
    proc = tmp_path / "proc"
    proc.mkdir()
    for pid, title in [
        (111, f"ssh: {socket_path}-other [mux]"),
        (222, f"ssh: {socket_path} [mux] extra"),
        (4242, f"ssh: {socket_path} [mux]"),
    ]:
        folder = proc / str(pid)
        folder.mkdir()
        (folder / "cmdline").write_bytes(title.encode() + b"\0ssh\0-O\0exit\0")
    attempts, killed, reads = [], [], []
    real_open, real_stat = builtins.open, Path.stat

    def listdir(path):
        assert path == "/proc"
        return ["self", "111", "222", "4242"] if case != "no-pid" else ["111", "222"]

    def open_proc(path, *args, **kwargs):
        assert str(path).startswith("/proc/") and str(path).endswith("/cmdline")
        reads.append(str(path))
        return real_open(proc / str(path).split("/")[2] / "cmdline", *args, **kwargs)

    def stat(path, *args, **kwargs):
        if case == "stat-error" and path == socket_path:
            raise PermissionError("socket inspection denied")
        return real_stat(path, *args, **kwargs)

    def query(argv, *, timeout):
        attempts.append(argv)
        option = f"ControlPath={tmp_path}/cache/uxon/ssh-%C"
        assert timeout == 2
        if len(attempts) == 1:
            assert argv == ["ssh", "-O", "exit", "-o", option, "edge"]
            if case == "removed":
                socket_path.unlink()
            if case == "graceful-timeout":
                raise subprocess.TimeoutExpired(argv, timeout)
            if case == "graceful-oserror":
                raise OSError("graceful command failed")
            return subprocess.CompletedProcess(argv, 0, "", "")
        assert len(attempts) == 2 and argv == ["ssh", "-G", "-o", option, "edge"]
        if case == "timeout":
            raise subprocess.TimeoutExpired(argv, timeout)
        if case == "missing-exec":
            raise FileNotFoundError("ssh")
        return subprocess.CompletedProcess(
            argv,
            255 if case == "nonzero" else 0,
            "user alice\n"
            if case == "missing-path"
            else f"user alice\ncontrolpath {socket_path}\nport 22\n",
            "",
        )

    def kill(pid, sig):
        assert len(attempts) == 2 and pid == 4242 and sig == signal.SIGKILL
        killed.append((pid, sig))
        if case == "kill-error":
            raise ProcessLookupError(pid)

    with (
        patch("uxon.infra.remote.master_recovery.run_query", side_effect=query),
        patch("uxon.infra.remote.master_recovery.os.listdir", side_effect=listdir),
        patch("builtins.open", side_effect=open_proc),
        patch.object(Path, "stat", stat),
    ):
        recover_wedged_master(
            host(command_template=("kubectl", "{remote_command}") if case == "custom" else None),
            _kill=kill,
        )
    assert len(attempts) == (0 if case == "custom" else 2)
    assert killed == (
        [(4242, signal.SIGKILL)]
        if case in {"residual", "kill-error", "graceful-timeout", "graceful-oserror"}
        else []
    )
    assert socket_path.exists() == (
        case in {"custom", "nonzero", "timeout", "missing-exec", "missing-path", "stat-error"}
    )
    assert bool(reads) == (
        case in {"residual", "no-pid", "kill-error", "graceful-timeout", "graceful-oserror"}
    )


@pytest.mark.parametrize(
    "multiplex,template,recovery_count",
    [("auto", None, 2), ("off", None, 0), ("auto", ("kubectl", "{remote_command}"), 0)],
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
        assert timeout == 2 and argv == (
            ["ssh", "-O", "exit", "-o", f"ControlPath={tmp_path}/uxon/ssh-%C", "edge"]
            if len(attempts) == 2
            else ["ssh", "-G", "-o", f"ControlPath={tmp_path}/uxon/ssh-%C", "edge"]
        )
        return subprocess.CompletedProcess(argv, 0, "user alice\n", "")

    with (
        patch("uxon.infra.remote.collector.run_query", side_effect=query),
        patch("uxon.infra.remote.master_recovery.run_query", side_effect=query),
        patch("os.kill") as kill,
    ):
        snap = fetch_remote_snapshot(host(command_template=template), ssh_multiplex=multiplex)
    assert snap.error_code == "timeout" and snap.sessions == []
    assert len(attempts) == 1 + recovery_count
    kill.assert_not_called()

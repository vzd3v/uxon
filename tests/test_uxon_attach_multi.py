"""Tests for ``uxon attach --user`` and ``uxon attach --host`` (multi-host).

Symmetric to ``test_uxon_kill_multi.py``. Parser-level tests live
here; behaviour tests for cross-user / cross-host execution paths
live in companion classes below.
"""

from __future__ import annotations

import io
import json
import shlex
import subprocess
import sys
from contextlib import redirect_stdout
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

import pytest
from helpers import make_config as _make_config

import uxon.app.attach as attach_app
import uxon.tui.bridge as tui_bridge
from uxon.cli.parsing import parse_args
from uxon.domain.args import ParsedArgs
from uxon.domain.launch_profiles import ResolvedLaunchProfile
from uxon.domain.session import SessionIdentity
from uxon.infra import execution, launch_records
from uxon.infra.remote_hosts import RemoteHost
from uxon.infra.tmux_diagnostics import ACKNOWLEDGED_FORMAT


@pytest.mark.parametrize("producer", ["cli", "bridge"])
@pytest.mark.parametrize(
    "template",
    [
        None,
        ("ssh", "-J", "gate", "-o", "ControlMaster=yes", "{ssh_alias}", "{remote_command}"),
        ("kubectl", "exec", "pod", "--", "sh", "-c", "{remote_command}"),
    ],
)
def test_actual_remote_attach_producer_quotes_and_peer_parser(
    producer, template, tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    cfg = _make_config(
        remote_hosts=[
            RemoteHost(
                "peer",
                "edge",
                "",
                "/opt/u 'xon/uxon",
                command_template=template,
                extra_ssh_options=("-o", "ProxyJump=bastion"),
            )
        ]
    )
    target, user = "job 'quoted@codex", "alice"
    identity = SessionIdentity(
        user, target, "$1", "123", "a" * 32, "/run/uxon/alice.sock", "local", "f" * 64
    )
    output = io.StringIO()
    with (
        mock.patch("uxon.infra.audit.audit") as audit,
        mock.patch("os.execvp") as execute,
        mock.patch("uxon.infra.execution.run_query") as query,
        redirect_stdout(output),
    ):
        if producer == "cli":
            assert (
                attach_app.do_attach(
                    ParsedArgs(
                        action="attach",
                        host="peer",
                        user=user,
                        target_id=target,
                        dry_run=True,
                        expected_session=identity,
                    ),
                    cfg,
                    "bob",
                )
                == 0
            )
            argv = shlex.split(output.getvalue().strip())
        else:
            req = tui_bridge.TuiBridge(cfg, "bob", "bob", str(tmp_path)).on_remote_attach(
                "peer", user, target, identity
            )
            argv = list(req.cmd)
            assert req.label == f"attach {target}@peer"
            assert req.capture_ssh_diagnostics == (not template)
    query.assert_not_called()
    execute.assert_not_called()
    remote = shlex.split(argv[-1])
    assert remote[0] == "/opt/u 'xon/uxon"
    parsed = parse_args(remote[1:])
    assert (parsed.action, parsed.target_id, parsed.user, parsed.host, parsed.force) == (
        "attach",
        target,
        user,
        None,
        False,
    )
    assert parsed.audit_correlation_id is not None
    assert parsed.expected_session == identity
    if template:
        expected = [
            t.replace("{ssh_alias}", "edge").replace("{remote_command}", argv[-1]) for t in template
        ]
        if expected[0] == "ssh":
            expected.insert(1, "-tt")
    else:
        expected = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "ProxyJump=bastion",
            "-tt",
            "edge",
            argv[-1],
        ]
    assert argv == expected and list(tmp_path.iterdir()) == []
    audit.assert_called_once_with(
        "attach.remote.out.dispatch",
        peer_name="peer",
        ssh_alias="edge",
        target_user=user,
        target_session=target,
        session_id=identity.session_id,
        launch_nonce=identity.launch_nonce,
        execution_backend=identity.execution_backend,
        dry_run=producer == "cli",
        correlation_id=parsed.audit_correlation_id,
    )


def test_attach_parser_public_grammar():
    parsed = parse_args(["attach", "demo@claude", "--user", "alice"])
    assert (parsed.target_id, parsed.user, parsed.host) == ("demo@claude", "alice", None)
    parsed = parse_args(["attach", "demo@claude", "--host", "peer", "--user", "alice", "--dry-run"])
    assert parsed.host == "peer" and parsed.user == "alice" and parsed.dry_run
    for argv in (
        ["attach", "demo@claude", "--host", "peer"],
        ["attach", "demo@claude", "--unknown"],
    ):
        with pytest.raises(SystemExit):
            parse_args(argv)


@pytest.mark.parametrize("producer", ["cli", "bridge"])
def test_remote_attach_shared_handoff_preserves_target_and_correlation(producer, tmp_path):
    from uxon.app.session_handoff import run_launch_request

    cfg = _make_config(remote_hosts=[RemoteHost("peer", "edge", "", "uxon")])
    expected = SessionIdentity(
        "alice",
        "uxon-job@claude",
        "$7",
        "123",
        "a" * 32,
        "/run/uxon/alice.sock",
        "remote-backend",
        "f" * 64,
    )
    with (
        mock.patch("uxon.infra.audit.audit") as audit,
        mock.patch("uxon.infra.audit.set_correlation_id") as global_correlation,
        mock.patch("uxon.app.session_handoff._run_ssh_client", return_value=(255, "")) as transport,
    ):
        if producer == "cli":
            rc = attach_app.do_attach(
                ParsedArgs(
                    action="attach",
                    host="peer",
                    user="alice",
                    target_id=expected.name,
                    expected_session=expected,
                ),
                cfg,
                "bob",
            )
        else:
            request = tui_bridge.TuiBridge(cfg, "bob", "bob", str(tmp_path)).on_remote_attach(
                "peer",
                "alice",
                expected.name,
                expected,
            )
            rc = run_launch_request(request).rc
    assert rc == 255 and "-tt" in transport.call_args.args[0]
    global_correlation.assert_not_called()
    dispatch, ended = audit.call_args_list
    assert (dispatch.args, ended.args) == (("attach.remote.out.dispatch",), ("session.ended",))
    for event in (dispatch.kwargs, ended.kwargs):
        assert (
            event["session_id"] == expected.session_id
            and event["launch_nonce"] == expected.launch_nonce
        )
        assert event["target_user"] == "alice" and event["peer_name"] == "peer"
        assert event["execution_backend"] == expected.execution_backend
    assert ended.kwargs["correlation_id"] == dispatch.kwargs["correlation_id"]
    assert ended.kwargs["rc"] == 255 and ended.kwargs["outcome"] == "error"


def test_unknown_peer_cli_and_bridge_forbid_execution_and_report_not_found(tmp_path):
    from uxon.tui.context import CallbackError

    cfg = _make_config(remote_hosts=[RemoteHost("peer", "edge", "", "uxon")])
    with (
        mock.patch("uxon.infra.audit.audit") as audit,
        mock.patch("os.execvp") as execute,
        mock.patch("uxon.infra.execution.run_query") as query,
        mock.patch("sys.stderr", new_callable=io.StringIO) as error,
    ):
        with pytest.raises(SystemExit):
            attach_app.do_attach(
                ParsedArgs(action="attach", host="missing", user="alice", target_id="job"),
                cfg,
                "bob",
            )
        assert "configured: peer" in error.getvalue()
        with pytest.raises(CallbackError, match="unknown remote host: missing"):
            tui_bridge.TuiBridge(cfg, "bob", "bob", str(tmp_path)).on_remote_attach(
                "missing", "alice", "job"
            )
    execute.assert_not_called()
    query.assert_not_called()
    assert audit.call_count == 2
    assert all(
        call.kwargs["outcome"] == "not_found" and call.kwargs["peer_name"] == "missing"
        for call in audit.call_args_list
    )


@pytest.fixture
def _stub_execution_identity_probe_by_default():
    yield


@pytest.mark.parametrize(
    "target,allowed",
    [("bob", True), ("alice", True), ("alice", False)],
    ids=["same-user", "cross-user", "denied"],
)
def test_target_attach_real_resolver_execution_boundary_title_and_audit(
    tmp_path, monkeypatch, target, allowed
):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("UXON_DEMO_HOSTS", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SSH_CONNECTION", "192.0.2.1 1000 192.0.2.2 22")
    cfg = _make_config(
        tmux_socket_template=str(tmp_path / "{user}.sock"),
        tmux_manage_options=False,
        tmux_set_terminal_title=True,
    )
    name = "uxon-demo@claude_fast"
    socket_path = str(tmp_path / f"{target}.sock")
    profile = replace(cfg.launch.profiles["claude"], id="claude_fast", launch_user=target)
    resolved = ResolvedLaunchProfile(
        profile, cfg.agents["claude"], target, execution=execution.resolve_target(cfg, target)
    )
    pending = launch_records.pending_from_resolved(
        socket_path=socket_path,
        session_name=name,
        resolved=resolved,
        target_dir=str(tmp_path),
        nonce="a" * 32,
    )
    if allowed:
        launch_records.create_pending_record(pending)
        launch_records.finalize_pending_record(
            pending, launch_records.TmuxSessionMetadata("$1", "123", name, pending.launch_nonce)
        )
    attempts = []
    account = SimpleNamespace(pw_uid=1001, pw_gid=1001, pw_name="alice")

    def query(argv, *, timeout):
        assert argv[:6] == ["/usr/bin/sudo", "-n", "-H", "-u", "alice", "--"]
        assert argv[-2:] == ["-m", "uxon.infra.execution_probe"] and timeout > 0
        attempts.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0 if allowed else 1,
            json.dumps({"euid": 1001, "egid": 1001, "groups": [1001]}),
            "" if allowed else "denied",
        )

    output, error = io.StringIO(), io.StringIO()
    collected = []
    nonint_prefix = [] if target == "bob" else ["/usr/bin/sudo", "-n", "-H", "-u", "alice", "--"]

    def server(argv, *, timeout):
        assert allowed and argv == nonint_prefix + [
            sys.executable,
            "-m",
            "uxon.infra.tmux_server_probe",
            "--socket",
            socket_path,
        ]
        assert timeout == cfg.execution.backend_for_user(target).probe_timeout_seconds
        collected.append("server")
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"state": "running", "sessions": [name], "error": ""}), ""
        )

    def snapshot(argv, *, check=False, timeout):
        base = nonint_prefix + ["tmux", "-S", socket_path]
        session_format = "#{session_name}\t#{session_id}\t#{session_attached}\t#{session_windows}\t#{session_created}\t#{session_activity}\t#{E:UXON_LAUNCH_NONCE}\t#{E:UXON_RUNTIME_RESOURCE}\t#{E:UXON_RUNTIME_CGROUP}"
        pane_format = (
            "#{pane_active}\t#{pane_pid}\t#{pane_current_command}\t#{pane_current_path}\t#{window_active}\t#{pane_id}\t#{pane_dead}\t#{pane_dead_status}\t"
            + ACKNOWLEDGED_FORMAT
        )
        assert (
            allowed
            and check is False
            and timeout == cfg.execution.backend_for_user(target).probe_timeout_seconds
        )
        if argv == base + ["list-sessions", "-F", session_format]:
            text = f"{name}\t$1\t0\t1\t123\t456\t{pending.launch_nonce}\t\t"
            collected.append("sessions")
        else:
            assert argv == base + ["list-panes", "-s", "-t", "$1", "-F", pane_format]
            text = f"1\t999999\tclaude\t{tmp_path}\t1\t%1\t0\t\t"
            collected.append("panes")
        return subprocess.CompletedProcess(argv, 0, text, "")

    def usage(argv, **kwargs):
        assert (
            allowed
            and argv == nonint_prefix + ["ps", "-eo", "pid=,ppid=,rss=,%cpu="]
            and kwargs == {}
        )
        collected.append("usage")
        return subprocess.CompletedProcess(argv, 0, "", "")

    with (
        mock.patch("pwd.getpwnam", return_value=account),
        mock.patch("pwd.getpwuid", return_value=SimpleNamespace(pw_name="bob")),
        mock.patch("os.getgrouplist", return_value=[1001]),
        mock.patch("uxon.infra.execution.run_query", side_effect=query),
        mock.patch("uxon.infra.tmux.run_query", side_effect=server),
        mock.patch("uxon.infra.sessions_probe.run_cmd", side_effect=snapshot),
        mock.patch("uxon.infra.sessions_probe.run_query", side_effect=usage),
        mock.patch("uxon.infra.audit.audit") as audit,
        mock.patch("os.execvp") as execute,
        redirect_stdout(output),
        mock.patch("sys.stderr", error),
    ):
        rc = attach_app.do_attach(
            ParsedArgs(action="attach", target_id="demo@claude_fast", user=target, dry_run=True),
            cfg,
            "bob",
        )
    execute.assert_not_called()
    assert len(attempts) == (0 if target == "bob" else 1)
    assert rc == (0 if allowed else 1)
    assert audit.call_count == 1
    fields = audit.call_args.kwargs
    assert fields["target_user"] == target
    assert fields.get("outcome", "ok") == ("ok" if allowed else "denied")
    if not allowed:
        assert "execution_backend" not in fields
        assert "uxon-error: not-reachable" in error.getvalue() and output.getvalue() == ""
        assert fields["session"] == "demo@claude_fast" and collected == []
        return
    argv = shlex.split(
        next(line[5:] for line in output.getvalue().splitlines() if line.startswith("exec "))
    )
    prefix = [] if target == "bob" else ["/usr/bin/sudo", "-H", "-u", "alice", "--"]
    assert argv[: len(prefix) + 7] == prefix + [
        "tmux",
        "-S",
        str(tmp_path / f"{target}.sock"),
        "if-shell",
        "-F",
        "-t",
        "$1",
    ]
    assert pending.launch_nonce in argv[-3] and "#{session_created},123" in argv[-3]
    assert (
        argv[-2]
        == "set-option -t '$1:' set-titles-string '#{session_name}' ; set-option -t '$1:' set-titles on ; attach-session -t '$1'"
    )
    assert "operation refused" in argv[-1]
    assert (
        fields["session"] == "uxon-demo@claude_fast"
        and fields["agent"] == "claude"
        and fields["profile"] == "claude_fast"
        and fields["dry_run"] is True
        and fields["execution_backend"] == pending.execution_backend
    )
    assert collected == ["server", "sessions", "panes", "usage"]

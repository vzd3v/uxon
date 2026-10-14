"""Public kill gates, protected cleanup and peer argv round trips."""

import io
import json
import shlex
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from unittest import mock

import pytest
from harness.session_commands import SessionCommands, session_config
from helpers import make_config

from uxon.app import kill
from uxon.cli.parsing import parse_args
from uxon.domain.args import ParsedArgs
from uxon.domain.sudo import SudoCapability
from uxon.infra.remote_hosts import RemoteHost
from uxon.tui.bridge import TuiBridge

pytestmark = pytest.mark.usefixtures("real_execution", "real_host_probes")


def test_kill_parser_preserves_target_flags_and_rejects_incomplete_or_unknown_input(subtests):
    for verb in ("kill", "-k"):
        for tail, user, host, json_output, force in (
            ([], None, None, False, False),
            (["--user", "bob"], "bob", None, False, False),
            (["--host", "peer"], None, "peer", False, False),
            (["--user", "bob", "--host", "peer", "--json", "--force"], "bob", "peer", True, True),
        ):
            with subtests.test(verb=verb, tail=tail):
                args = parse_args([verb, "demo@claude", *tail])
                assert (
                    args.action,
                    args.target_id,
                    args.user,
                    args.host,
                    args.json_output,
                    args.force,
                ) == ("kill", "demo@claude", user, host, json_output, force)
        for tail in (
            [],
            ["demo@claude", "--bogus"],
            ["demo@claude", "--user"],
            ["demo@claude", "--host"],
        ):
            with subtests.test(verb=verb, invalid=tail), pytest.raises(SystemExit) as error:
                parse_args([verb, *tail])
            assert error.value.code == 2


def test_local_kill_reachability_confirmation_and_dry_run_precede_mutation(
    tmp_path, monkeypatch, subtests
):
    target = tmp_path / "repo"
    target.mkdir()
    for index, (user, mode) in enumerate(
        (
            ("alice", "self"),
            ("bob", "reachable"),
            ("bob", "dry"),
            ("bob", "denied"),
            ("bob", "denied-dry"),
            ("bob", "json-confirm"),
            ("bob", "cancel"),
        )
    ):
        with subtests.test(user=user, mode=mode):
            monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / f"state-{index}"))
            monkeypatch.setenv("SSH_CONNECTION", "192.0.2.1 1 192.0.2.2 2")
            cfg = session_config(target)
            external = SessionCommands()
            with (
                external.installed_boundary(),
                mock.patch("uxon.infra.audit.audit") as audit,
                redirect_stdout(io.StringIO()) as output,
                redirect_stderr(io.StringIO()) as errors,
            ):
                row = external.add(cfg, target, user=user)
                if mode.startswith("denied"):
                    external.denied.add("bob")
                args = ParsedArgs(
                    action="kill",
                    target_id=row.name,
                    user=user,
                    force=mode not in {"json-confirm", "cancel"},
                    dry_run=mode in {"dry", "denied-dry"},
                    json_output=mode != "cancel",
                )
                before = len(external.calls)
                if mode in {"json-confirm", "cancel"}:
                    with (
                        mock.patch("uxon.infra.identity.is_interactive_tty", return_value=True),
                        mock.patch("builtins.input", return_value="no"),
                        pytest.raises(SystemExit) as error,
                    ):
                        kill.do_kill(args, cfg, "alice")
                    assert error.value.code == (130 if mode == "cancel" else 2)
                    assert (
                        "cancelled" in error.value.uxon_msg
                        if mode == "cancel"
                        else "requires --force" in error.value.uxon_msg
                    )
                else:
                    assert kill.do_kill(args, cfg, "alice") == int(mode.startswith("denied"))
                probes = [
                    cmd
                    for _, cmd, _ in external.calls[before:]
                    if cmd[-2:] == ["-m", "uxon.infra.execution_probe"]
                ]
                assert len(probes) == int(user != "alice")
                if mode.startswith("denied"):
                    assert "uxon-error: not-reachable" in errors.getvalue()
                    assert len(external.calls) == before + 1
                success = mode in {"self", "reachable"}
                assert external.mutations == ([(user, row.name)] if success else [])
                assert external.stops == ([row.name] if success else [])
                assert row.path.exists() != success and row.visible != success
                terminal = [
                    call.kwargs for call in audit.call_args_list if call.args == ("session.kill",)
                ]
                assert len(terminal) == int(mode not in {"json-confirm", "cancel"})
                if terminal:
                    assert terminal[0].get("outcome", "ok") == (
                        "denied" if mode.startswith("denied") else "ok"
                    )
                    assert terminal[0]["target_user"] == user
                if mode in {"self", "reachable", "dry"}:
                    data = json.loads(output.getvalue())["data"]
                    assert (
                        data["action"] == ("killed" if success else "would-kill")
                        and data["dry_run"] != success
                    )
                    if user == "bob":
                        assert data["target_user"] == "bob" and data["reachable"]


def test_cli_and_tui_single_kill_have_one_truthful_terminal_event_and_real_record_cleanup(
    tmp_path, monkeypatch, subtests
):
    target = tmp_path / "repo"
    target.mkdir()
    for caller in ("cli", "tui"):
        for fault in ("", "tmux", "stop", "record"):
            with subtests.test(caller=caller, fault=fault):
                monkeypatch.setenv(
                    "XDG_STATE_HOME", str(tmp_path / f"state-{caller}-{fault or 'ok'}")
                )
                cfg = session_config(target)
                external = SessionCommands()
                with (
                    external.installed_boundary(),
                    mock.patch("uxon.infra.audit.audit") as audit,
                    redirect_stdout(io.StringIO()),
                ):
                    row = external.add(cfg, target, failure=fault)
                    saved = row.path.read_bytes()

                    def invoke(*, caller=caller, cfg=cfg, row=row):
                        if caller == "cli":
                            return kill.do_kill(
                                ParsedArgs(
                                    action="kill", target_id=row.name, force=True, json_output=True
                                ),
                                cfg,
                                "alice",
                            )
                        return TuiBridge(cfg, "operator", "alice", str(target)).on_kill(
                            "alice", row.name
                        )

                    if fault:
                        with pytest.raises(SystemExit) as error:
                            invoke()
                        assert error.value.code == 1
                        assert error.value.uxon_msg == (
                            "kill-session failed (rc=9)"
                            if fault == "tmux"
                            else "tmux session was killed, but post-kill cleanup failed"
                        )
                    else:
                        assert invoke() == (0 if caller == "cli" else None)
                assert external.mutations == [("alice", row.name)]
                assert external.stops == ([] if fault == "tmux" else [row.name])
                assert row.visible == (fault == "tmux") and row.path.exists() == bool(fault)
                if fault:
                    assert row.path.read_bytes() == saved
                terminal = [
                    call.kwargs for call in audit.call_args_list if call.args == ("session.kill",)
                ]
                assert len(terminal) == 1
                event = terminal[0]
                assert event["outcome"] == ("error" if fault else "ok")
                assert (
                    event["session"],
                    event["target_user"],
                    event["profile"],
                    event["agent"],
                ) == (row.name, "alice", "boxed", "claude")
                assert event.get("rc") == (9 if fault == "tmux" else None)
                assert event.get("error") == (
                    "post-kill cleanup failed" if fault in {"stop", "record"} else None
                )
                stops = [
                    call.kwargs
                    for call in audit.call_args_list
                    if call.args == ("runtime.session_stop",)
                ]
                assert [(event["outcome"], event.get("error")) for event in stops] == (
                    []
                    if fault == "tmux"
                    else [("error", "stop refused")]
                    if fault == "stop"
                    else [("ok", None)]
                )


def test_public_kills_preserve_authority_for_each_preparation_failure_and_ignore_direct_markers(
    tmp_path, monkeypatch, subtests
):
    target = tmp_path / "repo"
    target.mkdir()
    cases = {
        "missing-runtime": "missing_runtime",
        "missing-profile": "missing_profile",
        "fingerprint": "fingerprint_mismatch",
        "saved-id": "identity_unresolved",
        "saved-epoch": "identity_unresolved",
        "unsafe-resource": "missing_resource",
        "bad-template": "invalid_stop_config",
        "no-stop": None,
        "direct": None,
        "matching-stop": None,
    }
    for caller in ("cli", "tui", "cli-bulk", "tui-bulk"):
        for label, reason in cases.items():
            with subtests.test(caller=caller, case=label):
                monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / f"{caller}-{label}"))
                cfg = session_config(target)
                external = SessionCommands(
                    resource_names=("-unsafe",) if label == "unsafe-resource" else ("box-repo",)
                )
                with (
                    external.installed_boundary(),
                    mock.patch("uxon.infra.audit.audit") as audit,
                    redirect_stdout(io.StringIO()) as output,
                ):
                    row = external.add(
                        cfg, target, profile="fast" if label == "direct" else "boxed"
                    )
                    payload = json.loads(row.path.read_text())
                    if label in {"missing-runtime", "saved-id", "saved-epoch", "unsafe-resource"}:
                        field, value = {
                            "missing-runtime": ("runtime", ""),
                            "saved-id": ("runtime_id", ""),
                            "saved-epoch": ("runtime_epoch", ""),
                            "unsafe-resource": ("runtime_resource", "-unsafe"),
                        }[label]
                        payload[field] = value
                    elif label == "missing-profile":
                        cfg.runtimes.clear()
                    elif label == "fingerprint":
                        cfg.runtimes["box"] = replace(cfg.runtimes["box"], id="changed")
                    elif label in {"no-stop", "bad-template"}:
                        cfg.runtimes["box"] = replace(
                            cfg.runtimes["box"],
                            stop_command=()
                            if label == "no-stop"
                            else ("engine", "stop", "{unknown}"),
                        )
                        payload["runtime_fingerprint"] = cfg.runtimes["box"].fingerprint
                    row.path.write_text(json.dumps(payload))
                    saved = row.path.read_bytes()
                    bridge = TuiBridge(cfg, "operator", "alice", str(target))

                    def invoke(*, caller=caller, cfg=cfg, row=row, bridge=bridge):
                        if caller == "cli":
                            return kill.do_kill(
                                ParsedArgs(
                                    action="kill", target_id=row.name, force=True, json_output=True
                                ),
                                cfg,
                                "alice",
                            )
                        if caller == "tui":
                            return bridge.on_kill("alice", row.name)
                        if caller == "cli-bulk":
                            return kill.do_kill_all(
                                ParsedArgs(action="kill-all", force=True, json_output=True),
                                cfg,
                                "alice",
                            )
                        return bridge.on_kill_all()

                    if reason and caller in {"cli", "tui"}:
                        with pytest.raises(SystemExit) as error:
                            invoke()
                        assert error.value.code == 1
                        assert (
                            error.value.uxon_msg
                            == "tmux session was killed, but post-kill cleanup failed"
                        )
                    else:
                        assert invoke() == (0 if caller.startswith("cli") else None)
                assert external.mutations == [("alice", row.name)] and not row.visible
                assert external.stops == ([row.name] if label == "matching-stop" else [])
                assert row.path.exists() == bool(reason)
                if reason:
                    assert row.path.read_bytes() == saved
                terminal_name = "session.kill_all" if caller.endswith("bulk") else "session.kill"
                terminal = [c.kwargs for c in audit.call_args_list if c.args == (terminal_name,)]
                assert len(terminal) == 1 and terminal[0]["outcome"] == (
                    "error" if reason else "ok"
                )
                if caller.endswith("bulk"):
                    assert terminal[0] == {
                        "outcome": "error" if reason else "ok",
                        "target_users": ["alice"],
                        "attempted_count": 1,
                        "killed_count": 1,
                        "failed_count": 0,
                        "cleanup_failed_count": int(bool(reason)),
                        "dry_run": False,
                    }
                    if caller == "cli-bulk":
                        assert json.loads(output.getvalue())["data"]["sessions"] == [
                            {
                                "name": row.name,
                                "action": "killed",
                                "cleanup_outcome": "error" if reason else "ok",
                            }
                        ]
                else:
                    assert terminal[0]["session"] == row.name
                    assert terminal[0].get("error") == (
                        "post-kill cleanup failed" if reason else None
                    )
                stops = [
                    c.kwargs for c in audit.call_args_list if c.args == ("runtime.session_stop",)
                ]
                assert [(e["outcome"], e.get("reason")) for e in stops] == (
                    [("skipped", reason)]
                    if reason
                    else [("ok", None)]
                    if label == "matching-stop"
                    else []
                )


def test_tui_bulk_kills_continue_after_failures_and_limit_reachable_users(
    tmp_path, monkeypatch, subtests
):
    target = tmp_path / "repo"
    target.mkdir()
    for reachable in (False, True):
        with subtests.test(reachable=reachable):
            monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / f"state-{reachable}"))
            cfg = session_config(target)
            external = SessionCommands()
            with external.installed_boundary(), mock.patch("uxon.infra.audit.audit") as audit:
                rows = [
                    external.add(cfg, target, stem=f"row-{i}", failure=fault)
                    for i, fault in enumerate(("tmux", "stop", "record", ""))
                ]
                peer = external.add(cfg, target, user="bob", stem="peer")
                hidden = external.add(cfg, target, user="carol", stem="hidden")
                bridge = TuiBridge(cfg, "operator", "alice", str(target))
                bridge.sudo_caps = SudoCapability(reachable_users=frozenset({"bob"}))
                if reachable:
                    bridge.on_kill_all_reachable()
                else:
                    bridge.on_kill_all()
            count = 4 + int(reachable)
            assert external.mutations == [("alice", r.name) for r in rows] + (
                [("bob", peer.name)] if reachable else []
            )
            assert external.stops == [r.name for r in rows[1:]] + ([peer.name] if reachable else [])
            assert [r.path.exists() for r in rows] == [True, True, True, False]
            assert peer.path.exists() != reachable and peer.visible != reachable
            assert hidden.path.exists() and hidden.visible
            terminal = [
                call.kwargs for call in audit.call_args_list if call.args == ("session.kill_all",)
            ]
            assert terminal == [
                {
                    "outcome": "error",
                    "target_users": ["alice", "bob"] if reachable else ["alice"],
                    "attempted_count": count,
                    "killed_count": count - 1,
                    "failed_count": 1,
                    "cleanup_failed_count": 2,
                    "dry_run": False,
                }
            ]


def test_bulk_empty_dry_run_and_cancel_never_mutate(tmp_path, monkeypatch, subtests):
    target = tmp_path / "repo"
    target.mkdir()
    for mode in ("empty", "dry", "json-confirm", "cancel", "non-tty"):
        with subtests.test(mode=mode):
            monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / f"state-{mode}"))
            cfg = session_config(target)
            external = SessionCommands()
            with (
                external.installed_boundary(),
                mock.patch("uxon.infra.audit.audit") as audit,
                redirect_stdout(io.StringIO()) as output,
            ):
                row = None if mode == "empty" else external.add(cfg, target)
                args = ParsedArgs(
                    action="kill-all",
                    dry_run=mode == "dry",
                    json_output=mode in {"empty", "dry", "json-confirm"},
                )
                if mode in {"json-confirm", "cancel", "non-tty"}:
                    with (
                        mock.patch(
                            "uxon.infra.identity.is_interactive_tty", return_value=mode != "non-tty"
                        ),
                        mock.patch("builtins.input", return_value="no"),
                        pytest.raises(SystemExit) as error,
                    ):
                        kill.do_kill_all(args, cfg, "alice")
                    assert error.value.code == (130 if mode == "cancel" else 2)
                else:
                    assert kill.do_kill_all(args, cfg, "alice") == 0
                    results = json.loads(output.getvalue())["data"]["sessions"]
                    assert results == (
                        []
                        if row is None
                        else [
                            {"name": row.name, "action": "would-kill", "cleanup_outcome": "not_run"}
                        ]
                    )
                    terminal = [
                        c.kwargs for c in audit.call_args_list if c.args == ("session.kill_all",)
                    ]
                    assert (
                        len(terminal) == 1
                        and terminal[0]["attempted_count"] == 0
                        and terminal[0]["killed_count"] == 0
                    )
                assert not external.mutations and not external.stops
                if row:
                    assert row.visible and row.path.exists()


def test_remote_kill_argv_round_trips_into_peer_parser_and_reports_transport_outcomes(
    tmp_path, monkeypatch, subtests
):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    for template in ((), ("ssh", "-J", "bastion", "{ssh_alias}", "{remote_command}")):
        for mode in ("dry-json", "dry-human", "ok", "peer-fail", "timeout", "missing"):
            with subtests.test(template=template, mode=mode):
                cfg = make_config(
                    remote_hosts=[
                        RemoteHost(
                            name="peer",
                            ssh_alias="peer-ssh",
                            description="",
                            remote_uxon="/opt/tools/uxon cli",
                            command_template=template,
                        )
                    ]
                )
                args = ParsedArgs(
                    action="kill",
                    target_id="space ; ' @claude",
                    host="peer",
                    user="bob",
                    force=True,
                    dry_run=mode.startswith("dry"),
                    json_output=mode == "dry-json",
                )
                calls = []

                def respond(
                    argv, *, args=args, calls=calls, mode=mode, template=template, **kwargs
                ):
                    calls.append(list(argv))
                    assert (
                        argv[0] == "ssh"
                        and kwargs["start_new_session"]
                        and kwargs["stdin"] == subprocess.DEVNULL
                    )
                    if "-O" in argv:
                        assert not template and mode == "timeout" and kwargs["timeout"] == 2
                        assert argv[-1] == "peer-ssh" and argv[argv.index("-O") + 1] == "exit"
                        return subprocess.CompletedProcess(argv, 0, "", "")
                    if "-G" in argv:
                        assert not template and mode == "timeout" and kwargs["timeout"] == 2
                        return subprocess.CompletedProcess(argv, 1, "", "")
                    assert kwargs["timeout"] == 10
                    assert argv[-2] == "peer-ssh"
                    parts = shlex.split(argv[-1])
                    assert parts[0] == "/opt/tools/uxon cli"
                    peer = parse_args(parts[1:])
                    assert (peer.action, peer.target_id, peer.user, peer.force, peer.host) == (
                        "kill",
                        args.target_id,
                        "bob",
                        True,
                        None,
                    )
                    assert peer.audit_correlation_id
                    if template:
                        assert argv[1:3] == ["-J", "bastion"]
                    else:
                        assert (
                            "BatchMode=yes" in argv
                            and "ServerAliveInterval=15" in argv
                            and "ControlMaster=auto" in argv
                        )
                    if mode == "timeout":
                        raise subprocess.TimeoutExpired(argv, 10)
                    if mode == "missing":
                        raise FileNotFoundError("ssh")
                    return subprocess.CompletedProcess(
                        argv,
                        1 if mode == "peer-fail" else 0,
                        "peer stdout\n",
                        "uxon-error: not-reachable\n" if mode == "peer-fail" else "",
                    )

                with (
                    mock.patch("uxon.infra.run.subprocess.run", side_effect=respond),
                    mock.patch("uxon.infra.audit.audit") as audit,
                    redirect_stdout(io.StringIO()) as output,
                    redirect_stderr(io.StringIO()) as errors,
                ):
                    assert kill.do_kill(args, cfg, "alice") == int(
                        mode in {"peer-fail", "timeout", "missing"}
                    )
                terminal = [
                    c.kwargs for c in audit.call_args_list if c.args == ("kill.remote.out",)
                ]
                assert len(terminal) == 1 and terminal[0]["target_user"] == "bob"
                assert terminal[0].get("outcome", "ok") == (
                    "error" if mode in {"peer-fail", "timeout", "missing"} else "ok"
                )
                assert terminal[0].get("rc") == (
                    {"ok": 0, "peer-fail": 1, "timeout": 124, "missing": 127}.get(mode)
                )
                if args.dry_run:
                    assert not calls
                    if args.json_output:
                        env = json.loads(output.getvalue())
                        assert (
                            env["host"] == "peer"
                            and env["data"]["action"] == "would-kill"
                            and env["data"]["target_user"] == "bob"
                        )
                        argv = env["data"]["ssh_argv"]
                    else:
                        assert output.getvalue().startswith("dry-run: ")
                        argv = shlex.split(output.getvalue()[9:])
                    assert parse_args(shlex.split(argv[-1])[1:]).target_id == args.target_id
                else:
                    assert len(calls) == (3 if mode == "timeout" and not template else 1)
                    if mode in {"ok", "peer-fail"}:
                        assert output.getvalue() == "peer stdout\n"
                    if mode == "peer-fail":
                        assert errors.getvalue() == "uxon-error: not-reachable\n"
                    if mode == "timeout":
                        assert "ssh timeout" in errors.getvalue()
                    if mode == "missing":
                        assert "ssh not installed" in errors.getvalue()


def test_remote_unknown_peer_and_unconfirmed_json_fail_before_transport(subtests):
    for mode in ("empty", "unknown", "unconfirmed"):
        with subtests.test(mode=mode):
            cfg = make_config(
                remote_hosts=[]
                if mode == "empty"
                else [
                    RemoteHost(
                        name="peer", ssh_alias="peer-ssh", description="", remote_uxon="uxon"
                    )
                ]
            )
            args = ParsedArgs(
                action="kill",
                target_id="demo@claude",
                host="unknown" if mode == "unknown" else "peer",
                json_output=True,
            )
            with (
                mock.patch("uxon.infra.run.subprocess.run") as spawn,
                mock.patch("uxon.infra.audit.audit") as audit,
                pytest.raises(SystemExit) as error,
            ):
                kill.do_kill(args, cfg, "alice")
            assert error.value.code == 2
            assert (
                "configured: peer"
                if mode == "unknown"
                else "no [[remote_hosts]]"
                if mode == "empty"
                else "requires --force"
            ) in error.value.uxon_msg
            spawn.assert_not_called()
            terminal = [c.kwargs for c in audit.call_args_list if c.args == ("kill.remote.out",)]
            assert [e["outcome"] for e in terminal] == (
                [] if mode == "unconfirmed" else ["not_found"]
            )

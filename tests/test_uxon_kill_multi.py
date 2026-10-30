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


def _expected(cfg, row):
    from uxon.domain.session import SessionIdentity
    from uxon.infra.tmux import tmux_socket_path

    backend = cfg.execution.backend_for_user(row.user)
    return SessionIdentity(
        row.user,
        row.name,
        row.session_id,
        "100",
        row.nonce,
        tmux_socket_path(cfg, row.user),
        backend.id,
        backend.fingerprint,
    )


def test_cli_and_tui_single_kill_have_one_truthful_terminal_event_and_real_record_cleanup(
    tmp_path, monkeypatch, subtests
):
    target = tmp_path / "repo"
    target.mkdir()
    for caller in ("cli", "tui"):
        for fault in ("", "tmux", "stop", "record"):
            with subtests.test(caller=caller, fault=fault):
                monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / f"{caller}-{fault or 'ok'}"))
                cfg = session_config(target)
                external = SessionCommands()
                with (
                    external.installed_boundary(),
                    mock.patch("uxon.infra.audit.audit") as audit,
                    redirect_stdout(io.StringIO()),
                ):
                    row = external.add(cfg, target, failure=fault)
                    saved = row.path.read_bytes()
                    if caller == "cli":
                        status = kill.do_kill(
                            ParsedArgs(action="kill", target_id=row.name, force=True), cfg, "alice"
                        )
                        assert status == int(bool(fault))
                    else:
                        result = TuiBridge(cfg, "operator", "alice", str(target)).on_kill(
                            _expected(cfg, row)
                        )
                        assert result.ok == (not fault)
                        assert result.identity.session_id == row.session_id
                        assert bool(result.error) == bool(fault)
                        if fault == "tmux":
                            assert "rc=9" in result.error and "tmux refused" in result.error
                        if fault == "stop":
                            assert "rc=7" in result.error and "stop refused" in result.error
                assert external.mutations == [("alice", row.name)]
                assert external.stops == ([] if fault in {"tmux", "record"} else [row.name])
                assert row.visible == (fault == "tmux") and row.path.exists() == bool(fault)
                if fault:
                    assert row.path.read_bytes() == saved
                terminal = [c.kwargs for c in audit.call_args_list if c.args == ("session.kill",)]
                assert len(terminal) == 1
                event = terminal[0]
                assert event["outcome"] == ("error" if fault else "ok")
                assert (
                    event["session"],
                    event["session_id"],
                    event["launch_nonce"],
                    event["target_user"],
                ) == (row.name, row.session_id, row.nonce, "alice")
                assert bool(event.get("error")) == bool(fault)
                stops = [
                    c.kwargs for c in audit.call_args_list if c.args == ("runtime.session_stop",)
                ]
                assert len(stops) == int(fault not in {"tmux", "record"})
                if stops:
                    assert stops[0]["session_id"] == row.session_id
                    assert (
                        stops[0]["execution_backend"] == cfg.execution.backend_for_user("alice").id
                    )
                    assert stops[0]["outcome"] == ("error" if fault == "stop" else "ok")


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
                    redirect_stdout(io.StringIO()),
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
                    if caller == "cli":
                        assert kill.do_kill(
                            ParsedArgs(action="kill", target_id=row.name, force=True), cfg, "alice"
                        ) == int(bool(reason))
                    elif caller == "cli-bulk":
                        assert kill.do_kill_all(
                            ParsedArgs(action="kill-all", force=True), cfg, "alice"
                        ) == int(bool(reason))
                    else:
                        result = (
                            bridge.on_kill(_expected(cfg, row))
                            if caller == "tui"
                            else bridge.on_kill_all((_expected(cfg, row),))
                        )
                        assert result.ok == (not reason)
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
                assert all(
                    event["execution_backend"] == cfg.execution.backend_for_user("alice").id
                    for event in stops
                )


def test_tui_bulk_kills_continue_after_failures_and_limit_reachable_users(
    tmp_path, monkeypatch, subtests
):
    target = tmp_path / "repo"
    target.mkdir()
    for reachable in (False, True):
        with subtests.test(reachable=reachable):
            monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / f"state-{reachable}"))
            cfg, external = session_config(target), SessionCommands()
            with external.installed_boundary(), mock.patch("uxon.infra.audit.audit") as audit:
                rows = [
                    external.add(cfg, target, stem=f"row-{i}", failure=fault)
                    for i, fault in enumerate(("tmux", "stop", "record", ""))
                ]
                peer = external.add(cfg, target, user="bob", stem="peer")
                hidden = external.add(cfg, target, user="carol", stem="hidden")
                bridge = TuiBridge(cfg, "operator", "alice", str(target))
                bridge.sudo_caps = SudoCapability(reachable_users=frozenset({"bob"}))
                targets = tuple(
                    _expected(cfg, row) for row in [*rows, *([peer] if reachable else [])]
                )
                # A new session appearing after confirmation is outside the snapshot.
                new = external.add(cfg, target, stem="after-confirmation")
                result = (
                    bridge.on_kill_all_reachable(targets)
                    if reachable
                    else bridge.on_kill_all(targets)
                )
            assert (
                result.killed_count == 3 + int(reachable)
                and result.failed_count == 3
                and not result.ok
            )
            assert "rc=9" in result.results[0].error and "stop refused" in result.results[1].error
            assert [r.path.exists() for r in rows] == [True, True, True, False]
            assert peer.visible != reachable and hidden.visible and new.visible
            terminal = [c.kwargs for c in audit.call_args_list if c.args == ("session.kill_all",)]
            assert len(terminal) == 1
            assert (
                terminal[0]["attempted_count"],
                terminal[0]["killed_count"],
                terminal[0]["failed_count"],
                terminal[0]["cleanup_failed_count"],
            ) == (len(targets), len(targets) - 1, 1, 2)
            assert len(terminal[0]["sessions"]) == len(targets)


def test_bulk_empty_dry_run_and_cancel_never_mutate(tmp_path, monkeypatch, subtests):
    target = tmp_path / "repo"
    target.mkdir()
    for mode in ("empty", "dry", "json-confirm", "cancel", "non-tty"):
        with subtests.test(mode=mode):
            monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / f"state-{mode}"))
            cfg, external = session_config(target), SessionCommands()
            with (
                external.installed_boundary(),
                mock.patch("uxon.infra.audit.audit"),
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
                    assert len(results) == int(row is not None)
                    if row:
                        assert (
                            results[0]["action"] == "would-kill"
                            and results[0]["session_id"] == row.session_id
                        )
                assert not external.mutations and not external.stops


def test_remote_kill_argv_round_trips_into_peer_parser_and_reports_transport_outcomes(
    tmp_path, monkeypatch, subtests
):
    from uxon.domain.session import SessionIdentity
    from uxon.domain.wire_schema import make_envelope

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    expected = SessionIdentity(
        "bob",
        "space ; ' @claude",
        "$3",
        "100",
        "n" * 32,
        "/run/uxon/bob.sock",
        "peer-backend",
        "f" * 64,
    )
    for template in ((), ("ssh", "-J", "bastion", "{ssh_alias}", "{remote_command}")):
        for mode in (
            "dry",
            "ok",
            "peer-fail",
            "timeout",
            "missing",
            "malformed",
            "missing-error",
            "contradictory-error",
        ):
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
                    target_id=expected.name,
                    host="peer",
                    user="bob",
                    expected_session=expected,
                    force=True,
                    dry_run=mode == "dry",
                    json_output=True,
                )
                calls = []

                def respond(argv, *, kwargs_mode=mode, calls=calls, **kwargs):
                    calls.append(list(argv))
                    if "-O" in argv or "-G" in argv:
                        return subprocess.CompletedProcess(argv, 1, "", "")
                    assert kwargs["timeout"] == 10
                    parts = shlex.split(argv[-1])
                    peer = parse_args(parts[1:])
                    assert peer.expected_session == expected and peer.force and peer.json_output
                    if kwargs_mode == "timeout":
                        raise subprocess.TimeoutExpired(argv, 10)
                    if kwargs_mode == "missing":
                        raise FileNotFoundError("ssh")
                    data = {
                        "name": expected.name,
                        "target_user": "bob",
                        "session_id": expected.session_id,
                        "launch_nonce": expected.launch_nonce,
                        "action": "failed" if kwargs_mode == "peer-fail" else "killed",
                        "cleanup_outcome": "not_run" if kwargs_mode == "peer-fail" else "ok",
                        "error": "denied on peer" if kwargs_mode == "peer-fail" else "",
                    }
                    if kwargs_mode == "missing-error":
                        data.update(action="failed", error="")
                    elif kwargs_mode == "contradictory-error":
                        data["error"] = "cleanup failed despite reported success"
                    payload = (
                        "[]"
                        if kwargs_mode == "malformed"
                        else json.dumps(make_envelope("kill", data, uxon_version="4.0.0"))
                    )
                    return subprocess.CompletedProcess(
                        argv, int(kwargs_mode == "peer-fail"), payload, ""
                    )

                with (
                    mock.patch("uxon.infra.run.subprocess.run", side_effect=respond),
                    mock.patch("uxon.infra.audit.audit") as audit,
                    redirect_stdout(io.StringIO()) as output,
                    redirect_stderr(io.StringIO()),
                ):
                    assert kill.do_kill(args, cfg, "alice") == int(mode not in {"dry", "ok"})
                terminal = [
                    c.kwargs for c in audit.call_args_list if c.args == ("kill.remote.out",)
                ]
                assert (
                    len(terminal) == 1
                    and terminal[0]["session_id"] == expected.session_id
                    and terminal[0]["target_session"] == expected.name
                    and terminal[0]["execution_backend"] == expected.execution_backend
                )
                assert terminal[0]["outcome"] == ("ok" if mode in {"dry", "ok"} else "error")
                assert json.loads(output.getvalue())["data"]["session_id"] == expected.session_id
                if mode == "dry":
                    assert not calls


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
            with mock.patch("uxon.infra.run.subprocess.run") as spawn, pytest.raises(SystemExit):
                kill.do_kill(args, cfg, "alice")
            spawn.assert_not_called()

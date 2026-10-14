"""CLI interactive handoff through external commands and protected records."""

import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest
from harness.session_commands import SessionCommands, session_config
from harness.target_commands import TargetCommands

from uxon.app import attach, session_handoff
from uxon.app.launch_profile import resolve_launch_profile
from uxon.domain.args import ParsedArgs
from uxon.domain.launch_request import LaunchRequest, TmuxHandoff
from uxon.infra import launch_records, sessions_probe, tmux, tmux_socket

pytestmark = pytest.mark.usefixtures("real_execution", "real_host_probes")


def test_cli_managed_launch_finalizes_and_releases_before_waiting_for_interactive_client(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = tmp_path / "repo"
    target.mkdir()
    cfg = session_config(target, tmux_socket_template=str(tmp_path / "socket" / "tmux.sock"))
    sequence = []
    metadata = None

    def respond(user, command, kwargs):
        nonlocal metadata
        assert user == "alice" and kwargs["timeout"] == 10
        if "uxon.infra.tmux_socket" in command:
            assert command[-3:] == ["--prepare", "--socket", cfg.tmux_socket_template]
            tmux_socket.prepare_socket_parent(Path(cfg.tmux_socket_template))
            sequence.append("socket")
        elif "new-session" in command:
            assert command[command.index("new-session") + 1 : command.index("new-session") + 4] == [
                "-d",
                "-s",
                "uxon-repo@fast",
            ]
            nonce = command[command.index("--nonce") + 1]
            metadata = launch_records.TmuxSessionMetadata("$1", "100", "uxon-repo@fast", nonce)
            [path] = list(launch_records.default_launch_record_dir().glob("*.json"))
            assert json.loads(path.read_text())["status"] == "pending"
            sequence.append("create")
        elif "display-message" in command:
            sequence.append("metadata")
            return subprocess.CompletedProcess(
                command,
                0,
                "\t".join(
                    (metadata.session_id, metadata.created, metadata.name, metadata.launch_nonce)
                )
                + "\nroot\n",
                "",
            )
        else:
            assert command[:4] == [
                "tmux",
                "-S",
                cfg.tmux_socket_template,
                "source-file",
            ] and command[4:] == [
                "-",
                ";",
                "wait-for",
                "-S",
                launch_records.handshake_channel(metadata.launch_nonce, "release"),
            ]
            assert "bind-key" in kwargs["input"]
            payload = launch_records.read_verified_record(cfg.tmux_socket_template, metadata)
            assert (
                payload is not None
                and payload["profile"] == "fast"
                and payload["agent"] == "claude"
                and payload["launch_user"] == "alice"
            )
            assert (
                payload["runtime_kind"] == "direct" and payload["execution_backend"] == "boundary"
            )
            sequence.append("release")
        return subprocess.CompletedProcess(command, 0, "", "")

    def interactive(argv):
        assert argv[:3] == ["boundary", "alice", "--"] and argv.count("boundary") == 1
        assert argv[-3:] == ["attach-session", "-t", "uxon-repo@fast"]
        assert sequence == ["socket", "create", "metadata", "release"]
        assert launch_records.read_verified_record(cfg.tmux_socket_template, metadata) is not None
        sequence.append("interactive")
        return 17

    with (
        TargetCommands(respond).installed_boundary(),
        mock.patch("uxon.app.session_handoff.subprocess.call", side_effect=interactive) as spawn,
    ):
        resolved = resolve_launch_profile(cfg, "alice", "fast", str(target), "normal")
        assert (
            session_handoff.launch_in_tmux(
                str(target),
                "uxon-repo@fast",
                ParsedArgs(action="run", agent_args=[]),
                cfg,
                None,
                resolved_profile=resolved,
            )
            == 17
        )
    spawn.assert_called_once()
    assert sequence[-1] == "interactive"


def test_attach_preparation_and_dismissal_failures_preserve_exact_handoff_result(
    tmp_path, monkeypatch, subtests
):
    monkeypatch.delenv("TMUX", raising=False)
    target = tmp_path / "repo"
    target.mkdir()
    for mode in ("ok", "query-fail", "install-fail", "system-exit", "unverified-dismissal"):
        with subtests.test(mode=mode):
            monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / f"state-{mode}"))
            cfg = session_config(target)
            external = SessionCommands()
            transcript = []
            original = external.respond

            def respond(
                user, command, kwargs, *, mode=mode, original=original, transcript=transcript
            ):
                transcript.append(command)
                if any("list-keys" in token for token in command):
                    assert kwargs["timeout"] == 10
                    if mode == "system-exit":
                        raise SystemExit(0)
                    return subprocess.CompletedProcess(
                        command,
                        8 if mode == "query-fail" else 0,
                        "root\n",
                        "binding query denied" if mode == "query-fail" else "",
                    )
                if "source-file" in command:
                    assert kwargs["timeout"] == 10 and kwargs["input"]
                    return subprocess.CompletedProcess(
                        command,
                        9 if mode == "install-fail" else 0,
                        "",
                        "binding install denied" if mode == "install-fail" else "",
                    )
                if mode == "unverified-dismissal" and "list-panes" in command:
                    return subprocess.CompletedProcess(
                        command, 0, "1\t111\tengine\t/mutable\t1\t%1\t1\t17\t111", ""
                    )
                return original(user, command, kwargs)

            external.respond = respond
            with (
                external.installed_boundary(),
                mock.patch(
                    "uxon.app.session_handoff.subprocess.call",
                    return_value=125 if mode == "unverified-dismissal" else 23,
                ) as spawn,
            ):
                row = external.add(
                    cfg, target, profile="fast", managed=mode != "unverified-dismissal"
                )
                if mode == "unverified-dismissal":
                    request = LaunchRequest(
                        tuple(
                            tmux.configured_tmux_base(cfg, "alice")
                            + ["attach-session", "-t", row.name]
                        ),
                        handoff=TmuxHandoff(cfg, "alice", row.name, False),
                    )
                    snapshot = sessions_probe.collect_session_snapshot_for_user(
                        cfg,
                        "alice",
                        cfg.session_prefix,
                        tmux.tmux_socket_path(cfg, "alice"),
                        for_dismissal=True,
                    )
                    assert len(snapshot.sessions) == 1 and snapshot.sessions[0].dismissed_panes == (
                        "%1",
                    ), transcript
                    assert not snapshot.sessions[0].launch_record_verified
                else:
                    [session] = sessions_probe.collect_sessions(["alice"], cfg)
                    request = tmux._build_tmux_attach_request(session, cfg, "alice")
                if mode == "system-exit":
                    with pytest.raises(SystemExit) as error:
                        session_handoff.run_launch_request(request)
                    assert error.value.code == 0
                elif mode == "ok":
                    assert attach.attach_session(session, cfg, "alice") == 23
                else:
                    result = session_handoff.run_launch_request(request)
                    expected = (
                        (2, "prepare", "could not read tmux bindings: binding query denied")
                        if mode == "query-fail"
                        else (
                            2,
                            "prepare",
                            "could not configure tmux diagnostics: binding install denied",
                        )
                        if mode == "install-fail"
                        else (
                            0,
                            "cmd",
                            "cannot close diagnostics: the launch identity could not be verified",
                        )
                    )
                    assert (
                        result.rc,
                        result.stage,
                        result.warning,
                    ) == expected and result.wall_seconds >= 0
                assert spawn.call_count == int(mode in {"ok", "unverified-dismissal"})
                if spawn.call_count:
                    assert spawn.call_args.args[0] == list(request.cmd)
                assert not external.mutations and not external.stops and row.visible
                if row.path:
                    assert row.path.exists()
            if mode in {"query-fail", "system-exit"}:
                assert not any("source-file" in command for command in transcript)

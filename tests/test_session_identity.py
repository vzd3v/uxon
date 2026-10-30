"""Captured session targets survive confirmation, rename and server reuse."""

import asyncio
import json
import os
import subprocess
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from unittest import mock

import pytest
from harness.session_commands import SessionCommands, session_config
from harness.textual_ready import wait_until
from helpers import land_dashboard_context, make_config, make_dashboard_context, make_session
from textual.widgets import Input

from uxon.app import kill
from uxon.domain.session import BulkKillResult, KillResult, SessionIdentity, to_tui_session
from uxon.infra import launch_bootstrap, launch_records, process, sessions_probe, tmux
from uxon.infra.identity import process_user
from uxon.tui.app import UxonApp
from uxon.tui.bridge import TuiBridge
from uxon.tui.screens.confirm import ConfirmYesNo
from uxon.tui.widgets.session_list_view import SessionListView

pytestmark = pytest.mark.usefixtures("real_execution", "real_host_probes")


def test_confirmation_keeps_original_identity_and_rename_keeps_record_authority(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = tmp_path / "repo"
    target.mkdir()
    cfg, external = session_config(target), SessionCommands()
    with external.installed_boundary(), mock.patch("uxon.infra.audit.audit") as audit:
        original = external.add(cfg, target)
        replacement = external.add(cfg, target, profile="fast", stem="replacement")
        replacement.name = original.name
        replacement.visible = False
        selected = sessions_probe.collect_sessions(["alice"], cfg)[0]
        captured = to_tui_session(selected, cfg.session_prefix)
        bridge = TuiBridge(cfg, "operator", "alice", str(target))
        ctx = make_dashboard_context(sessions=[captured], on_kill=bridge.on_kill)

        async def confirm():
            app = UxonApp(ctx, probe_agents=False)
            async with app.run_test(size=(120, 30)) as pilot:
                land_dashboard_context(app, ctx)
                table = app.screen.query_one("#sessions-dashboard", SessionListView)
                table.focus()
                table.move_cursor(row=0)
                await pilot.press("d")
                assert isinstance(app.screen, ConfirmYesNo)
                assert selected.tmux_session_id in app.screen.prompt
                original.name = "manually-renamed"
                replacement.visible = True
                app.kick_refresh = lambda **kwargs: None
                await pilot.press("y")
                await app.workers.wait_for_complete()
                await pilot.pause()

        asyncio.run(confirm())
        assert not original.visible and replacement.visible
        assert not original.path.exists() and replacement.path.exists()
        assert external.mutations == [("alice", "manually-renamed")]
        assert external.stops == ["manually-renamed"]
        [event] = [call.kwargs for call in audit.call_args_list if call.args == ("session.kill",)]
        assert event["session"] == "manually-renamed" and event["session_id"] == original.session_id
        assert event["outcome"] == "ok"


def test_unknown_absence_and_missing_controller_authority_never_stop_or_delete(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "controller-one"))
    target = tmp_path / "repo"
    target.mkdir()
    cfg, external = session_config(target), SessionCommands()
    with external.installed_boundary():
        original = external.add(cfg, target)
        expected = sessions_probe.collect_sessions(["alice"], cfg)[0].identity
        # A different private controller cannot silently acquire authority.
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "controller-two"))
        result = kill.kill_expected(cfg, expected)
        assert not result.ok and "authoritative launch record" in result.error
        assert original.visible and original.path.exists() and not external.mutations
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "controller-one"))
        # Mutable pane environment cannot prove that the original terminal is gone.
        original.nonce = "b" * 32
        result = kill.kill_expected(cfg, expected)
        assert not result.ok and "removal is unproven" in result.error
        assert original.visible and original.path.exists() and not external.stops
        assert not external.mutations


def test_two_captured_direct_cleanups_accept_only_completed_record_removal(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = tmp_path / "repo"
    target.mkdir()
    cfg, external = session_config(target), SessionCommands()
    with external.installed_boundary():
        original = external.add(cfg, target, profile="fast")
        first = sessions_probe.collect_sessions(["alice"], cfg)[0]
        second = sessions_probe.collect_sessions(["alice"], cfg)[0]
        assert first.launch_record_verified and second.launch_record_verified
        original.visible = False
        assert kill.finish_killed_session(cfg, first, None, target_user="alice").ok
        assert not original.path.exists()
        # The second client rereads authority after the first client removed it.
        assert kill.finish_killed_session(cfg, second, None, target_user="alice").ok
        assert not external.stops and not external.mutations
        # A fresh request without captured authority must still fail closed.
        result = kill.kill_expected(cfg, second.identity)
        assert not result.ok and "authoritative launch record" in result.error


@pytest.mark.parametrize(
    "failure",
    [
        "changed",
        "symlink",
        "unreadable",
        "unsafe_store",
        "missing_store",
        "command",
        "live",
        "unreachable",
    ],
)
def test_direct_cleanup_race_does_not_weaken_authority(tmp_path, monkeypatch, failure):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = tmp_path / "repo"
    target.mkdir()
    cfg, external = session_config(target), SessionCommands()
    with external.installed_boundary():
        original = external.add(cfg, target, profile="boxed" if failure == "command" else "fast")
        captured = sessions_probe.collect_sessions(["alice"], cfg)[0]
        assert captured.launch_record_verified
        original.visible = failure == "live"
        metadata = launch_records.TmuxSessionMetadata(
            captured.tmux_session_id,
            captured.tmux_session_created,
            captured.name,
            captured.launch_nonce,
        )
        if failure == "changed":
            payload = json.loads(original.path.read_text())
            payload["runtime_dir"] = "/different-workspace"
            original.path.write_text(json.dumps(payload))
        elif failure == "symlink":
            sibling = original.path.with_suffix(".saved")
            original.path.rename(sibling)
            original.path.symlink_to(sibling)
        elif failure == "unreadable":
            real_open = os.open

            def denied_open(path, *args, **kwargs):
                if Path(path) == original.path:
                    raise PermissionError("record read denied")
                return real_open(path, *args, **kwargs)

            monkeypatch.setattr(os, "open", denied_open)
        else:
            original.path.unlink()
            if failure == "unsafe_store":
                original.path.parent.chmod(0o777)
            elif failure == "missing_store":
                original.path.parent.rename(original.path.parent.with_name("saved-records"))
            elif failure == "unreachable":
                respond = external.respond

                def unreachable(user, command, kwargs):
                    if command[0] == "tmux" or "uxon.infra.tmux_server_probe" in command:
                        return subprocess.CompletedProcess(command, 1, "", "transport unavailable")
                    return respond(user, command, kwargs)

                external.respond = unreachable
        result = kill.finish_killed_session(cfg, captured, None, target_user="alice")
        assert not result.ok
        assert not external.stops and not external.mutations
        if failure == "live":
            assert "still exists" in result.error
        elif failure == "unreachable":
            assert "cannot prove terminal removal" in result.error
        else:
            assert "protected launch record" in result.error
        if failure in {"changed", "symlink", "unreadable"}:
            assert original.path.lstat()
        if failure in {"unsafe_store", "missing_store"}:
            assert not launch_records.record_is_absent(captured.socket_path, metadata)
            assert not launch_records.delete_verified_record(captured.socket_path, metadata)
            if failure == "unsafe_store":
                with pytest.raises(SystemExit) as error:
                    external.add(cfg, target, profile="fast", stem="another")
                assert "mode 0700" in error.value.uxon_msg
                assert original.path.parent.stat().st_mode & 0o777 == 0o777
            else:
                assert not original.path.parent.exists()


def test_bulk_confirmation_freezes_targets_and_surfaces_partial_error():
    from helpers import make_dashboard_session

    selected = [
        make_dashboard_session("uxon-one@claude"),
        make_dashboard_session("uxon-two@claude"),
    ]
    # A captured legacy identity is an individual refusal, not a reason to
    # silently omit it or prevent independent targets from being attempted.
    selected[1] = replace(selected[1], identity=replace(selected[1].identity, launch_nonce=""))
    captured, notifications = [], []

    def remove(targets):
        captured.extend(targets)
        return BulkKillResult(
            (
                KillResult(targets[0], "killed", "ok"),
                KillResult(targets[1], "failed", error="backend unavailable"),
            )
        )

    ctx = make_dashboard_context(sessions=selected, on_kill_all=remove)

    async def confirm():
        app = UxonApp(ctx, probe_agents=False)
        async with app.run_test(size=(120, 30)) as pilot:
            land_dashboard_context(app, ctx)
            await pilot.press("D")
            app.notify = lambda message, **kwargs: notifications.append((message, kwargs))
            land_dashboard_context(
                app, replace(ctx, sessions=[*selected, make_dashboard_session("uxon-new@claude")])
            )
            app.kick_refresh = lambda **kwargs: None
            app.screen.query_one(Input).value = "kill-all"
            await pilot.press("enter")
            await wait_until(pilot, lambda: bool(captured), "frozen bulk dispatch")
            await app.workers.wait_for_complete()
            await pilot.pause()

    asyncio.run(confirm())
    assert captured == [session.identity for session in selected]
    assert any(
        "1/2" in message and options["severity"] == "error" for message, options in notifications
    )
    assert any("backend unavailable" in message and "$" in message for message, _ in notifications)


@pytest.mark.slow
def test_native_queue_targets_original_id_and_refuses_reused_id(tmp_path, monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    user = process_user()
    # Keep the UNIX socket short even when pytest's workspace path is long.
    with tempfile.TemporaryDirectory(prefix="uxon-id-") as raw:
        socket = str(Path(raw) / "tmux.sock")
        cfg = make_config(tmux_socket_template=socket, tmux_manage_options=False)
        base = ["tmux", "-f", "/dev/null", "-S", socket]

        def command(*args):
            return subprocess.run(
                [*base, *args], capture_output=True, text=True, check=True, timeout=5
            ).stdout.strip()

        try:
            command("new-session", "-d", "-s", "uxon-original@claude", "sleep 60")
            session_id, created = command(
                "display-message",
                "-p",
                "-t",
                "uxon-original@claude",
                "#{session_id}\t#{session_created}",
            ).split("\t")
            nonce = "a" * 32
            command("set-environment", "-t", session_id, "UXON_LAUNCH_NONCE", nonce)
            backend = cfg.execution.backend_for_user(user)
            expected = SessionIdentity(
                user,
                "uxon-original@claude",
                session_id,
                created,
                nonce,
                socket,
                backend.id,
                backend.fingerprint,
            )
            delayed_attach = tmux._build_tmux_attach_request(
                replace(
                    make_session(expected.name, user=user),
                    tmux_session_id=session_id,
                    tmux_session_created=created,
                    launch_nonce=nonce,
                    socket_path=socket,
                    execution_backend=backend.id,
                    execution_fingerprint=backend.fingerprint,
                    launch_record_verified=True,
                ),
                cfg,
                user,
            )
            command("rename-session", "-t", session_id, "manual-title")
            command("new-session", "-d", "-s", expected.name, "sleep 60")
            tmux.prepare_diagnostics_attach(delayed_attach)
            assert command("show-options", "-t", session_id, "-v", "@uxon-diagnostics-table")
            assert "@uxon-diagnostics-table" not in command("show-options", "-t", expected.name)
            assert tmux.conditional_kill(cfg, expected) == (True, "")
            assert not tmux.exact_identity_exists(cfg, expected)
            assert command("list-sessions", "-F", "#{session_name}") == expected.name

            boot_name = "uxon-bootstrap@claude"
            command("new-session", "-d", "-s", boot_name, "sleep 60")
            boot_pane = command("display-message", "-p", "-t", boot_name, "#{pane_id}")
            command("set-environment", "-t", boot_name, "UXON_LAUNCH_NONCE", "d" * 32)
            command("rename-session", "-t", boot_name, "manual-bootstrap")
            command("new-session", "-d", "-s", boot_name, "sleep 60")
            command("set-environment", "-t", boot_name, "UXON_LAUNCH_NONCE", "e" * 32)
            with mock.patch.dict(os.environ, {"TMUX_PANE": boot_pane}):
                launch_bootstrap._kill_exact_session(socket, boot_name, "d" * 32)
            assert "manual-bootstrap" not in command("list-sessions", "-F", "#{session_name}")
            assert (
                command("display-message", "-p", "-t", boot_name, "#{E:UXON_LAUNCH_NONCE}")
                == "e" * 32
            )
            command("kill-session", "-t", boot_name)
            command("kill-server")
            deadline = time.monotonic() + 5
            while (
                tmux.probe_tmux_server(cfg, user, socket).state != "absent"
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            assert tmux.probe_tmux_server(cfg, user, socket).state == "absent", (
                "old tmux server is still reachable"
            )
            command("new-session", "-d", "-s", expected.name, "sleep 60")
            command("set-environment", "-t", session_id, "UXON_LAUNCH_NONCE", "b" * 32)
            before_options = command("show-options", "-t", session_id)
            assert not tmux.conditional_kill(cfg, expected)[0]
            assert command("list-sessions", "-F", "#{session_id}") == session_id
            refused = subprocess.run(delayed_attach.cmd, capture_output=True, text=True, timeout=5)
            assert (
                refused.returncode == 1 and "identity changed; operation refused" in refused.stdout
            )
            assert command("show-options", "-t", session_id) == before_options

            # Restart after capture, before rollback's native assertion executes.
            metadata = launch_records.TmuxSessionMetadata(
                session_id,
                command("display-message", "-p", "-t", session_id, "#{session_created}"),
                expected.name,
                "b" * 32,
            )
            pending = launch_records.PendingLaunchRecord(
                socket,
                expected.name,
                metadata.launch_nonce,
                "claude",
                "claude",
                user,
            )
            query = (
                *base,
                "display-message",
                "-p",
                "-t",
                session_id,
                "#{session_id}\t#{session_created}\t#{session_name}\t#{E:UXON_LAUNCH_NONCE}",
            )
            managed = tmux.ManagedTmuxLaunch(
                (),
                query,
                (),
                (*base, "kill-session", "-t"),
                socket,
                expected.name,
                metadata.launch_nonce,
                str(tmp_path),
                "claude",
                "claude",
                user,
                execution_backend=backend.id,
                execution_fingerprint=backend.fingerprint,
            )
            run = process.run_cmd

            def restart(nonce):
                command("kill-server")
                deadline = time.monotonic() + 5
                while (
                    tmux.probe_tmux_server(cfg, user, socket).state != "absent"
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                command("new-session", "-d", "-s", expected.name, "sleep 60")
                command("set-environment", "-t", session_id, "UXON_LAUNCH_NONCE", nonce)

            def restarted(argv, **kwargs):
                if "if-shell" in argv:
                    restart("c" * 32)
                return run(argv, **kwargs)

            with mock.patch.object(process, "run_cmd", side_effect=restarted):
                tmux._kill_created_session_if_owned(managed, pending, metadata)
            assert (
                command("display-message", "-p", "-t", session_id, "#{E:UXON_LAUNCH_NONCE}")
                == "c" * 32
            )
            launch_bootstrap._kill_exact_session(socket, expected.name, "b" * 32)
            query_run = launch_bootstrap.run_query

            def bootstrap_restarted(argv, **kwargs):
                result = query_run(argv, **kwargs)
                if "display-message" in argv:
                    restart("d" * 32)
                return result

            with mock.patch.object(launch_bootstrap, "run_query", side_effect=bootstrap_restarted):
                launch_bootstrap._kill_exact_session(socket, expected.name, "c" * 32)
            assert (
                command("display-message", "-p", "-t", session_id, "#{E:UXON_LAUNCH_NONCE}")
                == "d" * 32
            )
        finally:
            subprocess.run([*base, "kill-server"], capture_output=True, timeout=5)

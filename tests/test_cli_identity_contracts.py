"""Identity and callback contracts at their OS and public callback boundaries."""

import os
import pwd
import socket
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

import pytest
from helpers import make_config

from uxon.cli.main import main
from uxon.domain.session import SessionIdentity
from uxon.errors import eprint, fail
from uxon.infra import audit, config_loader, identity
from uxon.infra.remote_hosts import RemoteHost
from uxon.tui.callback_wrap import _wrap_tui_callback
from uxon.tui.context import CallbackError
from uxon.tui.context_builder import build_tui_context


def test_kernel_login_identity_overrides_forged_environment_and_falls_back(monkeypatch, subtests):
    read = Path.read_text
    monkeypatch.setenv("SUDO_USER", "forged")
    monkeypatch.setattr(os, "getuid", lambda: 1234)

    def user(uid):
        if uid == 5678:
            return pwd.struct_passwd(("alice", "x", uid, 123, "", "/home/alice", "/bin/sh"))
        if uid == 1234:
            return pwd.struct_passwd(("bob", "x", uid, 123, "", "/home/bob", "/bin/sh"))
        raise KeyError(uid)

    monkeypatch.setattr(pwd, "getpwuid", user)
    for value, expected in [
        ("5678\n", "alice"),
        ("4294967295", "bob"),
        ("invalid", "bob"),
        ("9876", "9876"),
        (None, "bob"),
    ]:
        with subtests.test(loginuid=value):

            def proc(path, *args, value=value, **kwargs):
                if path != Path("/proc/self/loginuid"):
                    return read(path, *args, **kwargs)
                if value is None:
                    raise OSError("proc unavailable")
                return value

            monkeypatch.setattr(Path, "read_text", proc)
            assert identity.resolve_caller_user() == expected
    cfg = make_config(
        default_launch_user="dana_agent",
        default_launch_mode="fixed",
        session_users=["alice", "alice"],
    )
    assert identity.resolve_launch_user(cfg, "bob") == "dana_agent"
    cfg.default_launch_mode = "caller"
    assert identity.resolve_launch_user(cfg, "bob") == "bob"
    cfg.launch_user_by_caller = {"bob": " erin "}
    assert identity.resolve_launch_user(cfg, "bob") == "erin"
    assert identity.resolve_all_session_users(cfg, "erin") == ["alice", "erin"]


def test_malformed_operator_file_emits_config_error_before_audit_disable(tmp_path, monkeypatch):
    operator = tmp_path / "operator.toml"
    operator.write_text("[audit]\nenabled=false\ninvalid=[\n")
    monkeypatch.setattr(config_loader, "OPERATOR_CONFIG_PATH", operator)
    address = tmp_path / "audit.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sink:
        sink.bind(str(address))
        sink.settimeout(2)
        monkeypatch.setattr(audit, "_JOURNAL_SOCKET_PATH", str(address))
        monkeypatch.setattr(audit, "_DEV_LOG_PATH", str(tmp_path / "absent-syslog"))
        audit.configure(enabled=True, syslog_facility="user", subcmd="")
        with pytest.raises(SystemExit) as caught:
            main(["doctor"])
        assert "TOML" in caught.value.uxon_msg
        packet = sink.recv(65536)
        assert b"EVENT=config.error" in packet
        assert b"OUTCOME=error" in packet
        assert f"PATH={operator}".encode() in packet
        assert b"TOML" in packet
        assert audit.enabled is True


def test_callback_wrapper_preserves_values_and_complete_human_failures(monkeypatch):
    class CallbackError(Exception):
        pass

    assert _wrap_tui_callback(lambda x, y: x + y, CallbackError)(2, 3) == 5

    def rejected():
        eprint("upstream warning")
        fail("directory must be under one of:\nuxon:   - /srv/projects\nuxon: got: /tmp")

    with pytest.raises(CallbackError) as caught:
        _wrap_tui_callback(rejected, CallbackError)()
    assert (
        str(caught.value)
        == "upstream warning\ndirectory must be under one of:\n  - /srv/projects\ngot: /tmp"
    )

    def exited():
        raise SystemExit(7)

    with pytest.raises(CallbackError, match="^command exited with code 7$"):
        _wrap_tui_callback(exited, CallbackError)()


def test_concurrent_callbacks_keep_errors_separate_and_parser_quiet(capsys):
    original = sys.stderr
    first_entered, second_entered, release_first, first_finished = (
        threading.Event() for _ in range(4)
    )

    def first():
        first_entered.set()
        assert second_entered.wait(5)
        assert release_first.wait(5)
        with pytest.raises(config_loader.ConfigValidationError):
            config_loader.parse_config({"unsupported-setting": True})
        eprint("alpha warning")
        fail("alpha failure")

    def second():
        second_entered.set()
        assert first_finished.wait(5)
        eprint("beta warning")
        fail("beta failure")

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(_wrap_tui_callback(first, CallbackError))
        assert first_entered.wait(5)
        b = pool.submit(_wrap_tui_callback(second, CallbackError))
        assert second_entered.wait(5)
        print("outside", file=sys.stderr)
        release_first.set()
        try:
            with pytest.raises(CallbackError, match="^alpha warning\nalpha failure$"):
                a.result(timeout=5)
        finally:
            first_finished.set()
        with pytest.raises(CallbackError, match="^beta warning\nbeta failure$"):
            b.result(timeout=5)
    assert sys.stderr is original
    assert capsys.readouterr().err == "outside\n"


def test_callback_diagnostics_survive_screen_redirect_with_an_old_worker():
    original = sys.stderr
    old_screen, new_screen = StringIO(), StringIO()
    entered, release = threading.Event(), threading.Event()

    def old_callback():
        entered.set()
        assert release.wait(5)
        eprint("old warning")
        fail("old failure")

    def new_callback():
        eprint("new warning")
        fail("new failure")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with redirect_stderr(old_screen):
            old = pool.submit(_wrap_tui_callback(old_callback, CallbackError))
            assert entered.wait(5)
        try:
            with redirect_stderr(new_screen):
                with pytest.raises(CallbackError, match="^new warning\nnew failure$"):
                    _wrap_tui_callback(new_callback, CallbackError)()
                print("third-party output", file=sys.stderr)
                release.set()
                with pytest.raises(CallbackError, match="^old warning\nold failure$"):
                    old.result(timeout=5)
        finally:
            release.set()
    assert sys.stderr is original
    assert old_screen.getvalue() == ""
    assert new_screen.getvalue() == "third-party output\n"


def test_remote_attach_identity_failure_reaches_bound_callback(tmp_path, capsys):
    cfg = make_config(remote_hosts=[RemoteHost("peer", "edge", "", "uxon")])
    ctx = build_tui_context(cfg, "alice", "alice", str(tmp_path), skeleton=True)
    expected = SessionIdentity("alice", "job", "", "", "", "", "local", "fingerprint")
    with pytest.raises(CallbackError, match="invalid captured identity.*session_id"):
        ctx.on_remote_attach("peer", "alice", "job", expected)
    assert capsys.readouterr().err == ""

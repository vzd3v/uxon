"""Public audit contracts delivered to bounded Unix datagram receivers."""

from __future__ import annotations

import datetime
import json
import os
import pwd
import socket
from pathlib import Path
from unittest.mock import patch

import pytest

from uxon import __version__
from uxon.domain.args import ParsedArgs, owned_option_flags
from uxon.infra import audit as au

CID = "8f3c2d4e-1a6b-4c5e-9f7d-0a1b2c3d4e5f"
OTHER_CID = "12345678-1234-1234-1234-123456789abc"


def reset():
    if au._socket is not None:
        au._socket.close()
    au.enabled, au.sink, au._initialized, au._socket = True, "", False, None
    au._prefix, au._prefix_subcmd, au._syslog_facility_name, au._correlation_id = (
        {},
        "",
        "user",
        None,
    )


def kernel_identity():
    uid = os.getuid()
    raw = int(Path("/proc/self/loginuid").read_text().strip())
    if 0 <= raw < 2**32 - 1:
        uid = raw
    try:
        user = pwd.getpwuid(uid).pw_name
    except KeyError:
        user = str(uid)
    return uid, user


def assert_common(fields, *, event, outcome, correlation, journal=False):
    key = str.upper if journal else lambda s: s
    uid, user = kernel_identity()
    expected = {
        "v": 2,
        "event": event,
        "outcome": outcome,
        "host": socket.gethostname(),
        "uxon_version": __version__,
        "process_user": user,
        "process_uid": uid,
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "subcmd": "run",
        "correlation_id": correlation,
    }
    for name, value in expected.items():
        assert fields[key(name)] == (str(value) if journal else value)
    assert "ssh_client" not in fields and "SSH_CLIENT" not in fields
    assert "audit_schema_version" not in fields and "AUDIT_SCHEMA_VERSION" not in fields
    datetime.datetime.fromisoformat(fields[key("ts")].replace("Z", "+00:00"))


def test_journal_preferred_full_unicode_types_identity_and_correlation(tmp_path, monkeypatch):
    journal, syslog = str(tmp_path / "journal"), str(tmp_path / "syslog")
    monkeypatch.setattr(au, "_JOURNAL_SOCKET_PATH", journal)
    monkeypatch.setattr(au, "_DEV_LOG_PATH", syslog)
    monkeypatch.setenv("USER", "forged")
    monkeypatch.setenv("SUDO_USER", "forged")
    monkeypatch.setenv("SUDO_UID", "9001")
    monkeypatch.setenv("SSH_CONNECTION", "192.0.2.1 1000 192.0.2.2 22")
    reset()
    with (
        socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver,
        socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as fallback,
    ):
        receiver.bind(journal)
        fallback.bind(syslog)
        receiver.settimeout(1)
        fallback.setblocking(False)
        au.configure(enabled=True, syslog_facility="daemon", subcmd="run")
        au.set_correlation_id(CID)
        au.audit(
            "session.ended",
            outcome="error",
            correlation_id=OTHER_CID,
            session="雪\nnext",
            rc=7,
            ok=True,
            values=[1, "雪"],
            absent=None,
            following="after",
        )
        fields = dict(line.split("=", 1) for line in receiver.recv(65536).decode().splitlines())
        assert au.sink == "journal" and au._socket is not None and not au._socket.getblocking()
        assert_common(
            fields, event="session.ended", outcome="error", correlation=OTHER_CID, journal=True
        )
        assert fields["SYSLOG_IDENTIFIER"] == "uxon"
        assert {k: fields[k] for k in ("SESSION", "RC", "OK", "VALUES", "ABSENT", "FOLLOWING")} == {
            "SESSION": '"雪\\nnext"',
            "RC": "7",
            "OK": "true",
            "VALUES": '[1, "雪"]',
            "ABSENT": "",
            "FOLLOWING": "after",
        }
        with pytest.raises(BlockingIOError):
            fallback.recv(65536)
        au.audit("runtime.prepare", runtime_resource="sandbox-a")
        fields = dict(line.split("=", 1) for line in receiver.recv(65536).decode().splitlines())
        assert_common(fields, event="runtime.prepare", outcome="ok", correlation=CID, journal=True)
        assert fields["RUNTIME_RESOURCE"] == "sandbox-a"
    reset()


def test_syslog_fallback_facilities_flags_worktree_and_absent_sink(tmp_path, monkeypatch):
    journal, syslog = tmp_path / "journal", str(tmp_path / "syslog")
    journal.write_text("not a socket")
    monkeypatch.setattr(au, "_JOURNAL_SOCKET_PATH", str(journal))
    monkeypatch.setattr(au, "_DEV_LOG_PATH", syslog)
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
        receiver.bind(syslog)
        receiver.settimeout(1)
        args = ParsedArgs(
            action="run",
            profile="codex",
            permission_mode="safe",
            dry_run=True,
            agent_args=["--api-key", "private-key", "private prompt"],
        )
        flags = owned_option_flags(args)
        assert flags == ["--dry-run", "--profile", "--mode"]
        assert owned_option_flags(
            ParsedArgs(action="attach", host="private-host", user="private-user")
        ) == ["--host", "--user"]
        for facility, pri in [("user", 14), ("daemon", 30), ("bogus", 14)]:
            reset()
            au.configure(enabled=True, syslog_facility=facility, subcmd="run")
            au.set_correlation_id(CID)
            au.audit(
                "cli.start",
                flags=flags,
                profiles_enabled=["codex"],
                enable_all_users_list=False,
                audit_enabled=True,
                allowed_roots_count=0,
                remote_hosts_count=2,
                text="雪\nnext",
                nothing=None,
            )
            text = receiver.recv(65536).decode()
            header, body = text.split("@cee: ", 1)
            fields = json.loads(body)
            assert au.sink == "syslog" and au._socket is not None and not au._socket.getblocking()
            assert_common(fields, event="cli.start", outcome="ok", correlation=CID)
            assert (
                header
                == f"<{pri}>1 {fields['ts']} {socket.gethostname()} uxon {os.getpid()} cli.start - "
            )
            assert {
                k: fields[k]
                for k in (
                    "flags",
                    "profiles_enabled",
                    "enable_all_users_list",
                    "audit_enabled",
                    "allowed_roots_count",
                    "remote_hosts_count",
                    "text",
                    "nothing",
                )
            } == {
                "flags": flags,
                "profiles_enabled": ["codex"],
                "enable_all_users_list": False,
                "audit_enabled": True,
                "allowed_roots_count": 0,
                "remote_hosts_count": 2,
                "text": "雪\nnext",
                "nothing": None,
            }
            assert "private" not in text
        payload = {
            "profile": "claude",
            "agent": "claude",
            "project": "/srv/work/myapp",
            "branch": "feature/auth",
            "path": "/srv/work/myapp/.uxon/worktrees/feature-auth",
            "base": "local",
            "session": "uxon-myapp-feature-auth@claude",
        }
        au.audit("worktree.create", **payload)
        fields = json.loads(receiver.recv(65536).decode().split("@cee: ", 1)[1])
        assert_common(fields, event="worktree.create", outcome="ok", correlation=CID)
        assert {key: fields[key] for key in payload} == payload
    reset()
    Path(syslog).unlink()
    au.audit("session.ended", session="absent")
    assert au.sink == "none" and au._socket is None and journal.read_text() == "not a socket"


def test_disabled_forbids_initialization_and_enabled_control_sends(tmp_path, monkeypatch):
    endpoint = str(tmp_path / "syslog")
    monkeypatch.setattr(au, "_JOURNAL_SOCKET_PATH", str(tmp_path / "missing"))
    monkeypatch.setattr(au, "_DEV_LOG_PATH", endpoint)
    reset()
    au.configure(enabled=False, syslog_facility="user", subcmd="run")
    with (
        patch("socket.socket") as constructor,
        patch("os.stat") as stat,
        patch("pwd.getpwuid") as identity,
    ):
        au.audit("session.ended", session="disabled")
    constructor.assert_not_called()
    stat.assert_not_called()
    identity.assert_not_called()
    assert not au._initialized and au._prefix == {}
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
        receiver.bind(endpoint)
        receiver.settimeout(1)
        au.configure(enabled=True, syslog_facility="user", subcmd="run")
        au.audit("session.ended", session="enabled")
        assert (
            json.loads(receiver.recv(65536).decode().split("@cee: ", 1)[1])["session"] == "enabled"
        )


def test_real_cyclic_serialization_and_external_send_failures_are_failsoft(tmp_path, monkeypatch):
    endpoint = str(tmp_path / "syslog")
    monkeypatch.setattr(au, "_JOURNAL_SOCKET_PATH", str(tmp_path / "absent"))
    monkeypatch.setattr(au, "_DEV_LOG_PATH", endpoint)
    reset()
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
        receiver.bind(endpoint)
        receiver.settimeout(1)
        au.audit("session.ended", session="positive")
        assert b'"session": "positive"' in receiver.recv(65536)
        cycle = []
        cycle.append(cycle)
        au.audit("session.ended", cycle=cycle)
        receiver.setblocking(False)
        with pytest.raises(BlockingIOError):
            receiver.recv(65536)
        sent = []

        class FailingDatagram:
            def send(self, payload):
                sent.append(payload)
                raise failure

        for failure in (OSError("sink failed"), BlockingIOError("backpressure")):
            with patch.object(au, "_socket", FailingDatagram()):
                au.audit("session.ended", session="dropped")
        assert len(sent) == 2
        assert all(
            json.loads(p.decode().split("@cee: ", 1)[1])["session"] == "dropped" for p in sent
        )
        receiver.settimeout(1)
        au.audit("session.ended", session="recovered")
        assert b'"session": "recovered"' in receiver.recv(65536)


def test_correlation_extraction_preserves_remaining_argv():
    for argv, expected in [
        (["--audit-correlation-id", CID, "--json"], (CID, ["--json"])),
        ([f"--audit-correlation-id={CID}", "--json"], (CID, ["--json"])),
        (["--audit-correlation-id", "not-a-uuid", "--json"], (None, ["--json"])),
        (["--audit-correlation-id=evil\nfield", "--json"], (None, ["--json"])),
        (["--json", "--all-users"], (None, ["--json", "--all-users"])),
        (["--json", "--audit-correlation-id"], (None, ["--json"])),
    ]:
        assert au.extract_correlation_id(argv) == expected

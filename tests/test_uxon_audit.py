# SPDX-License-Identifier: MIT
"""Tests for the audit channel (``uxon.audit``).

Covers spec §Testability:
- prefix construction (caller_uid from SUDO_UID, ssh_client omission)
- sink detection ordering (journal > syslog > none)
- native-journal serialization (``KEY=value\\n`` lines)
- syslog-CEE serialization (``<PRI>1 …  @cee: {…}``)
- flag sanitiser (Bug 8)
- ``audit.enabled = false`` short-circuit
- correlation-id flag plumbing
- end-to-end ``audit()`` field set

No test opens a real socket — every send goes through the
``_send_raw`` recorder seam.
"""

from __future__ import annotations

import json
import re
import stat
import unittest
from typing import Any
from unittest.mock import patch

from uxon import audit as au


def _reset_audit_state() -> None:
    """Restore module-level state to first-call shape between tests."""
    au.enabled = True
    au.sink = ""
    au._initialized = False
    au._socket = None
    au._prefix = {}
    au._prefix_subcmd = ""
    au._syslog_facility_name = "user"
    au._correlation_id = None


class _BaseAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_audit_state()
        self.addCleanup(_reset_audit_state)


class PrefixConstructionTests(_BaseAuditTests):
    def test_sudo_uid_overrides_real_uid(self) -> None:
        env = {"SUDO_USER": "alice", "SUDO_UID": "9001", "USER": "root"}
        with (
            patch.dict("os.environ", env, clear=False),
            patch("socket.gethostname", return_value="host1"),
        ):
            prefix = au._build_prefix()
        self.assertEqual(prefix["caller_user"], "alice")
        self.assertEqual(prefix["caller_uid"], 9001)
        self.assertEqual(prefix["host"], "host1")
        self.assertNotIn("ssh_client", prefix)

    def test_ssh_client_recorded_when_env_present(self) -> None:
        env = {"SSH_CONNECTION": "10.0.0.7 51234 192.168.1.5 22", "USER": "bob"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch("socket.gethostname", return_value="host2"),
        ):
            prefix = au._build_prefix()
        self.assertEqual(prefix["ssh_client"], "10.0.0.7 51234 192.168.1.5 22")
        # No SUDO_USER → caller_user falls back to USER.
        self.assertEqual(prefix["caller_user"], "bob")

    def test_subcmd_in_prefix_after_configure(self) -> None:
        au.configure(enabled=True, syslog_facility="user", subcmd="attach")
        with patch.dict("os.environ", {"USER": "x"}, clear=False):
            prefix = au._build_prefix()
        self.assertEqual(prefix["subcmd"], "attach")


class SinkDetectionTests(_BaseAuditTests):
    def _stat_for(self, present: dict[str, bool]):
        sock_mode = stat.S_IFSOCK | 0o666

        class _ST:
            def __init__(self, mode: int) -> None:
                self.st_mode = mode

        def fake(path: str):
            if path in present and present[path]:
                return _ST(sock_mode)
            raise OSError(2, "no")

        return fake

    def test_journal_preferred(self) -> None:
        with patch(
            "os.stat",
            new=self._stat_for({au._JOURNAL_SOCKET_PATH: True, au._DEV_LOG_PATH: True}),
        ):
            self.assertEqual(au._detect_sink(), "journal")

    def test_syslog_when_journal_absent(self) -> None:
        with patch(
            "os.stat",
            new=self._stat_for({au._JOURNAL_SOCKET_PATH: False, au._DEV_LOG_PATH: True}),
        ):
            self.assertEqual(au._detect_sink(), "syslog")

    def test_none_when_both_absent(self) -> None:
        with patch(
            "os.stat",
            new=self._stat_for({au._JOURNAL_SOCKET_PATH: False, au._DEV_LOG_PATH: False}),
        ):
            self.assertEqual(au._detect_sink(), "none")


class JournalSerializationTests(_BaseAuditTests):
    def test_keys_uppercased(self) -> None:
        body = au._serialize_journal({"event": "x.y", "num": 3, "ok": True})
        text = body.decode("utf-8")
        self.assertIn("EVENT=x.y\n", text)
        self.assertIn("NUM=3\n", text)
        self.assertIn("OK=true\n", text)
        self.assertIn("SYSLOG_IDENTIFIER=uxon\n", text)

    def test_multiline_value_does_not_truncate_subsequent_fields(self) -> None:
        # Defensive: if a rendered value ever contains a literal newline,
        # the binary-form encoding ``KEY\n<le u64 length>\nDATA\n`` must
        # be emitted inline and the loop must continue to subsequent
        # fields. ``_journal_value`` currently JSON-escapes newlines so
        # this path is hard to hit upstream — patch it to force the case.
        original = au._journal_value

        def fake(value: Any) -> str:
            if value == "__MULTILINE__":
                return "line1\nline2"
            return original(value)

        with patch.object(au, "_journal_value", side_effect=fake):
            body = au._serialize_journal({"first": "__MULTILINE__", "second": "after"})

        self.assertIn(b"SECOND=after\n", body)
        # Binary form for FIRST: KEY\n + 8-byte little-endian length + data + \n
        idx = body.index(b"FIRST\n")
        length_bytes = body[idx + len(b"FIRST\n") : idx + len(b"FIRST\n") + 8]
        length = int.from_bytes(length_bytes, "little")
        self.assertEqual(length, len(b"line1\nline2"))


class SyslogSerializationTests(_BaseAuditTests):
    def test_pri_header_and_cee_body(self) -> None:
        body = au._serialize_syslog(
            {
                "event": "x.y",
                "ts": "2026-05-05T10:11:12.345Z",
                "host": "h1",
                "pid": 123,
                "extra": [1, 2, 3],
            }
        )
        text = body.decode("utf-8")
        self.assertRegex(text, r"^<\d+>1 ")
        self.assertIn("@cee: ", text)
        # The body after @cee: is parseable JSON.
        idx = text.index("@cee: ") + len("@cee: ")
        parsed = json.loads(text[idx:])
        self.assertEqual(parsed["event"], "x.y")
        self.assertEqual(parsed["extra"], [1, 2, 3])

    def test_facility_default_user(self) -> None:
        body = au._serialize_syslog(
            {"event": "x", "ts": "2026-01-01T00:00:00.000Z", "host": "h", "pid": 1}
        )
        m = re.match(rb"<(\d+)>1 ", body)
        assert m is not None
        pri = int(m.group(1))
        # facility=1 (user), severity=6 (info) → 1*8 + 6 = 14
        self.assertEqual(pri, 14)

    def test_facility_daemon(self) -> None:
        au._syslog_facility_name = "daemon"
        body = au._serialize_syslog(
            {"event": "x", "ts": "2026-01-01T00:00:00.000Z", "host": "h", "pid": 1}
        )
        m = re.match(rb"<(\d+)>1 ", body)
        assert m is not None
        # facility=3 (daemon) → 3*8 + 6 = 30
        self.assertEqual(int(m.group(1)), 30)

    def test_unknown_facility_falls_back_to_user(self) -> None:
        # Operator typo or schema drift: ``[audit].syslog_facility`` set
        # to a name that is not in ``_FACILITY_NAMES``. The serializer
        # must not crash; spec says default is "user" (facility=1).
        au._syslog_facility_name = "bogus"
        body = au._serialize_syslog(
            {"event": "x", "ts": "2026-01-01T00:00:00.000Z", "host": "h", "pid": 1}
        )
        m = re.match(rb"<(\d+)>1 ", body)
        assert m is not None
        # 1 * 8 + 6 = 14
        self.assertEqual(int(m.group(1)), 14)


class FlagSanitizerTests(_BaseAuditTests):
    def test_token_file_value_redacted_inline(self) -> None:
        out = au._sanitize_flags(["--token-file=/etc/secrets/x", "--project=x"])
        self.assertEqual(out, ["--token-file=REDACTED", "--project=x"])

    def test_token_file_value_redacted_separated(self) -> None:
        out = au._sanitize_flags(["--token-file", "/etc/secrets/x", "--project", "p"])
        self.assertEqual(out, ["--token-file", "REDACTED", "--project", "p"])

    def test_password_and_secret_prefixes(self) -> None:
        out = au._sanitize_flags(
            [
                "--password=abc",
                "--secret-key",
                "xyz",
                "--keep",
                "v",
            ]
        )
        self.assertEqual(out, ["--password=REDACTED", "--secret-key", "REDACTED", "--keep", "v"])

    def test_audit_correlation_id_separated_dropped(self) -> None:
        out = au._sanitize_flags(
            ["--audit-correlation-id", "8f3c2d4e-1a6b-4c5e-9f7d-0a1b2c3d4e5f", "list", "--json"]
        )
        self.assertEqual(out, ["list", "--json"])

    def test_audit_correlation_id_inline_dropped(self) -> None:
        out = au._sanitize_flags(["--audit-correlation-id=abc", "list", "--json"])
        self.assertEqual(out, ["list", "--json"])

    def test_audit_correlation_id_trailing_no_value(self) -> None:
        out = au._sanitize_flags(["list", "--audit-correlation-id"])
        self.assertEqual(out, ["list"])


class AuditDisabledTests(_BaseAuditTests):
    def test_disabled_short_circuit_no_send(self) -> None:
        recorded: list[bytes] = []
        with patch.object(au, "_send_raw", side_effect=recorded.append):
            au.configure(enabled=False, syslog_facility="user", subcmd="list")
            au.audit("session.attach", session="x", target_user="y")
        self.assertEqual(recorded, [])


class AuditNeverRaisesTests(_BaseAuditTests):
    """The hot-path contract: ``audit()`` never raises and never blocks."""

    def test_audit_swallows_send_raw_exception(self) -> None:
        # If _send_raw itself raises (a serialiser bug, a memoryview
        # error, etc.), audit() must swallow and return None.
        def boom(_payload: bytes) -> None:
            raise RuntimeError("simulated send failure")

        with (
            patch.object(au, "_detect_sink", return_value="syslog"),
            patch.object(au, "_open_sink_socket", return_value=object()),
            patch.object(au, "_send_raw", side_effect=boom),
            patch.dict("os.environ", {"USER": "tester"}, clear=False),
        ):
            au.configure(enabled=True, syslog_facility="user", subcmd="run")
            # Must not propagate the RuntimeError.
            au.audit("session.attach", session="x", target_user="y")

    def test_audit_swallows_serializer_exception(self) -> None:
        # A serialiser raising (e.g. a future bug in _serialize_syslog)
        # must not propagate either — the bare ``except Exception`` at
        # the bottom of audit() is the safety net.
        def boom(_fields: dict[str, Any]) -> bytes:
            raise ValueError("simulated serializer failure")

        with (
            patch.object(au, "_detect_sink", return_value="syslog"),
            patch.object(au, "_open_sink_socket", return_value=object()),
            patch.object(au, "_serialize_syslog", side_effect=boom),
            patch.dict("os.environ", {"USER": "tester"}, clear=False),
        ):
            au.configure(enabled=True, syslog_facility="user", subcmd="run")
            au.audit("session.attach", session="x", target_user="y")


class CorrelationIdTests(_BaseAuditTests):
    _VALID_UUID = "8f3c2d4e-1a6b-4c5e-9f7d-0a1b2c3d4e5f"

    def test_extract_when_present(self) -> None:
        cid, rest = au.extract_correlation_id(
            ["--audit-correlation-id", self._VALID_UUID, "--json"]
        )
        self.assertEqual(cid, self._VALID_UUID)
        self.assertEqual(rest, ["--json"])

    def test_extract_inline_form(self) -> None:
        cid, rest = au.extract_correlation_id(
            [f"--audit-correlation-id={self._VALID_UUID}", "--json"]
        )
        self.assertEqual(cid, self._VALID_UUID)
        self.assertEqual(rest, ["--json"])

    def test_invalid_uuid_dropped_argv_still_stripped(self) -> None:
        # Malformed value: not a UUID. The flag and value are still
        # stripped from argv so the per-parser walk doesn't see them,
        # but ``cid`` is ``None`` so module state stays clean.
        cid, rest = au.extract_correlation_id(
            ["--audit-correlation-id", "not-a-uuid", "--json"]
        )
        self.assertIsNone(cid)
        self.assertEqual(rest, ["--json"])

    def test_invalid_uuid_inline_dropped(self) -> None:
        cid, rest = au.extract_correlation_id(
            ["--audit-correlation-id=evil\nfield", "--json"]
        )
        self.assertIsNone(cid)
        self.assertEqual(rest, ["--json"])

    def test_extract_absent(self) -> None:
        cid, rest = au.extract_correlation_id(["--json", "--all-users"])
        self.assertIsNone(cid)
        self.assertEqual(rest, ["--json", "--all-users"])

    def test_extract_trailing_value_missing_does_not_raise(self) -> None:
        cid, rest = au.extract_correlation_id(["--json", "--audit-correlation-id"])
        self.assertIsNone(cid)
        self.assertEqual(rest, ["--json"])


class AuditSendTests(_BaseAuditTests):
    def test_envelope_fields_present(self) -> None:
        recorded: list[bytes] = []

        def fake_send(payload: bytes) -> None:
            recorded.append(payload)

        # Force syslog sink so we can json-decode the payload trivially.
        with (
            patch.object(au, "_detect_sink", return_value="syslog"),
            patch.object(au, "_open_sink_socket", return_value=object()),
            patch.object(au, "_send_raw", side_effect=fake_send),
            patch.dict("os.environ", {"USER": "tester"}, clear=False),
        ):
            au.configure(enabled=True, syslog_facility="user", subcmd="run")
            au.audit(
                "cli.start",
                flags=[],
                agents_enabled=["claude"],
                enable_all_users_list=False,
                audit_enabled=True,
                allowed_roots_count=0,
                remote_hosts_count=0,
            )

        self.assertEqual(len(recorded), 1)
        text = recorded[0].decode("utf-8")
        idx = text.index("@cee: ") + len("@cee: ")
        evt: dict[str, Any] = json.loads(text[idx:])
        self.assertEqual(evt["v"], 1)
        self.assertEqual(evt["event"], "cli.start")
        self.assertEqual(evt["outcome"], "ok")
        self.assertIn("ts", evt)
        self.assertEqual(evt["subcmd"], "run")
        self.assertEqual(evt["agents_enabled"], ["claude"])

    def test_correlation_id_threaded_through(self) -> None:
        recorded: list[bytes] = []
        with (
            patch.object(au, "_detect_sink", return_value="syslog"),
            patch.object(au, "_open_sink_socket", return_value=object()),
            patch.object(au, "_send_raw", side_effect=recorded.append),
            patch.dict("os.environ", {"USER": "tester"}, clear=False),
        ):
            au.set_correlation_id("uuid-1234")
            au.audit("list.remote.in", scope="own")

        text = recorded[0].decode("utf-8")
        idx = text.index("@cee: ") + len("@cee: ")
        evt = json.loads(text[idx:])
        self.assertEqual(evt["correlation_id"], "uuid-1234")


if __name__ == "__main__":
    unittest.main()

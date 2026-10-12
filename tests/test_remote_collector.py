"""Tests for the ``uxon.infra.remote`` collector package.

Pin the fail-soft contract that the TUI integration depends on:
every documented failure mode produces a :class:`RemoteSnapshot`
(no exceptions escape), successful fetches do not persist session data,
failed live fetches never return historical rows, and the
ssh argv is constructed with the documented hardening options.

Pure tests — no real SSH, no real subprocess. Spawns go through the
single sanctioned ``run_query`` primitive (:mod:`uxon.infra.run`); tests
patch it (in the consuming module's namespace) to simulate any subprocess
outcome without spawning a child.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from uxon.domain.wire_schema import WIRE_SCHEMA_VERSION
from uxon.infra.remote.collector import (
    DEFAULT_CONNECT_TIMEOUT_SEC,
    DEFAULT_TOTAL_TIMEOUT_SEC,
    _build_fetch_argv,
    fetch_remote_snapshot,
)
from uxon.infra.remote.envelope import decode_envelope
from uxon.infra.remote.master_recovery import (
    _resolved_control_path,
    recover_wedged_master,
)
from uxon.infra.remote_hosts import RemoteHost

_CORRELATION_ID = "12345678-1234-1234-1234-123456789abc"


def _host(**overrides: object) -> RemoteHost:
    base: dict[str, object] = {
        "name": "vz-prod1",
        "ssh_alias": "vz-prod1",
        "description": "",
        "remote_uxon": "uxon",
    }
    base.update(overrides)
    return RemoteHost(**base)  # type: ignore[arg-type]


def _argv_for(host: RemoteHost, **kwargs: Any) -> list[str]:
    """Call ``build_peer_ssh_argv`` with the four primitive fields of
    ``host`` — mirrors how production call sites (attach/kill/fetch)
    invoke it now that the builder is a leaf taking primitives.
    """
    from uxon.infra.remote.ssh_argv import build_peer_ssh_argv

    return build_peer_ssh_argv(
        command_template=host.command_template,
        extra_ssh_options=host.extra_ssh_options,
        ssh_alias=host.ssh_alias,
        remote_uxon=host.remote_uxon,
        **kwargs,
    )


def _good_envelope(sessions: list[dict[str, Any]]) -> str:
    return json.dumps(
        {
            "schema_version": WIRE_SCHEMA_VERSION,
            "uxon_version": "9.9.9",
            "kind": "list",
            "data": {
                "all_users": False,
                "scope_users": ["alice"],
                "session_prefix": "uxon-",
                "sessions": sessions,
            },
        }
    )


class BuildFetchArgvTests(unittest.TestCase):
    def test_passes_alias_and_constructs_remote_command(self) -> None:
        argv = _build_fetch_argv(
            _host(ssh_alias="edge-eu"),
            connect_timeout=7,
            all_users=True,
            correlation_id=_CORRELATION_ID,
            ssh_multiplex="auto",
        )
        self.assertEqual(argv[0], "ssh")
        # Hardening options that disable interactive prompts and bound
        # the connect phase to ConnectTimeout.
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("ConnectTimeout=7", argv)
        # The alias is the host token; remote command is one shell-string
        # argument (so the remote side gets a single shell command line).
        self.assertEqual(argv[-2], "edge-eu")
        # Default invocation now requests the per-target ``--all-users``
        # view; the collector falls back to plain ``list --json`` only
        # when the peer rejects the flag (see
        # ``ALL_USERS_DISABLED_MARKER``).
        self.assertEqual(
            argv[-1],
            f"uxon list --all-users --json --audit-correlation-id {_CORRELATION_ID}",
        )
        # ControlMaster=auto is the default in stage 5+
        self.assertIn("ControlMaster=auto", argv)

    def test_remote_uxon_is_shell_quoted(self) -> None:
        argv = _build_fetch_argv(
            _host(remote_uxon="/opt/u xon/uxon"),
            connect_timeout=5,
            all_users=True,
            correlation_id=_CORRELATION_ID,
            ssh_multiplex="auto",
        )
        # shlex.quote should produce a single-quoted form so the space
        # in the path doesn't split into two tokens on the remote shell.
        self.assertIn("'/opt/u xon/uxon'", argv[-1])

    def test_ssh_multiplex_off_strips_control_options(self) -> None:
        argv = _build_fetch_argv(
            _host(),
            connect_timeout=5,
            all_users=True,
            correlation_id=_CORRELATION_ID,
            ssh_multiplex="off",
        )
        joined = " ".join(argv)
        self.assertNotIn("ControlMaster", joined)
        self.assertNotIn("ControlPath", joined)
        self.assertNotIn("ControlPersist", joined)

    def test_extra_ssh_options_inserted_before_alias(self) -> None:
        argv = _build_fetch_argv(
            _host(extra_ssh_options=("-o", "ProxyJump=bastion")),
            connect_timeout=5,
            all_users=True,
            correlation_id=_CORRELATION_ID,
            ssh_multiplex="auto",
        )
        # extra_ssh_options come immediately before the alias; alias
        # is followed only by the remote command string.
        alias_idx = argv.index("vz-prod1")
        self.assertEqual(argv[alias_idx - 2 : alias_idx], ["-o", "ProxyJump=bastion"])

    def test_command_template_overrides_default(self) -> None:
        argv = _build_fetch_argv(
            _host(
                command_template=(
                    "kubectl",
                    "exec",
                    "uxon-pod",
                    "--",
                    "/bin/sh",
                    "-c",
                    "{remote_command}",
                )
            ),
            connect_timeout=5,
            all_users=True,
            correlation_id=_CORRELATION_ID,
            ssh_multiplex="auto",
        )
        self.assertEqual(argv[0], "kubectl")
        self.assertNotIn("ssh", argv)
        self.assertEqual(
            argv[-1],
            f"uxon list --all-users --json --audit-correlation-id {_CORRELATION_ID}",
        )

    def test_command_template_ignores_extra_ssh_options(self) -> None:
        argv = _build_fetch_argv(
            _host(
                command_template=("ssh", "{ssh_alias}", "{remote_command}"),
                extra_ssh_options=("-o", "ShouldBeIgnored=1"),
            ),
            connect_timeout=5,
            all_users=True,
            correlation_id=_CORRELATION_ID,
            ssh_multiplex="auto",
        )
        self.assertNotIn("-o", argv)
        self.assertNotIn("ShouldBeIgnored=1", argv)

    def test_all_users_false_uses_own_scope_command(self) -> None:
        argv = _build_fetch_argv(
            _host(),
            connect_timeout=5,
            all_users=False,
            correlation_id=_CORRELATION_ID,
            ssh_multiplex="auto",
        )
        self.assertEqual(argv[-1], f"uxon list --json --audit-correlation-id {_CORRELATION_ID}")


class BuildPeerSshArgvTests(unittest.TestCase):
    def test_default_template_no_tty(self) -> None:
        argv = _argv_for(
            _host(ssh_alias="edge-eu"),
            remote_command="uxon list --json",
            allocate_tty=False,
            connect_timeout=7,
            ssh_multiplex="auto",
        )
        self.assertEqual(argv[0], "ssh")
        self.assertNotIn("-tt", argv)
        self.assertIn("ControlMaster=auto", argv)
        self.assertEqual(argv[-2], "edge-eu")
        self.assertEqual(argv[-1], "uxon list --json")

    def test_allocate_tty_inserts_dash_tt_after_ssh(self) -> None:
        argv = _argv_for(
            _host(),
            remote_command="uxon attach --user alice abc",
            allocate_tty=True,
            connect_timeout=5,
            ssh_multiplex="auto",
        )
        self.assertEqual(argv[0], "ssh")
        self.assertEqual(argv[1], "-tt")

    def test_allocate_tty_skipped_for_non_ssh_first_token(self) -> None:
        # Custom templates that don't start with "ssh" do NOT receive
        # -tt — operator owns interactive-tty plumbing in their argv.
        argv = _argv_for(
            _host(
                command_template=(
                    "kubectl",
                    "exec",
                    "uxon-pod",
                    "--",
                    "/bin/sh",
                    "-c",
                    "{remote_command}",
                )
            ),
            remote_command="uxon attach foo",
            allocate_tty=True,
            connect_timeout=5,
            ssh_multiplex="auto",
        )
        self.assertEqual(argv[0], "kubectl")
        self.assertNotIn("-tt", argv)

    def test_ssh_multiplex_off_strips_control_options(self) -> None:
        argv = _argv_for(
            _host(),
            remote_command="uxon attach abc",
            allocate_tty=True,
            connect_timeout=5,
            ssh_multiplex="off",
        )
        joined = " ".join(argv)
        self.assertNotIn("ControlMaster", joined)
        self.assertNotIn("ControlPath", joined)
        self.assertIn("-tt", argv)  # tty insertion still happens

    def test_custom_command_template_honoured(self) -> None:
        argv = _argv_for(
            _host(command_template=("ssh", "-J", "bastion", "{ssh_alias}", "{remote_command}")),
            remote_command="uxon attach --user alice abc",
            allocate_tty=True,
            connect_timeout=5,
            ssh_multiplex="auto",
        )
        # Operator's jumphost preserved; -tt inserted right after ssh
        # (before -J), so it applies to the outermost ssh.
        self.assertEqual(argv[0], "ssh")
        self.assertEqual(argv[1], "-tt")
        self.assertIn("-J", argv)
        self.assertIn("bastion", argv)
        self.assertEqual(argv[-1], "uxon attach --user alice abc")

    def test_extra_ssh_options_inserted_before_alias(self) -> None:
        argv = _argv_for(
            _host(extra_ssh_options=("-o", "ProxyJump=bastion")),
            remote_command="uxon kill --force abc",
            allocate_tty=False,
            connect_timeout=5,
            ssh_multiplex="auto",
        )
        alias_idx = argv.index("vz-prod1")
        self.assertEqual(argv[alias_idx - 2 : alias_idx], ["-o", "ProxyJump=bastion"])


class DecodeEnvelopeTests(unittest.TestCase):
    def test_happy_path(self) -> None:
        result = decode_envelope(_good_envelope([{"name": "uxon-foo@claude"}]))
        self.assertIsNone(result.error)
        self.assertEqual(result.sessions, [{"name": "uxon-foo@claude"}])
        # Older envelopes that omit ``scope_skipped`` parse as an
        # empty list (forward-compatible per-target-sudo addition).
        self.assertEqual(result.scope_skipped, [])
        # Older envelopes also omit ``host_stats``; absence → ``None``.
        self.assertIsNone(result.host_stats)

    def test_invalid_json(self) -> None:
        result = decode_envelope("{not json")
        self.assertIsNone(result.sessions)
        assert result.error is not None
        self.assertIn("invalid JSON", result.error)

    def test_schema_version_mismatch_rejected(self) -> None:
        bad = json.dumps({"schema_version": "1", "kind": "list", "data": {"sessions": []}})
        result = decode_envelope(bad)
        self.assertIsNone(result.sessions)
        assert result.error is not None
        self.assertIn("schema_version mismatch", result.error)

    def test_kind_must_be_list(self) -> None:
        bad = json.dumps(
            {"schema_version": WIRE_SCHEMA_VERSION, "kind": "version", "data": {"sessions": []}}
        )
        result = decode_envelope(bad)
        self.assertIsNone(result.sessions)
        assert result.error is not None
        self.assertIn("unexpected envelope kind", result.error)

    def test_missing_sessions_list(self) -> None:
        bad = json.dumps({"schema_version": WIRE_SCHEMA_VERSION, "kind": "list", "data": {}})
        result = decode_envelope(bad)
        self.assertIsNone(result.sessions)
        assert result.error is not None
        self.assertIn("sessions", result.error)

    def test_top_level_must_be_object(self) -> None:
        result = decode_envelope("[]")
        self.assertIsNone(result.sessions)
        assert result.error is not None
        self.assertIn("not a JSON object", result.error)

    def test_scope_skipped_extracted_when_present(self) -> None:
        env = json.dumps(
            {
                "schema_version": WIRE_SCHEMA_VERSION,
                "kind": "list",
                "data": {
                    "sessions": [],
                    "scope_skipped": ["carol_agent", "dave_agent"],
                },
            }
        )
        result = decode_envelope(env)
        self.assertIsNone(result.error)
        self.assertEqual(result.sessions, [])
        self.assertEqual(result.scope_skipped, ["carol_agent", "dave_agent"])

    def test_host_stats_extracted_when_present(self) -> None:
        env = json.dumps(
            {
                "schema_version": WIRE_SCHEMA_VERSION,
                "kind": "list",
                "data": {"sessions": []},
                "host_stats": {
                    "cpu_pct": 12.5,
                    "mem_used_kib": 1024,
                    "mem_total_kib": 2048,
                    "loadavg_1m": 0.42,
                    "uptime_s": 3600,
                    "kernel": "6.8.0",
                },
            }
        )
        result = decode_envelope(env)
        self.assertIsNone(result.error)
        self.assertEqual(result.sessions, [])
        assert result.host_stats is not None
        self.assertEqual(result.host_stats["kernel"], "6.8.0")
        self.assertEqual(result.host_stats["mem_total_kib"], 2048)


def _patch_run_query(runner: Any) -> Any:
    """Patch the spawn seam used by the collector / master-recovery modules.

    ``run_query`` is the single sanctioned spawn primitive
    (:mod:`uxon.infra.run`); the collector imports it by name, so the
    effective patch target is the consuming module's namespace. Tests pass a
    ``runner(argv, **kwargs)`` callable returning a ``CompletedProcess``-shaped
    object (or raising), exactly as the old ``_runner`` seam did.
    """
    return mock.patch("uxon.infra.remote.collector.run_query", side_effect=runner)


class FetchRemoteSnapshotTests(unittest.TestCase):
    def _runner_returning(self, *, returncode: int, stdout: str = "", stderr: str = ""):
        def _runner(*_args: Any, **_kwargs: Any) -> Any:
            return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)

        return _runner

    def test_success_returns_fresh_without_persisting_session_data(self) -> None:
        with TemporaryDirectory() as tmp:
            sessions = [{"name": "uxon-x@claude"}]
            runner = self._runner_returning(returncode=0, stdout=_good_envelope(sessions))
            with (
                _patch_run_query(runner),
                mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp}),
                mock.patch.object(Path, "open") as disk_io,
            ):
                snap = fetch_remote_snapshot(
                    _host(),
                    ssh_multiplex="off",
                )
            self.assertIsNone(snap.error)
            self.assertEqual(snap.sessions, sessions)
            disk_io.assert_not_called()
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_success_emits_one_correlated_outbound_event(self) -> None:
        recorded: list[tuple[str, dict[str, object]]] = []

        def fake_audit(event: str, *, outcome: str = "ok", **fields: object) -> None:
            recorded.append((event, {"outcome": outcome, **fields}))

        runner = self._runner_returning(returncode=0, stdout=_good_envelope([]))
        with (
            _patch_run_query(runner) as run,
            mock.patch("uxon.infra.remote.collector.uuid.uuid4", return_value=_CORRELATION_ID),
            mock.patch("uxon.infra.audit.audit", side_effect=fake_audit),
        ):
            fetch_remote_snapshot(_host())
        [event] = [fields for name, fields in recorded if name == "list.remote.out"]
        self.assertEqual(event["outcome"], "ok")
        self.assertEqual(event["correlation_id"], _CORRELATION_ID)
        self.assertEqual(event["rc"], 0)
        self.assertIn(_CORRELATION_ID, run.call_args.args[0][-1])

    def test_all_users_retry_emits_one_event_with_final_rc(self) -> None:
        recorded: list[tuple[str, dict[str, object]]] = []
        attempts = iter(
            [
                mock.Mock(
                    returncode=2,
                    stdout="",
                    stderr="uxon-error: all-users-disabled",
                ),
                mock.Mock(returncode=0, stdout=_good_envelope([]), stderr=""),
            ]
        )

        def runner(*_args: Any, **_kwargs: Any) -> Any:
            return next(attempts)

        def fake_audit(event: str, *, outcome: str = "ok", **fields: object) -> None:
            recorded.append((event, {"outcome": outcome, **fields}))

        with (
            _patch_run_query(runner) as run,
            mock.patch("uxon.infra.audit.audit", side_effect=fake_audit),
        ):
            snap = fetch_remote_snapshot(_host())
        self.assertTrue(snap.scope_limited)
        self.assertEqual(run.call_count, 2)
        [event] = [fields for name, fields in recorded if name == "list.remote.out"]
        self.assertEqual(event["outcome"], "ok")
        self.assertEqual(event["rc"], 0)

    def test_ssh_nonzero_exit_returns_empty(self) -> None:
        runner = self._runner_returning(returncode=255, stderr="boom")
        with _patch_run_query(runner):
            snap = fetch_remote_snapshot(
                _host(),
            )
        self.assertEqual(snap.sessions, [])
        self.assertEqual(snap.scope_skipped, [])
        self.assertIsNone(snap.host_stats)
        assert snap.error is not None

    def test_malformed_live_payload_returns_only_diagnostics(self) -> None:
        runner = self._runner_returning(returncode=0, stdout="not json")
        with _patch_run_query(runner), mock.patch("uxon.infra.audit.audit") as audit:
            snap = fetch_remote_snapshot(_host(), ssh_multiplex="off")
        self.assertEqual(snap.sessions, [])
        self.assertIsNone(snap.host_stats)
        self.assertEqual(snap.error_code, "invalid_response")
        assert snap.error is not None
        self.assertIn("invalid JSON", snap.error)
        self.assertEqual(audit.call_args.kwargs["outcome"], "error")

    def test_timeout_captured_as_error(self) -> None:
        def _raise_timeout(*_args: Any, **_kwargs: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd="ssh", timeout=10)

        recorded: list[tuple[str, dict[str, object]]] = []

        def fake_audit(event: str, *, outcome: str = "ok", **fields: object) -> None:
            recorded.append((event, {"outcome": outcome, **fields}))

        with _patch_run_query(_raise_timeout):
            with mock.patch("uxon.infra.audit.audit", side_effect=fake_audit):
                snap = fetch_remote_snapshot(
                    _host(),
                )
            assert snap.error is not None
            self.assertIn("timeout", snap.error)
            self.assertEqual(snap.sessions, [])
        [event] = [fields for name, fields in recorded if name == "list.remote.out"]
        self.assertEqual(event["outcome"], "error")
        self.assertEqual(event["rc"], 124)

    def test_ssh_not_installed_captured(self) -> None:
        def _raise_fnf(*_args: Any, **_kwargs: Any) -> Any:
            raise FileNotFoundError(2, "No such file or directory: 'ssh'")

        with _patch_run_query(_raise_fnf):
            snap = fetch_remote_snapshot(
                _host(),
            )
            assert snap.error is not None
            self.assertIn("ssh not installed", snap.error)

    def test_keyboard_interrupt_propagates(self) -> None:
        # Ctrl-C must escape the fail-soft net so the operator can
        # actually cancel a stuck collector.
        def _raise_kbd(*_args: Any, **_kwargs: Any) -> Any:
            raise KeyboardInterrupt

        with _patch_run_query(_raise_kbd):
            with self.assertRaises(KeyboardInterrupt):
                fetch_remote_snapshot(
                    _host(),
                )

    def test_default_timeouts_are_sane(self) -> None:
        # Sanity-check the documented defaults haven't drifted.
        self.assertGreaterEqual(DEFAULT_CONNECT_TIMEOUT_SEC, 1)
        self.assertGreaterEqual(DEFAULT_TOTAL_TIMEOUT_SEC, DEFAULT_CONNECT_TIMEOUT_SEC)

    def test_per_host_connect_timeout_overrides_default(self) -> None:
        """``host.connect_timeout`` (if set) overrides the keyword default."""
        captured: list[list[str]] = []

        def runner(argv, **kwargs):
            captured.append(argv)
            cp = mock.Mock()
            cp.returncode = 0
            cp.stdout = _good_envelope([])
            cp.stderr = ""
            return cp

        with _patch_run_query(runner):
            fetch_remote_snapshot(
                _host(connect_timeout=2.0),
                connect_timeout=10,
            )
        argv = captured[0]
        self.assertIn("ConnectTimeout=2", argv)

    def test_sub_second_connect_timeout_ceil_rounds_to_one(self) -> None:
        """``connect_timeout = "500ms"`` must NOT produce ssh ``ConnectTimeout=0``.

        ssh treats 0 as "system default" (minutes). The collector
        ceil-rounds sub-second durations to 1 so the operator's intent
        ("tight cap") is preserved.
        """
        captured: list[list[str]] = []

        def runner(argv, **kwargs):
            captured.append(argv)
            cp = mock.Mock()
            cp.returncode = 0
            cp.stdout = _good_envelope([])
            cp.stderr = ""
            return cp

        with _patch_run_query(runner):
            fetch_remote_snapshot(
                _host(connect_timeout=0.5),
            )
        self.assertIn("ConnectTimeout=1", captured[0])
        self.assertNotIn("ConnectTimeout=0", captured[0])

    def test_per_host_total_timeout_overrides_default(self) -> None:
        """``host.total_timeout`` overrides the subprocess timeout=."""
        captured: dict = {}

        def runner(argv, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            cp = mock.Mock()
            cp.returncode = 0
            cp.stdout = _good_envelope([])
            cp.stderr = ""
            return cp

        with _patch_run_query(runner):
            fetch_remote_snapshot(
                _host(total_timeout=8.0),
                total_timeout=30,
            )
        self.assertEqual(captured["timeout"], 8)


class DefaultTemplateTests(unittest.TestCase):
    """Stage 5 step 3: default ssh argv template includes ControlMaster
    and the closed placeholder set."""

    def test_default_template_has_controlmaster(self) -> None:
        from uxon.infra.remote.ssh_argv import _default_template

        tmpl = _default_template()
        flat = " ".join(tmpl)
        self.assertIn("ControlMaster=auto", flat)
        self.assertIn("ControlPath={ssh_control_dir}/ssh-%C", flat)
        # Task 10: ControlPersist is parameterised by
        # ``ssh_control_persist_seconds`` (default 300, see
        # ``DEFAULT_CONFIG``).
        self.assertIn("ControlPersist={ssh_control_persist_seconds}s", flat)
        self.assertIn("ServerAliveInterval=15", flat)

    def test_default_template_uses_persist_placeholder(self) -> None:
        from uxon.infra.remote.ssh_argv import _default_template

        template = _default_template()
        assert "ControlPersist={ssh_control_persist_seconds}s" in template

    def test_default_template_uses_only_closed_placeholders(self) -> None:
        from uxon.infra.remote.ssh_argv import PLACEHOLDER_CLOSED_SET, _default_template

        for token in _default_template():
            i = 0
            while i < len(token):
                start = token.find("{", i)
                if start == -1:
                    break
                end = token.find("}", start)
                if end == -1:
                    break
                placeholder = token[start : end + 1]
                self.assertIn(
                    placeholder,
                    PLACEHOLDER_CLOSED_SET,
                    msg=f"unexpected placeholder {placeholder!r} in default template",
                )
                i = end + 1


class RenderArgvTests(unittest.TestCase):
    """Stage 5 step 3: _render_argv substitutes placeholders cleanly."""

    def test_renders_full_default_template(self) -> None:
        from uxon.infra.remote.ssh_argv import _default_template, _render_argv

        argv = _render_argv(
            _default_template(),
            ssh_alias="peer1",
            remote_uxon="uxon",
            connect_timeout=5,
            ssh_control_dir="/home/me/.cache/uxon",
            remote_command="uxon list --all-users --json",
        )
        self.assertEqual(argv[0], "ssh")
        self.assertIn("peer1", argv)
        self.assertIn("uxon list --all-users --json", argv)
        self.assertIn("ControlPath=/home/me/.cache/uxon/ssh-%C", argv)
        # No unresolved placeholders.
        for token in argv:
            self.assertNotRegex(token, r"\{[a-z_]+\}")

    def test_kubectl_recipe(self) -> None:
        from uxon.infra.remote.ssh_argv import _render_argv

        template = [
            "kubectl",
            "exec",
            "uxon-pod",
            "--",
            "/bin/sh",
            "-c",
            "{remote_command}",
        ]
        argv = _render_argv(
            template,
            ssh_alias="ignored",
            remote_uxon="uxon",
            connect_timeout=5,
            ssh_control_dir="/tmp/uxon",
            remote_command="uxon list --json",
        )
        self.assertEqual(argv[-1], "uxon list --json")
        self.assertEqual(argv[0], "kubectl")


class ValidateCommandTemplateTests(unittest.TestCase):
    """Stage 5 step 3: validate_command_template enforces the closed
    placeholder set and {remote_command}/{remote_uxon} mutual exclusion."""

    def test_default_template_validates(self) -> None:
        from uxon.infra.remote.ssh_argv import _default_template, validate_command_template

        validate_command_template(_default_template())  # must not raise

    def test_kubectl_template_validates(self) -> None:
        from uxon.infra.remote.ssh_argv import validate_command_template

        validate_command_template(
            ["kubectl", "exec", "uxon-pod", "--", "/bin/sh", "-c", "{remote_command}"]
        )

    def test_docker_template_validates(self) -> None:
        from uxon.infra.remote.ssh_argv import validate_command_template

        validate_command_template(
            ["docker", "exec", "uxon-container", "/bin/sh", "-c", "{remote_command}"]
        )

    def test_unknown_placeholder_rejected(self) -> None:
        from uxon.infra.remote.ssh_argv import validate_command_template

        with self.assertRaises(ValueError) as cm:
            validate_command_template(["ssh", "{ssh_alias}", "{bad}"])
        self.assertIn("{bad}", str(cm.exception))

    def test_remote_command_and_remote_uxon_mutually_exclusive(self) -> None:
        from uxon.infra.remote.ssh_argv import validate_command_template

        with self.assertRaises(ValueError) as cm:
            validate_command_template(
                ["ssh", "{ssh_alias}", "{remote_uxon}", "list", "{remote_command}"]
            )
        self.assertIn("mutually exclusive", str(cm.exception))

    def test_empty_template_rejected(self) -> None:
        from uxon.infra.remote.ssh_argv import validate_command_template

        with self.assertRaises(ValueError):
            validate_command_template([])


class ResolvedControlPathTests(unittest.TestCase):
    """``_resolved_control_path`` translates ``%C`` via ``ssh -G`` without
    opening a connection — the recovery path needs the real socket
    file path to know what to ``unlink``."""

    def test_extracts_path_from_ssh_g_output(self) -> None:
        def _runner(*_args: Any, **_kwargs: Any) -> Any:
            return mock.Mock(
                returncode=0,
                stdout="user erin\ncontrolpath /home/u/.cache/uxon/ssh-abc123\nport 22\n",
            )

        with mock.patch("uxon.infra.remote.master_recovery.run_query", side_effect=_runner):
            path = _resolved_control_path("box-b", "/home/u/.cache/uxon")
        self.assertEqual(path, "/home/u/.cache/uxon/ssh-abc123")

    def test_returns_none_on_nonzero_rc(self) -> None:
        def _runner(*_args: Any, **_kwargs: Any) -> Any:
            return mock.Mock(returncode=255, stdout="")

        with mock.patch("uxon.infra.remote.master_recovery.run_query", side_effect=_runner):
            self.assertIsNone(_resolved_control_path("box-b", "/home/u/.cache/uxon"))

    def test_returns_none_on_timeout(self) -> None:
        def _runner(*_args: Any, **_kwargs: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd="ssh", timeout=2)

        with mock.patch("uxon.infra.remote.master_recovery.run_query", side_effect=_runner):
            self.assertIsNone(_resolved_control_path("box-b", "/home/u/.cache/uxon"))

    def test_returns_none_when_output_missing_controlpath(self) -> None:
        def _runner(*_args: Any, **_kwargs: Any) -> Any:
            return mock.Mock(returncode=0, stdout="user erin\nport 22\n")

        with mock.patch("uxon.infra.remote.master_recovery.run_query", side_effect=_runner):
            self.assertIsNone(_resolved_control_path("box-b", "/home/u/.cache/uxon"))


class RecoverWedgedMasterTests(unittest.TestCase):
    """Self-heal path invoked when a slave hangs on a wedged
    ``ControlMaster``. Each step (graceful exit, hard kill, socket
    unlink) must run independently and tolerate partial failure."""

    def _capture_runner(self) -> tuple[list[list[str]], Any]:
        calls: list[list[str]] = []

        def _runner(argv: list[str], *_a: Any, **_kw: Any) -> Any:
            calls.append(list(argv))
            # Default: ssh -G returns the resolved path.
            return mock.Mock(
                returncode=0,
                stdout="controlpath /tmp/uxon-test/ssh-resolved\n",
                stderr="",
            )

        return calls, _runner

    def test_skips_when_host_uses_command_template(self) -> None:
        calls, runner = self._capture_runner()
        host = _host(command_template=["docker", "exec", "uxon-c", "{remote_command}"])
        with mock.patch("uxon.infra.remote.master_recovery.run_query", side_effect=runner):
            recover_wedged_master(host)
        self.assertEqual(calls, [])

    def test_runs_graceful_then_kills_then_unlinks(self) -> None:
        with TemporaryDirectory() as tmp:
            socket = Path(tmp) / "ssh-resolved"
            socket.touch()
            calls: list[list[str]] = []

            def _runner(argv: list[str], *_a: Any, **_kw: Any) -> Any:
                calls.append(list(argv))
                return mock.Mock(
                    returncode=0,
                    stdout=f"controlpath {socket}\n",
                    stderr="",
                )

            killed: list[tuple[int, int]] = []

            def _kill(pid: int, sig: int) -> None:
                killed.append((pid, sig))

            with mock.patch("uxon.infra.remote.master_recovery.run_query", side_effect=_runner):
                recover_wedged_master(
                    _host(),
                    _resolve=lambda alias, ctl_dir: str(socket),
                    _find_pid=lambda path: 4242,
                    _kill=_kill,
                )
            # Graceful ``ssh -O exit`` ran first.
            self.assertEqual(calls[0][:3], ["ssh", "-O", "exit"])
            # Master was hard-killed.
            self.assertEqual(killed, [(4242, signal.SIGKILL)])
            # Socket file is gone.
            self.assertFalse(socket.exists())

    def test_skips_kill_when_path_unresolved(self) -> None:
        _, runner = self._capture_runner()
        killed: list[tuple[int, int]] = []
        with mock.patch("uxon.infra.remote.master_recovery.run_query", side_effect=runner):
            recover_wedged_master(
                _host(),
                _resolve=lambda *a, **kw: None,
                _find_pid=lambda _path: 999,
                _kill=lambda pid, sig: killed.append((pid, sig)),
            )
        self.assertEqual(killed, [])

    def test_skips_kill_when_socket_already_gone(self) -> None:
        with TemporaryDirectory() as tmp:
            ghost = Path(tmp) / "ssh-already-cleaned"
            # File deliberately not created — graceful ssh -O exit removed it.
            _, runner = self._capture_runner()
            killed: list[tuple[int, int]] = []
            with mock.patch("uxon.infra.remote.master_recovery.run_query", side_effect=runner):
                recover_wedged_master(
                    _host(),
                    _resolve=lambda *a, **kw: str(ghost),
                    _find_pid=lambda _path: 999,
                    _kill=lambda pid, sig: killed.append((pid, sig)),
                )
            self.assertEqual(killed, [])

    def test_skips_kill_when_pid_not_found(self) -> None:
        with TemporaryDirectory() as tmp:
            socket = Path(tmp) / "ssh-resolved"
            socket.touch()
            _, runner = self._capture_runner()
            killed: list[tuple[int, int]] = []
            with mock.patch("uxon.infra.remote.master_recovery.run_query", side_effect=runner):
                recover_wedged_master(
                    _host(),
                    _resolve=lambda *a, **kw: str(socket),
                    _find_pid=lambda _path: None,
                    _kill=lambda pid, sig: killed.append((pid, sig)),
                )
            self.assertEqual(killed, [])
            # Socket still gets unlinked even when no pid found —
            # otherwise next ssh would slave-loop on the stale socket.
            self.assertFalse(socket.exists())

    def test_swallows_runner_failures(self) -> None:
        def _boom(*_a: Any, **_kw: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd="ssh", timeout=2)

        # Must not propagate even when every shell-out times out.
        with mock.patch("uxon.infra.remote.master_recovery.run_query", side_effect=_boom):
            recover_wedged_master(
                _host(),
                _resolve=lambda *a, **kw: None,
                _find_pid=lambda _p: None,
                _kill=lambda *a: None,
            )


class FetchTimeoutRecoveryTests(unittest.TestCase):
    """Fetch path invokes recovery exactly when its preconditions
    hold: a real ``TimeoutExpired`` AND multiplexing is enabled."""

    def _timeout_runner(self) -> Any:
        def _runner(*_a: Any, **_kw: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd="ssh", timeout=10)

        return _runner

    def test_timeout_triggers_recovery_when_multiplex_on(self) -> None:
        recovered: list[RemoteHost] = []

        def _fake_recover(host: RemoteHost) -> None:
            recovered.append(host)

        with (
            _patch_run_query(self._timeout_runner()),
            mock.patch(
                "uxon.infra.remote.collector.recover_wedged_master",
                side_effect=_fake_recover,
            ),
        ):
            snap = fetch_remote_snapshot(
                _host(),
                ssh_multiplex="auto",
            )
            assert snap.error is not None
            self.assertIn("timeout", snap.error)
            self.assertEqual([h.name for h in recovered], ["vz-prod1"])

    def test_timeout_skips_recovery_when_multiplex_off(self) -> None:
        recovered: list[RemoteHost] = []

        def _fake_recover(host: RemoteHost) -> None:
            recovered.append(host)

        with (
            _patch_run_query(self._timeout_runner()),
            mock.patch(
                "uxon.infra.remote.collector.recover_wedged_master",
                side_effect=_fake_recover,
            ),
        ):
            fetch_remote_snapshot(
                _host(),
                ssh_multiplex="off",
            )
            self.assertEqual(recovered, [])


if __name__ == "__main__":
    unittest.main()

"""Tests for ``uxon.remote_collector``.

Pin the fail-soft contract that the TUI integration depends on:
every documented failure mode produces a :class:`RemoteSnapshot`
(no exceptions escape), the cache is written only on a real
success, the cache is consulted only on a failed fetch, and the
ssh argv is constructed with the documented hardening options.

Pure tests — no real SSH, no real subprocess. The ``_runner`` seam
on :func:`fetch_remote_snapshot` lets us simulate any subprocess
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

from uxon.remote_collector import (
    DEFAULT_CONNECT_TIMEOUT_SEC,
    DEFAULT_TOTAL_TIMEOUT_SEC,
    RemoteSnapshot,
    _build_fetch_argv,
    _parse_envelope,
    _recover_wedged_master,
    _resolved_control_path,
    fetch_remote_snapshot,
    read_cached_snapshot,
    snapshot_cache_path,
    state_dir,
    write_cached_snapshot,
)
from uxon.remote_hosts import RemoteHost
from uxon.wire_schema import WIRE_SCHEMA_VERSION


def _host(**overrides: object) -> RemoteHost:
    base: dict[str, object] = {
        "name": "vz-prod1",
        "ssh_alias": "vz-prod1",
        "description": "",
        "remote_uxon": "uxon",
    }
    base.update(overrides)
    return RemoteHost(**base)  # type: ignore[arg-type]


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


class StateDirTests(unittest.TestCase):
    def test_xdg_state_home_honoured(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/tmp/xdg-test"}, clear=False):
            self.assertEqual(state_dir(), Path("/tmp/xdg-test/uxon/remote"))

    def test_default_under_home_local_state(self) -> None:
        # When ``$XDG_STATE_HOME`` is unset, platformdirs derives the
        # path from ``$HOME`` per the XDG Base Directory spec.
        env = dict(os.environ)
        env.pop("XDG_STATE_HOME", None)
        env["HOME"] = "/home/alice"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(state_dir(), Path("/home/alice/.local/state/uxon/remote"))

    def test_override_short_circuits_env(self) -> None:
        # Tests use this seam to keep their fixtures inside tmpdir.
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/tmp/x"}, clear=False):
            self.assertEqual(state_dir(override=Path("/foo")), Path("/foo"))


class SnapshotCachePathTests(unittest.TestCase):
    def test_filename_uses_host_name(self) -> None:
        with TemporaryDirectory() as tmp:
            p = snapshot_cache_path("vz-prod1", override_dir=Path(tmp))
            self.assertEqual(p.name, "vz-prod1.json")
            self.assertEqual(p.parent, Path(tmp))


class BuildFetchArgvTests(unittest.TestCase):
    def test_passes_alias_and_constructs_remote_command(self) -> None:
        argv = _build_fetch_argv(
            _host(ssh_alias="edge-eu"),
            connect_timeout=7,
            all_users=True,
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
        self.assertEqual(argv[-1], "uxon list --all-users --json")
        # ControlMaster=auto is the default in stage 5+
        self.assertIn("ControlMaster=auto", argv)

    def test_remote_uxon_is_shell_quoted(self) -> None:
        argv = _build_fetch_argv(
            _host(remote_uxon="/opt/u xon/uxon"),
            connect_timeout=5,
            all_users=True,
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
            ssh_multiplex="auto",
        )
        self.assertEqual(argv[0], "kubectl")
        self.assertNotIn("ssh", argv)
        self.assertEqual(argv[-1], "uxon list --all-users --json")

    def test_command_template_ignores_extra_ssh_options(self) -> None:
        argv = _build_fetch_argv(
            _host(
                command_template=("ssh", "{ssh_alias}", "{remote_command}"),
                extra_ssh_options=("-o", "ShouldBeIgnored=1"),
            ),
            connect_timeout=5,
            all_users=True,
            ssh_multiplex="auto",
        )
        self.assertNotIn("-o", argv)
        self.assertNotIn("ShouldBeIgnored=1", argv)

    def test_all_users_false_uses_legacy_command(self) -> None:
        argv = _build_fetch_argv(
            _host(),
            connect_timeout=5,
            all_users=False,
            ssh_multiplex="auto",
        )
        self.assertEqual(argv[-1], "uxon list --json")


class BuildPeerSshArgvTests(unittest.TestCase):
    def test_default_template_no_tty(self) -> None:
        from uxon.remote_collector import build_peer_ssh_argv

        argv = build_peer_ssh_argv(
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
        from uxon.remote_collector import build_peer_ssh_argv

        argv = build_peer_ssh_argv(
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
        from uxon.remote_collector import build_peer_ssh_argv

        argv = build_peer_ssh_argv(
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
        from uxon.remote_collector import build_peer_ssh_argv

        argv = build_peer_ssh_argv(
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
        from uxon.remote_collector import build_peer_ssh_argv

        argv = build_peer_ssh_argv(
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
        from uxon.remote_collector import build_peer_ssh_argv

        argv = build_peer_ssh_argv(
            _host(extra_ssh_options=("-o", "ProxyJump=bastion")),
            remote_command="uxon kill --force abc",
            allocate_tty=False,
            connect_timeout=5,
            ssh_multiplex="auto",
        )
        alias_idx = argv.index("vz-prod1")
        self.assertEqual(argv[alias_idx - 2 : alias_idx], ["-o", "ProxyJump=bastion"])


class ParseEnvelopeTests(unittest.TestCase):
    def test_happy_path(self) -> None:
        sessions, scope_skipped, host_stats, err = _parse_envelope(
            _good_envelope([{"name": "uxon-foo@claude"}])
        )
        self.assertIsNone(err)
        self.assertEqual(sessions, [{"name": "uxon-foo@claude"}])
        # Older envelopes that omit ``scope_skipped`` parse as an
        # empty list (forward-compatible per-target-sudo addition).
        self.assertEqual(scope_skipped, [])
        # Older envelopes also omit ``host_stats``; absence → ``None``.
        self.assertIsNone(host_stats)

    def test_invalid_json(self) -> None:
        sessions, _scope_skipped, _host_stats, err = _parse_envelope("{not json")
        self.assertIsNone(sessions)
        assert err is not None
        self.assertIn("invalid JSON", err)

    def test_schema_version_mismatch_rejected(self) -> None:
        bad = json.dumps({"schema_version": "2", "kind": "list", "data": {"sessions": []}})
        sessions, _scope_skipped, _host_stats, err = _parse_envelope(bad)
        self.assertIsNone(sessions)
        assert err is not None
        self.assertIn("schema_version mismatch", err)

    def test_kind_must_be_list(self) -> None:
        bad = json.dumps(
            {"schema_version": WIRE_SCHEMA_VERSION, "kind": "version", "data": {"sessions": []}}
        )
        sessions, _scope_skipped, _host_stats, err = _parse_envelope(bad)
        self.assertIsNone(sessions)
        assert err is not None
        self.assertIn("unexpected envelope kind", err)

    def test_missing_sessions_list(self) -> None:
        bad = json.dumps({"schema_version": WIRE_SCHEMA_VERSION, "kind": "list", "data": {}})
        sessions, _scope_skipped, _host_stats, err = _parse_envelope(bad)
        self.assertIsNone(sessions)
        assert err is not None
        self.assertIn("sessions", err)

    def test_top_level_must_be_object(self) -> None:
        sessions, _scope_skipped, _host_stats, err = _parse_envelope("[]")
        self.assertIsNone(sessions)
        assert err is not None
        self.assertIn("not a JSON object", err)

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
        sessions, scope_skipped, _host_stats, err = _parse_envelope(env)
        self.assertIsNone(err)
        self.assertEqual(sessions, [])
        self.assertEqual(scope_skipped, ["carol_agent", "dave_agent"])

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
        sessions, _scope_skipped, host_stats, err = _parse_envelope(env)
        self.assertIsNone(err)
        self.assertEqual(sessions, [])
        assert host_stats is not None
        self.assertEqual(host_stats["kernel"], "6.8.0")
        self.assertEqual(host_stats["mem_total_kib"], 2048)


class CacheRoundTripTests(unittest.TestCase):
    def test_write_then_read_round_trip(self) -> None:
        with TemporaryDirectory() as tmp:
            snap = RemoteSnapshot(
                host_name="vz-prod1",
                fetched_at_epoch=1700000000.0,
                from_cache=False,
                error=None,
                sessions=[{"name": "uxon-foo@claude", "user": "alice"}],
                cached_at_epoch=1700000000.0,
            )
            write_cached_snapshot(snap, override_dir=Path(tmp))
            loaded = read_cached_snapshot("vz-prod1", override_dir=Path(tmp))
            assert loaded is not None
            self.assertEqual(loaded.host_name, "vz-prod1")
            self.assertTrue(loaded.from_cache)
            self.assertEqual(loaded.sessions, snap.sessions)
            self.assertEqual(loaded.cached_at_epoch, 1700000000.0)

    def test_write_skipped_when_error_set(self) -> None:
        # Failed fetches must NOT overwrite the last good cache.
        with TemporaryDirectory() as tmp:
            good = RemoteSnapshot(
                host_name="x",
                fetched_at_epoch=1.0,
                from_cache=False,
                error=None,
                sessions=[{"name": "good"}],
                cached_at_epoch=1.0,
            )
            write_cached_snapshot(good, override_dir=Path(tmp))
            bad = RemoteSnapshot(
                host_name="x",
                fetched_at_epoch=2.0,
                from_cache=False,
                error="ssh exited 255",
                sessions=[],
                cached_at_epoch=None,
            )
            write_cached_snapshot(bad, override_dir=Path(tmp))
            loaded = read_cached_snapshot("x", override_dir=Path(tmp))
            assert loaded is not None
            self.assertEqual(loaded.sessions, [{"name": "good"}])

    def test_write_skipped_when_from_cache(self) -> None:
        # Re-writing a from_cache snapshot would mark cached_at_epoch
        # as 'now', falsifying staleness reports.
        with TemporaryDirectory() as tmp:
            snap = RemoteSnapshot(
                host_name="x",
                fetched_at_epoch=99.0,
                from_cache=True,
                error="ssh exited 255",
                sessions=[{"name": "stale"}],
                cached_at_epoch=1.0,
            )
            write_cached_snapshot(snap, override_dir=Path(tmp))
            self.assertFalse(snapshot_cache_path("x", override_dir=Path(tmp)).exists())

    def test_read_missing_file_returns_none(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertIsNone(read_cached_snapshot("absent", override_dir=Path(tmp)))

    def test_read_corrupt_file_returns_none(self) -> None:
        with TemporaryDirectory() as tmp:
            p = snapshot_cache_path("corrupt", override_dir=Path(tmp))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{not json", encoding="utf-8")
            self.assertIsNone(read_cached_snapshot("corrupt", override_dir=Path(tmp)))

    def test_write_failure_unlinks_temp_file(self) -> None:
        # If the atomic-write step crashes (disk full, EINTR), the
        # ``.tmp`` file must not survive. Otherwise a future ``ls`` of
        # the state dir would surface a stale partial.
        with TemporaryDirectory() as tmp:
            snap = RemoteSnapshot(
                host_name="x",
                fetched_at_epoch=1.0,
                from_cache=False,
                error=None,
                sessions=[],
                cached_at_epoch=1.0,
            )
            # Create the parent so chmod path doesn't fault first.
            (Path(tmp)).mkdir(parents=True, exist_ok=True)
            with mock.patch.object(
                Path,
                "replace",
                side_effect=OSError("simulated disk full"),
            ):
                with self.assertRaises(OSError):
                    write_cached_snapshot(snap, override_dir=Path(tmp))
            leftovers = sorted(p.name for p in Path(tmp).iterdir())
            self.assertNotIn("x.json.tmp", leftovers)

    def test_existing_dir_is_chmodded_to_0700(self) -> None:
        # mkdir(mode=0o700, exist_ok=True) does not chmod an existing
        # directory. We force-apply 0o700 after mkdir to honour the
        # documented per-user privacy invariant.
        with TemporaryDirectory() as tmp:
            override = Path(tmp) / "remote"
            override.mkdir(mode=0o755)
            self.assertEqual(override.stat().st_mode & 0o777, 0o755)
            snap = RemoteSnapshot(
                host_name="x",
                fetched_at_epoch=1.0,
                from_cache=False,
                error=None,
                sessions=[],
                cached_at_epoch=1.0,
            )
            write_cached_snapshot(snap, override_dir=override)
            self.assertEqual(override.stat().st_mode & 0o777, 0o700)

    def test_atomic_write_uses_temp_file(self) -> None:
        # The sequence is mkdir → write tmp → rename. After the call,
        # only the final file should exist (no leftover .tmp).
        with TemporaryDirectory() as tmp:
            snap = RemoteSnapshot(
                host_name="x",
                fetched_at_epoch=1.0,
                from_cache=False,
                error=None,
                sessions=[],
                cached_at_epoch=1.0,
            )
            write_cached_snapshot(snap, override_dir=Path(tmp))
            files = sorted(p.name for p in Path(tmp).iterdir())
            self.assertEqual(files, ["x.json"])


class FetchRemoteSnapshotTests(unittest.TestCase):
    def _runner_returning(self, *, returncode: int, stdout: str = "", stderr: str = ""):
        def _runner(*_args: Any, **_kwargs: Any) -> Any:
            return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)

        return _runner

    def test_success_writes_cache_and_returns_fresh(self) -> None:
        with TemporaryDirectory() as tmp:
            sessions = [{"name": "uxon-x@claude"}]
            runner = self._runner_returning(returncode=0, stdout=_good_envelope(sessions))
            snap = fetch_remote_snapshot(
                _host(),
                override_state_dir=Path(tmp),
                _runner=runner,
            )
            self.assertIsNone(snap.error)
            self.assertFalse(snap.from_cache)
            self.assertEqual(snap.sessions, sessions)
            # Cache should now exist.
            self.assertTrue(snapshot_cache_path("vz-prod1", override_dir=Path(tmp)).exists())

    def test_ssh_nonzero_exit_falls_back_to_cache(self) -> None:
        with TemporaryDirectory() as tmp:
            # Seed the cache with a known-good payload.
            seed = RemoteSnapshot(
                host_name="vz-prod1",
                fetched_at_epoch=1.0,
                from_cache=False,
                error=None,
                sessions=[{"name": "cached@claude"}],
                cached_at_epoch=1.0,
            )
            write_cached_snapshot(seed, override_dir=Path(tmp))

            runner = self._runner_returning(returncode=255, stderr="ssh: connect: timed out")
            snap = fetch_remote_snapshot(
                _host(),
                override_state_dir=Path(tmp),
                _runner=runner,
            )
            self.assertTrue(snap.from_cache)
            self.assertEqual(snap.sessions, [{"name": "cached@claude"}])
            assert snap.error is not None
            self.assertIn("ssh exited 255", snap.error)
            # Cache must not have been overwritten.
            self.assertEqual(snap.cached_at_epoch, 1.0)

    def test_ssh_nonzero_exit_no_cache_returns_empty(self) -> None:
        with TemporaryDirectory() as tmp:
            runner = self._runner_returning(returncode=255, stderr="boom")
            snap = fetch_remote_snapshot(
                _host(),
                override_state_dir=Path(tmp),
                _runner=runner,
            )
            self.assertFalse(snap.from_cache)
            self.assertEqual(snap.sessions, [])
            assert snap.error is not None

    def test_timeout_captured_as_error(self) -> None:
        def _raise_timeout(*_args: Any, **_kwargs: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd="ssh", timeout=10)

        with TemporaryDirectory() as tmp:
            snap = fetch_remote_snapshot(
                _host(),
                override_state_dir=Path(tmp),
                _runner=_raise_timeout,
            )
            assert snap.error is not None
            self.assertIn("timeout", snap.error)
            self.assertEqual(snap.sessions, [])

    def test_ssh_not_installed_captured(self) -> None:
        def _raise_fnf(*_args: Any, **_kwargs: Any) -> Any:
            raise FileNotFoundError(2, "No such file or directory: 'ssh'")

        with TemporaryDirectory() as tmp:
            snap = fetch_remote_snapshot(
                _host(),
                override_state_dir=Path(tmp),
                _runner=_raise_fnf,
            )
            assert snap.error is not None
            self.assertIn("ssh not installed", snap.error)

    def test_malformed_payload_falls_back_to_cache(self) -> None:
        with TemporaryDirectory() as tmp:
            # Seed cache.
            seed = RemoteSnapshot(
                host_name="vz-prod1",
                fetched_at_epoch=1.0,
                from_cache=False,
                error=None,
                sessions=[{"name": "cached"}],
                cached_at_epoch=1.0,
            )
            write_cached_snapshot(seed, override_dir=Path(tmp))
            runner = self._runner_returning(returncode=0, stdout="not json")
            snap = fetch_remote_snapshot(
                _host(),
                override_state_dir=Path(tmp),
                _runner=runner,
            )
            self.assertTrue(snap.from_cache)
            self.assertEqual(snap.sessions, [{"name": "cached"}])
            assert snap.error is not None
            self.assertIn("invalid JSON", snap.error)

    def test_keyboard_interrupt_propagates(self) -> None:
        # Ctrl-C must escape the fail-soft net so the operator can
        # actually cancel a stuck collector.
        def _raise_kbd(*_args: Any, **_kwargs: Any) -> Any:
            raise KeyboardInterrupt

        with TemporaryDirectory() as tmp:
            with self.assertRaises(KeyboardInterrupt):
                fetch_remote_snapshot(
                    _host(),
                    override_state_dir=Path(tmp),
                    _runner=_raise_kbd,
                )

    def test_cache_write_failure_does_not_taint_fresh_snapshot(self) -> None:
        # A successful live fetch must return a valid snapshot even
        # when the cache write step crashes — the operator may be on
        # a read-only home or out of disk; that should not blank the
        # TUI when fresh data is in hand.
        with TemporaryDirectory() as tmp:
            sessions = [{"name": "uxon-x@claude"}]
            runner = self._runner_returning(returncode=0, stdout=_good_envelope(sessions))
            with mock.patch(
                "uxon.remote_collector.write_cached_snapshot",
                side_effect=OSError("simulated"),
            ):
                snap = fetch_remote_snapshot(
                    _host(),
                    override_state_dir=Path(tmp),
                    _runner=runner,
                )
            self.assertIsNone(snap.error)
            self.assertFalse(snap.from_cache)
            self.assertEqual(snap.sessions, sessions)

    def test_default_timeouts_are_sane(self) -> None:
        # Sanity-check the documented defaults haven't drifted.
        self.assertGreaterEqual(DEFAULT_CONNECT_TIMEOUT_SEC, 1)
        self.assertGreaterEqual(DEFAULT_TOTAL_TIMEOUT_SEC, DEFAULT_CONNECT_TIMEOUT_SEC)

    def test_per_host_connect_timeout_overrides_default(self) -> None:
        """``host.connect_timeout`` (if set) overrides the keyword default."""
        with TemporaryDirectory() as tmp:
            captured: list[list[str]] = []

            def runner(argv, **kwargs):
                captured.append(argv)
                cp = mock.Mock()
                cp.returncode = 0
                cp.stdout = _good_envelope([])
                cp.stderr = ""
                return cp

            fetch_remote_snapshot(
                _host(connect_timeout=2.0),
                connect_timeout=10,
                override_state_dir=Path(tmp),
                _runner=runner,
            )
            argv = captured[0]
            self.assertIn("ConnectTimeout=2", argv)

    def test_sub_second_connect_timeout_ceil_rounds_to_one(self) -> None:
        """``connect_timeout = "500ms"`` must NOT produce ssh ``ConnectTimeout=0``.

        ssh treats 0 as "system default" (minutes). The collector
        ceil-rounds sub-second durations to 1 so the operator's intent
        ("tight cap") is preserved.
        """
        with TemporaryDirectory() as tmp:
            captured: list[list[str]] = []

            def runner(argv, **kwargs):
                captured.append(argv)
                cp = mock.Mock()
                cp.returncode = 0
                cp.stdout = _good_envelope([])
                cp.stderr = ""
                return cp

            fetch_remote_snapshot(
                _host(connect_timeout=0.5),
                override_state_dir=Path(tmp),
                _runner=runner,
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

        with TemporaryDirectory() as tmp:
            fetch_remote_snapshot(
                _host(total_timeout=8.0),
                total_timeout=30,
                override_state_dir=Path(tmp),
                _runner=runner,
            )
        self.assertEqual(captured["timeout"], 8)


class CacheScopeRoundtripTests(unittest.TestCase):
    """Stage 5 step 8: cache persists/restores scope_limited/scope_skipped."""

    def _runner_returning(self, returncode: int, stdout: str = "", stderr: str = "") -> Any:
        def runner(argv, **kwargs):
            cp = mock.Mock()
            cp.returncode = returncode
            cp.stdout = stdout
            cp.stderr = stderr
            return cp

        return runner

    def test_write_then_read_preserves_scope_flags(self) -> None:
        with TemporaryDirectory() as tmp:
            snap = RemoteSnapshot(
                host_name="vz-prod1",
                fetched_at_epoch=100.0,
                from_cache=False,
                error=None,
                sessions=[{"name": "uxon-x@claude"}],
                cached_at_epoch=100.0,
                scope_limited=True,
                scope_skipped=["alice", "bob"],
            )
            write_cached_snapshot(snap, override_dir=Path(tmp))
            loaded = read_cached_snapshot("vz-prod1", override_dir=Path(tmp))
            assert loaded is not None
            self.assertTrue(loaded.scope_limited)
            self.assertEqual(loaded.scope_skipped, ["alice", "bob"])

    def test_old_cache_without_scope_keys_treated_as_defaults(self) -> None:
        with TemporaryDirectory() as tmp:
            # Synthesise a pre-stage-5 cache file (no scope_* fields).
            old_blob = {
                "host_name": "legacy",
                "cached_at_epoch": 50.0,
                "sessions": [{"name": "uxon-y@codex"}],
            }
            cache_path = snapshot_cache_path("legacy", override_dir=Path(tmp))
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(old_blob), encoding="utf-8")

            loaded = read_cached_snapshot("legacy", override_dir=Path(tmp))
            assert loaded is not None
            self.assertFalse(loaded.scope_limited)
            self.assertEqual(loaded.scope_skipped, [])

    def test_failure_path_with_cache_returns_cached_scope_flags(self) -> None:
        """Live fetch fails; cache fallback must carry the cached flags
        — NOT the failed live-fetch's intermediate values."""
        with TemporaryDirectory() as tmp:
            cached = RemoteSnapshot(
                host_name="vz-prod1",
                fetched_at_epoch=99.0,
                from_cache=False,
                error=None,
                sessions=[{"name": "uxon-cached@claude"}],
                cached_at_epoch=99.0,
                scope_limited=True,
                scope_skipped=["carol"],
            )
            write_cached_snapshot(cached, override_dir=Path(tmp))

            # Simulate live failure — non-zero exit, no payload.
            failing = self._runner_returning(returncode=255, stderr="connect: timed out")
            snap = fetch_remote_snapshot(
                _host(),
                override_state_dir=Path(tmp),
                _runner=failing,
            )
            self.assertTrue(snap.from_cache)
            self.assertIsNotNone(snap.error)
            # Critical: cached flags survive the failure-with-cache path.
            self.assertTrue(snap.scope_limited)
            self.assertEqual(snap.scope_skipped, ["carol"])

    def test_failure_path_without_cache_returns_empty_scope_skipped(self) -> None:
        """No cache file; returned snapshot has scope_skipped=[] (no info)."""
        with TemporaryDirectory() as tmp:
            failing = self._runner_returning(returncode=255, stderr="connect: timed out")
            snap = fetch_remote_snapshot(
                _host(name="never-cached"),
                override_state_dir=Path(tmp),
                _runner=failing,
            )
            self.assertFalse(snap.from_cache)
            self.assertIsNotNone(snap.error)
            self.assertEqual(snap.scope_skipped, [])


class DefaultTemplateTests(unittest.TestCase):
    """Stage 5 step 3: default ssh argv template includes ControlMaster
    and the closed placeholder set."""

    def test_default_template_has_controlmaster(self) -> None:
        from uxon.remote_collector import _default_template

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
        from uxon.remote_collector import _default_template

        template = _default_template()
        assert "ControlPersist={ssh_control_persist_seconds}s" in template

    def test_default_template_uses_only_closed_placeholders(self) -> None:
        from uxon.remote_collector import PLACEHOLDER_CLOSED_SET, _default_template

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
        from uxon.remote_collector import _default_template, _render_argv

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
        from uxon.remote_collector import _render_argv

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
        from uxon.remote_collector import _default_template, validate_command_template

        validate_command_template(_default_template())  # must not raise

    def test_kubectl_template_validates(self) -> None:
        from uxon.remote_collector import validate_command_template

        validate_command_template(
            ["kubectl", "exec", "uxon-pod", "--", "/bin/sh", "-c", "{remote_command}"]
        )

    def test_docker_template_validates(self) -> None:
        from uxon.remote_collector import validate_command_template

        validate_command_template(
            ["docker", "exec", "uxon-container", "/bin/sh", "-c", "{remote_command}"]
        )

    def test_unknown_placeholder_rejected(self) -> None:
        from uxon.remote_collector import validate_command_template

        with self.assertRaises(ValueError) as cm:
            validate_command_template(["ssh", "{ssh_alias}", "{bad}"])
        self.assertIn("{bad}", str(cm.exception))

    def test_remote_command_and_remote_uxon_mutually_exclusive(self) -> None:
        from uxon.remote_collector import validate_command_template

        with self.assertRaises(ValueError) as cm:
            validate_command_template(
                ["ssh", "{ssh_alias}", "{remote_uxon}", "list", "{remote_command}"]
            )
        self.assertIn("mutually exclusive", str(cm.exception))

    def test_empty_template_rejected(self) -> None:
        from uxon.remote_collector import validate_command_template

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
                stdout="user remdepl\ncontrolpath /home/u/.cache/uxon/ssh-abc123\nport 22\n",
            )

        path = _resolved_control_path("box-b", "/home/u/.cache/uxon", _runner=_runner)
        self.assertEqual(path, "/home/u/.cache/uxon/ssh-abc123")

    def test_returns_none_on_nonzero_rc(self) -> None:
        def _runner(*_args: Any, **_kwargs: Any) -> Any:
            return mock.Mock(returncode=255, stdout="")

        self.assertIsNone(_resolved_control_path("box-b", "/home/u/.cache/uxon", _runner=_runner))

    def test_returns_none_on_timeout(self) -> None:
        def _runner(*_args: Any, **_kwargs: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd="ssh", timeout=2)

        self.assertIsNone(_resolved_control_path("box-b", "/home/u/.cache/uxon", _runner=_runner))

    def test_returns_none_when_output_missing_controlpath(self) -> None:
        def _runner(*_args: Any, **_kwargs: Any) -> Any:
            return mock.Mock(returncode=0, stdout="user remdepl\nport 22\n")

        self.assertIsNone(_resolved_control_path("box-b", "/home/u/.cache/uxon", _runner=_runner))


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
        _recover_wedged_master(host, _runner=runner)
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

            _recover_wedged_master(
                _host(),
                _runner=_runner,
                _resolve=lambda alias, ctl_dir, _runner: str(socket),
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
        _recover_wedged_master(
            _host(),
            _runner=runner,
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
            _recover_wedged_master(
                _host(),
                _runner=runner,
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
            _recover_wedged_master(
                _host(),
                _runner=runner,
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
        _recover_wedged_master(
            _host(),
            _runner=_boom,
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

        def _fake_recover(host: RemoteHost, *, _runner: Any) -> None:
            recovered.append(host)

        with (
            TemporaryDirectory() as tmp,
            mock.patch(
                "uxon.remote_collector._recover_wedged_master",
                side_effect=_fake_recover,
            ),
        ):
            snap = fetch_remote_snapshot(
                _host(),
                override_state_dir=Path(tmp),
                ssh_multiplex="auto",
                _runner=self._timeout_runner(),
            )
            assert snap.error is not None
            self.assertIn("timeout", snap.error)
            self.assertEqual([h.name for h in recovered], ["vz-prod1"])

    def test_timeout_skips_recovery_when_multiplex_off(self) -> None:
        recovered: list[RemoteHost] = []

        def _fake_recover(host: RemoteHost, *, _runner: Any) -> None:
            recovered.append(host)

        with (
            TemporaryDirectory() as tmp,
            mock.patch(
                "uxon.remote_collector._recover_wedged_master",
                side_effect=_fake_recover,
            ),
        ):
            fetch_remote_snapshot(
                _host(),
                override_state_dir=Path(tmp),
                ssh_multiplex="off",
                _runner=self._timeout_runner(),
            )
            self.assertEqual(recovered, [])


if __name__ == "__main__":
    unittest.main()

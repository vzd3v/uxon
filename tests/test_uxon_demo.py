"""Tests for ``uxon._demo``.

Pin the contract used by the screenshot demo:

- ``demo_hosts_dir()`` reads ``UXON_DEMO_HOSTS`` and returns ``None``
  unless the env var points to an existing directory.
- ``synthesize_remote_hosts()`` produces one :class:`RemoteHost` per
  ``*.json`` file, sorted by name, with the ``demo:`` sentinel alias.
- ``load_demo_snapshot()`` returns a fresh, in-memory
  :class:`RemoteSnapshot` from a valid envelope; malformed / missing
  envelopes yield an error snapshot (no exceptions).
- ``fetch_remote_snapshot()`` short-circuits to the demo loader when
  the env var is set AND the host carries the sentinel alias — no ssh
  is spawned.
- A sentinel alias seen without the env var set produces a clear error
  snapshot rather than a silent ``ssh demo:foo`` invocation.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from uxon import _demo as uxon_demo
from uxon.remote_collector import fetch_remote_snapshot
from uxon.remote_hosts import RemoteHost
from uxon.wire_schema import WIRE_SCHEMA_VERSION


def _envelope(sessions: list[dict[str, Any]], *, demo_color: str | None = None) -> dict[str, Any]:
    env: dict[str, Any] = {
        "schema_version": WIRE_SCHEMA_VERSION,
        "uxon_version": "demo",
        "kind": "list",
        "data": {"sessions": sessions, "scope_users": [], "scope_skipped": []},
    }
    if demo_color is not None:
        env["demo_color"] = demo_color
    return env


def _write(dir_path: Path, name: str, payload: dict[str, Any]) -> Path:
    p = dir_path / f"{name}.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


class DemoHostsDirTests(unittest.TestCase):
    def test_unset_returns_none(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop(uxon_demo.DEMO_ENV_VAR, None)
            self.assertIsNone(uxon_demo.demo_hosts_dir())

    def test_blank_returns_none(self) -> None:
        with mock.patch.dict("os.environ", {uxon_demo.DEMO_ENV_VAR: "   "}, clear=False):
            self.assertIsNone(uxon_demo.demo_hosts_dir())

    def test_nonexistent_returns_none(self) -> None:
        with mock.patch.dict(
            "os.environ", {uxon_demo.DEMO_ENV_VAR: "/no/such/dir/uxon-demo-xyzzy"}, clear=False
        ):
            self.assertIsNone(uxon_demo.demo_hosts_dir())

    def test_existing_dir_returned(self) -> None:
        with TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {uxon_demo.DEMO_ENV_VAR: tmp}, clear=False):
                got = uxon_demo.demo_hosts_dir()
            self.assertEqual(got, Path(tmp))


class SynthesizeRemoteHostsTests(unittest.TestCase):
    def test_one_host_per_envelope_sorted(self) -> None:
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write(d, "zulu", _envelope([]))
            _write(d, "alpha", _envelope([]))
            _write(d, "mike", _envelope([]))
            hosts = uxon_demo.synthesize_remote_hosts(d)
        self.assertEqual([h.name for h in hosts], ["alpha", "mike", "zulu"])
        for h in hosts:
            self.assertTrue(h.ssh_alias.startswith(uxon_demo.DEMO_SSH_ALIAS_PREFIX))
            self.assertIn(h.name, h.ssh_alias)

    def test_non_json_files_ignored(self) -> None:
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write(d, "alpha", _envelope([]))
            (d / "README.md").write_text("note", encoding="utf-8")
            (d / "rendered.yaml").write_text("x: 1", encoding="utf-8")
            hosts = uxon_demo.synthesize_remote_hosts(d)
        self.assertEqual([h.name for h in hosts], ["alpha"])

    def test_underscore_prefixed_files_skipped(self) -> None:
        """``_local.json`` (and any ``_*.json``) is reserved, never a peer."""
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write(d, "alpha", _envelope([]))
            _write(d, "_local", _envelope([]))
            _write(d, "_scratch", _envelope([]))
            hosts = uxon_demo.synthesize_remote_hosts(d)
        self.assertEqual([h.name for h in hosts], ["alpha"])

    def test_demo_color_extracted(self) -> None:
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write(d, "alpha", _envelope([], demo_color="magenta"))
            _write(d, "bravo", _envelope([]))
            hosts = uxon_demo.synthesize_remote_hosts(d)
        by_name = {h.name: h for h in hosts}
        self.assertEqual(by_name["alpha"].color, "magenta")
        self.assertIsNone(by_name["bravo"].color)


class LoadDemoSnapshotTests(unittest.TestCase):
    def test_valid_envelope_round_trips_sessions(self) -> None:
        sessions = [
            {
                "user": "alice",
                "name": "uxon-checkout@claude",
                "short_id": "checkout@claude",
                "agent": "claude",
                "attached": True,
                "windows": "3",
                "created": "",
                "last_attached": "",
                "pane_pids": [1234],
                "active_pid": 1234,
                "active_cmd": "claude",
                "active_path": "/home/alice/checkout",
                "cpu_pct": 12.5,
                "rss_kib": 524288,
                "legacy": False,
            }
        ]
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write(d, "laptop", _envelope(sessions))
            snap = uxon_demo.load_demo_snapshot("laptop", d, fetched_at=12345.0)
        self.assertIsNone(snap.error)
        self.assertEqual(snap.host_name, "laptop")
        self.assertFalse(snap.from_cache)
        self.assertEqual(snap.sessions, sessions)
        self.assertEqual(snap.fetched_at_epoch, 12345.0)
        self.assertEqual(snap.cached_at_epoch, 12345.0)

    def test_missing_file_yields_error_snapshot(self) -> None:
        with TemporaryDirectory() as tmp:
            snap = uxon_demo.load_demo_snapshot("ghost", Path(tmp), fetched_at=1.0)
        self.assertIsNotNone(snap.error)
        assert snap.error is not None
        self.assertIn("not found", snap.error)
        self.assertEqual(snap.sessions, [])

    def test_schema_mismatch_yields_error_snapshot(self) -> None:
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            payload = _envelope([])
            payload["schema_version"] = "999"
            _write(d, "laptop", payload)
            snap = uxon_demo.load_demo_snapshot("laptop", d, fetched_at=1.0)
        self.assertIsNotNone(snap.error)
        assert snap.error is not None
        self.assertIn("schema_version", snap.error)

    def test_malformed_json_yields_error_snapshot(self) -> None:
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "laptop.json").write_text("not json {", encoding="utf-8")
            snap = uxon_demo.load_demo_snapshot("laptop", d, fetched_at=1.0)
        self.assertIsNotNone(snap.error)
        assert snap.error is not None
        self.assertIn("unreadable", snap.error)


class FetchRemoteSnapshotDemoHookTests(unittest.TestCase):
    """Pin the short-circuit in ``fetch_remote_snapshot``."""

    def _runner_must_not_run(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess runner invoked in demo mode")

    def test_demo_alias_with_env_set_shortcircuits(self) -> None:
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write(d, "laptop", _envelope([{"user": "alice", "name": "uxon-x@claude"}]))
            host = RemoteHost(
                name="laptop",
                ssh_alias=f"{uxon_demo.DEMO_SSH_ALIAS_PREFIX}laptop",
                description="",
                remote_uxon="uxon",
            )
            with mock.patch.dict("os.environ", {uxon_demo.DEMO_ENV_VAR: str(d)}, clear=False):
                snap = fetch_remote_snapshot(host, _runner=self._runner_must_not_run)
        self.assertIsNone(snap.error)
        self.assertEqual(len(snap.sessions), 1)

    def test_demo_alias_without_env_yields_error_no_ssh(self) -> None:
        host = RemoteHost(
            name="laptop",
            ssh_alias=f"{uxon_demo.DEMO_SSH_ALIAS_PREFIX}laptop",
            description="",
            remote_uxon="uxon",
        )
        # ``mock.patch.dict`` with a pop in the patched block guarantees
        # the env var is restored after the test even if it was set in
        # the caller's environment.
        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop(uxon_demo.DEMO_ENV_VAR, None)
            snap = fetch_remote_snapshot(host, _runner=self._runner_must_not_run)
        self.assertIsNotNone(snap.error)
        assert snap.error is not None
        self.assertIn(uxon_demo.DEMO_ENV_VAR, snap.error)

    def test_real_alias_with_env_set_is_not_intercepted(self) -> None:
        """A real (non-demo) host must still go through ssh when env is set.

        Guarantees the demo hook is keyed on the alias sentinel, not on
        the env var alone — operators with a mixed config aren't
        silently disabled.
        """
        host = RemoteHost(
            name="prod1",
            ssh_alias="prod1",
            description="",
            remote_uxon="uxon",
        )
        ran: dict[str, bool] = {"called": False}

        def fake_runner(*args: Any, **kwargs: Any) -> Any:
            ran["called"] = True
            raise FileNotFoundError("ssh")

        with TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {uxon_demo.DEMO_ENV_VAR: tmp}, clear=False):
                fetch_remote_snapshot(host, _runner=fake_runner)
        self.assertTrue(ran["called"], "ssh runner was not invoked for non-demo host")


class LoadDemoLocalSessionsTests(unittest.TestCase):
    """Pin the synthetic-local-section loader: ``_local.json`` ⇒ SessionInfo."""

    def _record(self, **overrides: Any) -> dict[str, Any]:
        rec: dict[str, Any] = {
            "user": "alice",
            "name": "uxon-checkout@claude",
            "short_id": "checkout@claude",
            "agent": "claude",
            "attached": True,
            "windows": "3",
            "created": "2026-05-01T10:00:00+00:00",
            "last_attached": "2026-05-20T14:00:00+00:00",
            "pane_pids": [1234, 1235],
            "active_pid": 1234,
            "active_cmd": "claude",
            "active_path": "/home/alice/checkout",
            "cpu_pct": 12.5,
            "rss_kib": 524288,
            "legacy": False,
        }
        rec.update(overrides)
        return rec

    def test_missing_file_returns_empty(self) -> None:
        with TemporaryDirectory() as tmp:
            got = uxon_demo.load_demo_local_sessions(Path(tmp), "alice")
        self.assertEqual(got, [])

    def test_malformed_json_returns_empty(self) -> None:
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / uxon_demo.LOCAL_ENVELOPE_NAME).write_text("not json {", encoding="utf-8")
            got = uxon_demo.load_demo_local_sessions(d, "alice")
        self.assertEqual(got, [])

    def test_schema_mismatch_returns_empty(self) -> None:
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            payload = _envelope([self._record()])
            payload["schema_version"] = "999"
            (d / uxon_demo.LOCAL_ENVELOPE_NAME).write_text(json.dumps(payload), encoding="utf-8")
            got = uxon_demo.load_demo_local_sessions(d, "alice")
        self.assertEqual(got, [])

    def test_multi_user_envelope_is_filtered(self) -> None:
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            payload = _envelope(
                [
                    self._record(user="alice", name="uxon-a@claude"),
                    self._record(user="bob", name="uxon-b@claude"),
                    self._record(user="alice", name="uxon-c@claude"),
                ]
            )
            (d / uxon_demo.LOCAL_ENVELOPE_NAME).write_text(json.dumps(payload), encoding="utf-8")
            alice = uxon_demo.load_demo_local_sessions(d, "alice")
            bob = uxon_demo.load_demo_local_sessions(d, "bob")
            ghost = uxon_demo.load_demo_local_sessions(d, "carol")
        self.assertEqual([s.name for s in alice], ["uxon-a@claude", "uxon-c@claude"])
        self.assertEqual([s.name for s in bob], ["uxon-b@claude"])
        self.assertEqual(ghost, [])

    def test_record_fields_round_trip_into_session_info(self) -> None:
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            payload = _envelope([self._record(attached=True)])
            (d / uxon_demo.LOCAL_ENVELOPE_NAME).write_text(json.dumps(payload), encoding="utf-8")
            got = uxon_demo.load_demo_local_sessions(d, "alice")
        self.assertEqual(len(got), 1)
        s = got[0]
        self.assertEqual(s.user, "alice")
        self.assertEqual(s.name, "uxon-checkout@claude")
        self.assertEqual(s.attached, "1")  # bool ⇒ tmux-style "1"/"0"
        self.assertEqual(s.windows, "3")
        self.assertEqual(s.pane_pids, (1234, 1235))
        self.assertEqual(s.active_pid, 1234)
        self.assertEqual(s.active_cmd, "claude")
        self.assertEqual(s.cpu_pct, 12.5)
        self.assertEqual(s.rss_kib, 524288)
        self.assertEqual(s.agent, "claude")
        self.assertFalse(s.legacy)


class CollectSessionsDemoHookTests(unittest.TestCase):
    """Pin the short-circuit in ``cli.collect_sessions_for_user``."""

    def test_demo_env_set_does_not_spawn_subprocess(self) -> None:
        """Demo mode must never invoke tmux — otherwise the operator's
        real socket leaks into the demo's local section."""
        from uxon import cli as uxon_cli

        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            payload = _envelope(
                [
                    {
                        "user": "alice",
                        "name": "uxon-x@claude",
                        "agent": "claude",
                        "attached": False,
                        "windows": "1",
                        "pane_pids": [42],
                        "active_pid": 42,
                        "active_cmd": "claude",
                        "active_path": "/home/alice",
                        "cpu_pct": 0.0,
                        "rss_kib": 1024,
                        "legacy": False,
                    }
                ]
            )
            (d / uxon_demo.LOCAL_ENVELOPE_NAME).write_text(json.dumps(payload), encoding="utf-8")

            def boom(*args: Any, **kwargs: Any) -> Any:
                raise AssertionError("subprocess.run invoked in demo mode")

            with (
                mock.patch.dict("os.environ", {uxon_demo.DEMO_ENV_VAR: str(d)}, clear=False),
                mock.patch.object(uxon_cli.subprocess, "run", side_effect=boom),
            ):
                got = uxon_cli.collect_sessions_for_user("alice", "uxon-", None)
        self.assertEqual([s.name for s in got], ["uxon-x@claude"])

    def test_demo_env_unset_falls_through_to_tmux(self) -> None:
        """Sanity: with the env unset the existing tmux path runs.

        Mock ``subprocess.run`` to short-circuit the actual probe so the
        test stays hermetic (no real tmux binary required)."""
        from uxon import cli as uxon_cli

        calls: list[list[str]] = []

        class FakeProbe:
            returncode = 1
            stdout = ""
            stderr = ""

        def fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> Any:
            calls.append(cmd)
            return FakeProbe()

        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop(uxon_demo.DEMO_ENV_VAR, None)
            with mock.patch.object(uxon_cli.subprocess, "run", side_effect=fake_run):
                got = uxon_cli.collect_sessions_for_user("alice", "uxon-", None)
        self.assertEqual(got, [])
        self.assertTrue(calls, "expected tmux probe to be attempted when demo env is unset")


if __name__ == "__main__":
    unittest.main()

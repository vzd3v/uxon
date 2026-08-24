"""Tests for ``uxon.infra.sudo_probe``.

Pin the per-target execution probe contract that the TUI supervision
block, ``uxon list --all-users``, and the multi-host aggregator
all depend on. Pure unit tests — no real ``sudo`` is invoked; the
``subprocess.run`` calls inside :mod:`uxon.infra.sudo_probe` are stubbed
so the suite stays deterministic and fast on a CI runner with no
sudoers configuration.
"""

from __future__ import annotations

import subprocess
import sys
import time
import unittest
from contextlib import contextmanager
from unittest import mock

from helpers import make_config

from uxon.domain.sudo import SudoCapability
from uxon.infra.sudo_probe import (
    MAX_WORKERS,
    PROBE_TIMEOUT_SEC,
    probe_sudo_capability,
)

_CFG = make_config()


@contextmanager
def _stubbed_run(stub):
    """Route target-user probes through one deterministic stub."""
    with (
        mock.patch("uxon.infra.execution.run_query", side_effect=stub),
        mock.patch(
            "uxon.infra.execution.pwd.getpwnam",
            return_value=mock.Mock(pw_uid=1000, pw_gid=1000),
        ),
        mock.patch("uxon.infra.execution.os.getgrouplist", return_value=[1000]),
    ):
        yield


def _fake_completed(rc: int, *, stdout: str = "") -> subprocess.CompletedProcess:
    """Build a CompletedProcess for the stub side of ``subprocess.run``."""
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr="")


class _SudoStub:
    """Stub for ``subprocess.run`` that maps argv shape to a result.

    ``per_user`` maps each candidate user name to one of:
      - an int (returncode for the fixed target-user execution probe)
      - the sentinel string ``"timeout"`` to raise ``TimeoutExpired``
      - the sentinel string ``"oserror"`` to raise ``OSError``

    The stub records every observed argv on ``self.calls`` so tests
    can assert on flag plumbing (``-n``, ``-i``, ``-u <user>``,
    ``--``, probe module).
    """

    def __init__(
        self,
        per_user: dict[str, int | str] | None = None,
    ) -> None:
        self.per_user = dict(per_user or {})
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        # Per-target probe: argv-preserving sudo + fixed JSON probe module.
        if len(argv) >= 6 and argv[:4] == ["/usr/bin/sudo", "-n", "-H", "-u"]:
            user = argv[4]
            outcome = self.per_user.get(user, 1)
            return self._dispatch(outcome, argv, probe=True)
        raise AssertionError(f"unexpected argv to sudo stub: {argv!r}")

    @staticmethod
    def _dispatch(outcome: int | str, argv, *, probe: bool = False) -> subprocess.CompletedProcess:
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(cmd=argv, timeout=PROBE_TIMEOUT_SEC)
        if outcome == "oserror":
            raise OSError("simulated")
        if isinstance(outcome, int):
            stdout = '{"euid":1000,"egid":1000,"groups":[1000]}' if probe and outcome == 0 else ""
            return _fake_completed(outcome, stdout=stdout)
        raise AssertionError(f"bad outcome sentinel: {outcome!r}")


class ProbeReturnsCapabilitySnapshot(unittest.TestCase):
    """Smoke: the probe returns a SudoCapability with the right shape."""

    def test_empty_candidate_list_runs_no_probe(self) -> None:
        stub = _SudoStub()
        with _stubbed_run(stub):
            with mock.patch("uxon.infra.sudo_probe._self_user", return_value="vz"):
                caps = probe_sudo_capability(_CFG, [])
        self.assertIsInstance(caps, SudoCapability)
        self.assertEqual(caps.reachable_users, frozenset())
        self.assertEqual(stub.calls, [])


class ReachableUsersAreFiltered(unittest.TestCase):
    """The per-target probe controls which users land in ``reachable_users``."""

    def test_only_rc_zero_users_become_reachable(self) -> None:
        stub = _SudoStub(
            per_user={"alice_agent": 0, "bob_agent": 0, "carol_agent": 1},
        )
        with _stubbed_run(stub):
            with mock.patch("uxon.infra.sudo_probe._self_user", return_value="vz"):
                caps = probe_sudo_capability(_CFG, ["alice_agent", "bob_agent", "carol_agent"])
        self.assertEqual(caps.reachable_users, frozenset({"alice_agent", "bob_agent"}))

    def test_timeout_means_not_reachable(self) -> None:
        stub = _SudoStub(per_user={"alice_agent": "timeout", "bob_agent": 0})
        with _stubbed_run(stub):
            with mock.patch("uxon.infra.sudo_probe._self_user", return_value="vz"):
                caps = probe_sudo_capability(_CFG, ["alice_agent", "bob_agent"])
        self.assertEqual(caps.reachable_users, frozenset({"bob_agent"}))

    def test_oserror_means_not_reachable(self) -> None:
        stub = _SudoStub(per_user={"alice_agent": "oserror", "bob_agent": 0})
        with _stubbed_run(stub):
            with mock.patch("uxon.infra.sudo_probe._self_user", return_value="vz"):
                caps = probe_sudo_capability(_CFG, ["alice_agent", "bob_agent"])
        self.assertEqual(caps.reachable_users, frozenset({"bob_agent"}))


class SelfIsExcluded(unittest.TestCase):
    """``reachable_users`` never contains the running OS user."""

    def test_caller_in_candidates_is_filtered_before_probing(self) -> None:
        stub = _SudoStub(per_user={"alice_agent": 0})
        with _stubbed_run(stub):
            with mock.patch("uxon.infra.sudo_probe._self_user", return_value="vz"):
                caps = probe_sudo_capability(_CFG, ["vz", "alice_agent"])
        # No probe should have been issued for ``vz``.
        per_user_argvs = [c for c in stub.calls if c[:4] == ["/usr/bin/sudo", "-n", "-H", "-u"]]
        probed_users = {argv[4] for argv in per_user_argvs}
        self.assertEqual(probed_users, {"alice_agent"})
        self.assertEqual(caps.reachable_users, frozenset({"alice_agent"}))

    def test_duplicate_candidates_are_probed_once(self) -> None:
        stub = _SudoStub(per_user={"alice_agent": 0})
        with _stubbed_run(stub):
            with mock.patch("uxon.infra.sudo_probe._self_user", return_value="vz"):
                caps = probe_sudo_capability(_CFG, ["alice_agent", "alice_agent", "alice_agent"])
        per_user_argvs = [c for c in stub.calls if c[:4] == ["/usr/bin/sudo", "-n", "-H", "-u"]]
        self.assertEqual(len(per_user_argvs), 1)
        self.assertEqual(caps.reachable_users, frozenset({"alice_agent"}))

    def test_empty_or_blank_candidates_are_skipped(self) -> None:
        stub = _SudoStub(per_user={"alice_agent": 0})
        with _stubbed_run(stub):
            with mock.patch("uxon.infra.sudo_probe._self_user", return_value="vz"):
                caps = probe_sudo_capability(_CFG, ["", "alice_agent"])
        per_user_argvs = [c for c in stub.calls if c[:4] == ["/usr/bin/sudo", "-n", "-H", "-u"]]
        self.assertEqual(len(per_user_argvs), 1)
        self.assertEqual(caps.reachable_users, frozenset({"alice_agent"}))


class ProbeArgvShape(unittest.TestCase):
    """The exact argv shape matters — sudo is sensitive to flag order."""

    def test_per_target_probe_uses_fixed_execution_probe(self) -> None:
        stub = _SudoStub(per_user={"alice_agent": 0})
        with _stubbed_run(stub):
            with mock.patch("uxon.infra.sudo_probe._self_user", return_value="vz"):
                probe_sudo_capability(_CFG, ["alice_agent"])
        per_user_argvs = [c for c in stub.calls if c[:4] == ["/usr/bin/sudo", "-n", "-H", "-u"]]
        self.assertEqual(
            per_user_argvs,
            [
                [
                    "/usr/bin/sudo",
                    "-n",
                    "-H",
                    "-u",
                    "alice_agent",
                    "--",
                    sys.executable,
                    "-m",
                    "uxon.infra.execution_probe",
                ]
            ],
        )

    def test_probe_calls_have_finite_timeouts(self) -> None:
        captured: list[dict] = []

        def stub_run(argv, **kwargs):
            captured.append(kwargs)
            return _fake_completed(0)

        with _stubbed_run(stub_run):
            with mock.patch("uxon.infra.sudo_probe._self_user", return_value="vz"):
                probe_sudo_capability(_CFG, ["alice_agent"])

        # ``run_query`` owns terminal isolation; this layer must give every
        # probe the finite timeout that bounds startup.
        self.assertEqual(len(captured), 1)
        for kwargs in captured:
            self.assertIn("timeout", kwargs)
            self.assertEqual(kwargs["timeout"], PROBE_TIMEOUT_SEC)


class ParallelismBoundedByPool(unittest.TestCase):
    """The probe must run in parallel and not serialise N candidates."""

    def test_wall_time_under_concurrency_floor(self) -> None:
        # Stub each per-user probe to sleep 0.2s. With 8 workers in
        # the pool, 8 candidates should finish in <0.5s wall time
        # (one batch). A sequential implementation would take ~1.6s.
        per_user_sleep = 0.2

        def stub_run(argv, **kwargs):
            time.sleep(per_user_sleep)
            return _fake_completed(0, stdout='{"euid":1000,"egid":1000,"groups":[1000]}')

        candidates = [f"user{i}_agent" for i in range(MAX_WORKERS)]
        with _stubbed_run(stub_run):
            with mock.patch("uxon.infra.sudo_probe._self_user", return_value="vz"):
                t0 = time.monotonic()
                caps = probe_sudo_capability(_CFG, candidates)
                elapsed = time.monotonic() - t0

        self.assertEqual(caps.reachable_users, frozenset(candidates))
        # One batch worth of latency, with a generous safety margin
        # for slow CI: serial would be 1.6s, parallel is ~0.2-0.3s.
        self.assertLess(elapsed, 0.8)


if __name__ == "__main__":
    unittest.main()

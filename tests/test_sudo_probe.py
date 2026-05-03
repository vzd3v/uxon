"""Tests for ``uxon.sudo_probe``.

Pin the per-target sudo probe contract that the TUI superuser
block, ``uxon list --all-users``, and the multi-host aggregator
all depend on. Pure unit tests — no real ``sudo`` is invoked; the
``subprocess.run`` calls inside :mod:`uxon.sudo_probe` are stubbed
so the suite stays deterministic and fast on a CI runner with no
sudoers configuration.
"""

from __future__ import annotations

import subprocess
import time
import unittest
from unittest import mock

from uxon.sudo_probe import (
    MAX_WORKERS,
    PROBE_TIMEOUT_SEC,
    SudoCapability,
    probe_sudo_capability,
)


def _fake_completed(rc: int) -> subprocess.CompletedProcess:
    """Build a CompletedProcess for the stub side of ``subprocess.run``."""
    return subprocess.CompletedProcess(args=[], returncode=rc)


class _SudoStub:
    """Stub for ``subprocess.run`` that maps argv shape to a result.

    ``per_user`` maps each candidate user name to one of:
      - an int (returncode for ``sudo -niu <user> -- true``)
      - the sentinel string ``"timeout"`` to raise ``TimeoutExpired``
      - the sentinel string ``"oserror"`` to raise ``OSError``

    ``root`` controls the ``sudo -n true`` (no ``-u``) probe: int rc
    or one of the sentinel strings. Default rc=1 (root NOPASSWD
    unavailable) so most tests don't have to opt out explicitly.

    The stub records every observed argv on ``self.calls`` so tests
    can assert on flag plumbing (``-n``, ``-i``, ``-u <user>``,
    ``--``, ``true``).
    """

    def __init__(
        self,
        per_user: dict[str, int | str] | None = None,
        root: int | str = 1,
    ) -> None:
        self.per_user = dict(per_user or {})
        self.root = root
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        # Per-target probe: ["sudo", "-niu", "<user>", "--", "true"]
        if len(argv) >= 4 and argv[:2] == ["sudo", "-niu"] and argv[3] == "--":
            user = argv[2]
            outcome = self.per_user.get(user, 1)
            return self._dispatch(outcome, argv)
        # Root probe: ["sudo", "-n", "true"]
        if argv == ["sudo", "-n", "true"]:
            return self._dispatch(self.root, argv)
        raise AssertionError(f"unexpected argv to sudo stub: {argv!r}")

    @staticmethod
    def _dispatch(outcome: int | str, argv) -> subprocess.CompletedProcess:
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(cmd=argv, timeout=PROBE_TIMEOUT_SEC)
        if outcome == "oserror":
            raise OSError("simulated")
        if isinstance(outcome, int):
            return _fake_completed(outcome)
        raise AssertionError(f"bad outcome sentinel: {outcome!r}")


class ProbeReturnsCapabilitySnapshot(unittest.TestCase):
    """Smoke: the probe returns a SudoCapability with the right shape."""

    def test_empty_candidate_list_only_runs_root_probe(self) -> None:
        stub = _SudoStub(root=0)
        with mock.patch("uxon.sudo_probe.subprocess.run", stub):
            with mock.patch("uxon.sudo_probe._self_user", return_value="vz"):
                caps = probe_sudo_capability([])
        self.assertIsInstance(caps, SudoCapability)
        self.assertEqual(caps.reachable_users, frozenset())
        self.assertTrue(caps.can_root)
        self.assertEqual(stub.calls, [["sudo", "-n", "true"]])


class ReachableUsersAreFiltered(unittest.TestCase):
    """The per-target probe controls which users land in ``reachable_users``."""

    def test_only_rc_zero_users_become_reachable(self) -> None:
        stub = _SudoStub(
            per_user={"alice_agent": 0, "bob_agent": 0, "carol_agent": 1},
        )
        with mock.patch("uxon.sudo_probe.subprocess.run", stub):
            with mock.patch("uxon.sudo_probe._self_user", return_value="vz"):
                caps = probe_sudo_capability(
                    ["alice_agent", "bob_agent", "carol_agent"]
                )
        self.assertEqual(caps.reachable_users, frozenset({"alice_agent", "bob_agent"}))
        self.assertFalse(caps.can_root)

    def test_timeout_means_not_reachable(self) -> None:
        stub = _SudoStub(per_user={"alice_agent": "timeout", "bob_agent": 0})
        with mock.patch("uxon.sudo_probe.subprocess.run", stub):
            with mock.patch("uxon.sudo_probe._self_user", return_value="vz"):
                caps = probe_sudo_capability(["alice_agent", "bob_agent"])
        self.assertEqual(caps.reachable_users, frozenset({"bob_agent"}))

    def test_oserror_means_not_reachable(self) -> None:
        stub = _SudoStub(per_user={"alice_agent": "oserror", "bob_agent": 0})
        with mock.patch("uxon.sudo_probe.subprocess.run", stub):
            with mock.patch("uxon.sudo_probe._self_user", return_value="vz"):
                caps = probe_sudo_capability(["alice_agent", "bob_agent"])
        self.assertEqual(caps.reachable_users, frozenset({"bob_agent"}))


class SelfIsExcluded(unittest.TestCase):
    """``reachable_users`` never contains the running OS user."""

    def test_caller_in_candidates_is_filtered_before_probing(self) -> None:
        stub = _SudoStub(per_user={"alice_agent": 0})
        with mock.patch("uxon.sudo_probe.subprocess.run", stub):
            with mock.patch("uxon.sudo_probe._self_user", return_value="vz"):
                caps = probe_sudo_capability(["vz", "alice_agent"])
        # No probe should have been issued for ``vz``.
        per_user_argvs = [c for c in stub.calls if c[:2] == ["sudo", "-niu"]]
        probed_users = {argv[2] for argv in per_user_argvs}
        self.assertEqual(probed_users, {"alice_agent"})
        self.assertEqual(caps.reachable_users, frozenset({"alice_agent"}))

    def test_duplicate_candidates_are_probed_once(self) -> None:
        stub = _SudoStub(per_user={"alice_agent": 0})
        with mock.patch("uxon.sudo_probe.subprocess.run", stub):
            with mock.patch("uxon.sudo_probe._self_user", return_value="vz"):
                caps = probe_sudo_capability(
                    ["alice_agent", "alice_agent", "alice_agent"]
                )
        per_user_argvs = [c for c in stub.calls if c[:2] == ["sudo", "-niu"]]
        self.assertEqual(len(per_user_argvs), 1)
        self.assertEqual(caps.reachable_users, frozenset({"alice_agent"}))

    def test_empty_or_blank_candidates_are_skipped(self) -> None:
        stub = _SudoStub(per_user={"alice_agent": 0})
        with mock.patch("uxon.sudo_probe.subprocess.run", stub):
            with mock.patch("uxon.sudo_probe._self_user", return_value="vz"):
                caps = probe_sudo_capability(["", "alice_agent"])
        per_user_argvs = [c for c in stub.calls if c[:2] == ["sudo", "-niu"]]
        self.assertEqual(len(per_user_argvs), 1)
        self.assertEqual(caps.reachable_users, frozenset({"alice_agent"}))


class ProbeArgvShape(unittest.TestCase):
    """The exact argv shape matters — sudo is sensitive to flag order."""

    def test_per_target_probe_uses_n_i_u_dashdash_true(self) -> None:
        stub = _SudoStub(per_user={"alice_agent": 0})
        with mock.patch("uxon.sudo_probe.subprocess.run", stub):
            with mock.patch("uxon.sudo_probe._self_user", return_value="vz"):
                probe_sudo_capability(["alice_agent"])
        per_user_argvs = [c for c in stub.calls if c[:2] == ["sudo", "-niu"]]
        self.assertEqual(per_user_argvs, [["sudo", "-niu", "alice_agent", "--", "true"]])

    def test_root_probe_uses_n_true_no_dash_u(self) -> None:
        stub = _SudoStub(root=0)
        with mock.patch("uxon.sudo_probe.subprocess.run", stub):
            with mock.patch("uxon.sudo_probe._self_user", return_value="vz"):
                probe_sudo_capability([])
        self.assertIn(["sudo", "-n", "true"], stub.calls)

    def test_subprocess_run_kwargs_lock_down_io(self) -> None:
        captured: list[dict] = []

        def stub_run(argv, **kwargs):
            captured.append(kwargs)
            return _fake_completed(0)

        with mock.patch("uxon.sudo_probe.subprocess.run", stub_run):
            with mock.patch("uxon.sudo_probe._self_user", return_value="vz"):
                probe_sudo_capability(["alice_agent"])

        # Every probe must DEVNULL its stdin (so sudo cannot prompt) and
        # carry a finite timeout (so a hanging PAM module never blocks
        # startup forever).
        self.assertGreaterEqual(len(captured), 2)
        for kwargs in captured:
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
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
            return _fake_completed(0)

        candidates = [f"user{i}_agent" for i in range(MAX_WORKERS)]
        with mock.patch("uxon.sudo_probe.subprocess.run", stub_run):
            with mock.patch("uxon.sudo_probe._self_user", return_value="vz"):
                t0 = time.monotonic()
                caps = probe_sudo_capability(candidates)
                elapsed = time.monotonic() - t0

        self.assertEqual(caps.reachable_users, frozenset(candidates))
        # One batch worth of latency, with a generous safety margin
        # for slow CI: serial would be 1.6s, parallel is ~0.2-0.3s.
        self.assertLess(elapsed, 0.8)


class CanRootDecoupledFromReachable(unittest.TestCase):
    """``can_root`` is independent of ``reachable_users``."""

    def test_root_nopasswd_alone(self) -> None:
        stub = _SudoStub(per_user={}, root=0)
        with mock.patch("uxon.sudo_probe.subprocess.run", stub):
            with mock.patch("uxon.sudo_probe._self_user", return_value="vz"):
                caps = probe_sudo_capability([])
        self.assertTrue(caps.can_root)
        self.assertEqual(caps.reachable_users, frozenset())

    def test_per_target_only_no_root(self) -> None:
        stub = _SudoStub(per_user={"alice_agent": 0}, root=1)
        with mock.patch("uxon.sudo_probe.subprocess.run", stub):
            with mock.patch("uxon.sudo_probe._self_user", return_value="vz"):
                caps = probe_sudo_capability(["alice_agent"])
        self.assertFalse(caps.can_root)
        self.assertEqual(caps.reachable_users, frozenset({"alice_agent"}))

    def test_no_sudo_at_all(self) -> None:
        stub = _SudoStub(per_user={"alice_agent": 1}, root=1)
        with mock.patch("uxon.sudo_probe.subprocess.run", stub):
            with mock.patch("uxon.sudo_probe._self_user", return_value="vz"):
                caps = probe_sudo_capability(["alice_agent"])
        self.assertFalse(caps.can_root)
        self.assertEqual(caps.reachable_users, frozenset())

    def test_root_probe_short_circuits_on_uid_zero(self) -> None:
        # When the process is already root (euid==0) we don't need
        # to actually shell out for the root probe — the function
        # short-circuits to True. Per-target probes still run.
        stub = _SudoStub(per_user={"alice_agent": 0})
        with mock.patch("uxon.sudo_probe.subprocess.run", stub):
            with mock.patch("uxon.sudo_probe._self_user", return_value="root"):
                with mock.patch("uxon.sudo_probe.os.geteuid", return_value=0):
                    caps = probe_sudo_capability(["alice_agent"])
        self.assertTrue(caps.can_root)
        self.assertNotIn(["sudo", "-n", "true"], stub.calls)


if __name__ == "__main__":
    unittest.main()

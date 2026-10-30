"""Bounded concurrent target-user attestation, including failed candidates."""

from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from types import SimpleNamespace
from unittest import mock

from helpers import make_config

from uxon.infra.sudo_probe import MAX_WORKERS, probe_sudo_capability


def test_mixed_candidates_are_attested_once_and_empty_input_has_no_commands():
    calls = []
    cfg = make_config()

    def run(argv, *, timeout):
        user = argv[4]
        assert argv == [
            "/usr/bin/sudo",
            "-n",
            "-H",
            "-u",
            user,
            "--",
            sys.executable,
            "-m",
            "uxon.infra.execution_probe",
        ]
        assert timeout == cfg.execution.backend_for_user(user).probe_timeout_seconds
        calls.append(user)
        if user == "timeout":
            raise subprocess.TimeoutExpired(argv, timeout)
        if user == "unavailable":
            raise OSError("boundary unavailable")
        assert user in {"bob", "carol", "denied"}
        return subprocess.CompletedProcess(
            argv, int(user == "denied"), '{"euid":1001,"egid":1002,"groups":[1002,2000]}', ""
        )

    with (
        mock.patch(
            "uxon.infra.execution.pwd.getpwnam",
            return_value=SimpleNamespace(pw_uid=1001, pw_gid=1002),
        ),
        mock.patch("uxon.infra.execution.os.getgrouplist", return_value=[2000, 1002]),
        mock.patch("uxon.infra.sudo_probe._self_user", return_value="alice"),
        mock.patch("uxon.infra.execution.process_user", return_value="alice"),
        mock.patch("uxon.infra.execution.run_query", side_effect=run),
    ):
        assert probe_sudo_capability(cfg, []).reachable_users == frozenset()
        assert calls == []
        assert probe_sudo_capability(
            cfg, ["alice", "", "bob", "bob", "carol", "denied", "timeout", "unavailable"]
        ).reachable_users == frozenset({"bob", "carol"})
    assert sorted(calls) == ["bob", "carol", "denied", "timeout", "unavailable"]


def test_probe_pool_admits_at_most_limit_and_completes_all_candidates():
    release, admitted = Event(), Event()
    lock = Lock()
    active = peak = 0
    calls = []
    candidates = [f"user{i}" for i in range(MAX_WORKERS + 3)]

    def run(argv, *, timeout):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            calls.append(argv[4])
            if active == MAX_WORKERS:
                admitted.set()
        try:
            assert release.wait(5), "target probes did not receive release"
            return subprocess.CompletedProcess(
                argv, 0, '{"euid":1001,"egid":1002,"groups":[1002]}', ""
            )
        finally:
            with lock:
                active -= 1

    with (
        mock.patch(
            "uxon.infra.execution.pwd.getpwnam",
            return_value=SimpleNamespace(pw_uid=1001, pw_gid=1002),
        ),
        mock.patch("uxon.infra.execution.os.getgrouplist", return_value=[1002]),
        mock.patch("uxon.infra.sudo_probe._self_user", return_value="alice"),
        mock.patch("uxon.infra.execution.process_user", return_value="alice"),
        mock.patch("uxon.infra.execution.run_query", side_effect=run),
        ThreadPoolExecutor(max_workers=1) as driver,
    ):
        result = driver.submit(probe_sudo_capability, make_config(), candidates)
        try:
            assert admitted.wait(5), "probes serialized before pool reached its admission limit"
            with lock:
                assert active == MAX_WORKERS and len(calls) == MAX_WORKERS
                assert peak == MAX_WORKERS
        finally:
            release.set()
        assert result.result(timeout=5).reachable_users == frozenset(candidates)
    assert sorted(calls) == sorted(candidates) and peak == MAX_WORKERS and active == 0

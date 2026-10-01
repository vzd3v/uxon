"""Real Linux workload identity and teardown regressions (opt-in)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from uxon.domain.runtime import runtime_pidfile, wrap_agent_for_runtime

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not hasattr(os, "pidfd_open"), reason="Linux pidfd support required"),
]
STOP_HELPER = Path(__file__).resolve().parents[1] / "install" / "runtime_stop.py"


def _launch(pidfile: Path, nonce: str) -> subprocess.Popen:
    return subprocess.Popen(
        wrap_agent_for_runtime(
            ["sleep", "30"], session="uxon-same@claude", launch_nonce=nonce, pidfile=str(pidfile)
        )
    )


def _wait_record(pidfile: Path, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if pidfile.exists():
            fields = pidfile.read_text().split()
            if len(fields) == 2 and all(field.isdecimal() for field in fields):
                pid, ticks = map(int, fields)
                assert pid == process.pid and ticks > 0
                # Shell exports enter procfs environ only after exec. Observe
                # the workload itself, not the telemetry result under test.
                if Path(f"/proc/{pid}/cmdline").read_bytes() == b"sleep\00030\000":
                    return
        assert process.poll() is None, "workload exited before recording its identity"
        time.sleep(0.005)
    pytest.fail("workload did not record its identity and execute sleep")


def _stop(pidfile: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(STOP_HELPER), str(pidfile), "--timeout", "1"],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )


def test_same_named_workloads_stop_independently(tmp_path: Path) -> None:
    nonces = ("a" * 32, "b" * 32)
    records = [tmp_path / Path(runtime_pidfile(nonce)).name for nonce in nonces]
    processes = [_launch(record, nonce) for record, nonce in zip(records, nonces, strict=True)]
    try:
        for record, process in zip(records, processes, strict=True):
            _wait_record(record, process)
            pid, ticks = map(int, record.read_text().split())
            assert pid == process.pid and ticks > 0
            assert record.stat().st_mode & 0o777 == 0o600
        from uxon.infra.runtime_telemetry_probe import session_markers

        assert session_markers([p.pid for p in processes])["markers"] == {
            str(p.pid): nonce for p, nonce in zip(processes, nonces, strict=True)
        }
        assert _stop(records[0]).returncode == 0
        assert processes[0].wait(timeout=1) < 0
        assert processes[1].poll() is None
        assert records[1].exists() and not records[0].exists()
        assert _stop(records[1]).returncode == 0
        processes[1].wait(timeout=1)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=2)


@pytest.mark.parametrize("invalid", ["missing", "reused", "fifo", "symlink"])
def test_invalid_workload_record_never_signals_live_process(tmp_path: Path, invalid: str) -> None:
    record = tmp_path / "workload.pid"
    process = _launch(record, "c" * 32)
    try:
        _wait_record(record, process)
        if invalid == "reused":
            pid, ticks = map(int, record.read_text().split())
            record.write_text(f"{pid} {ticks + 1}\n")
        else:
            record.unlink()
            if invalid == "fifo":
                os.mkfifo(record)
            elif invalid == "symlink":
                record.symlink_to(tmp_path / "missing")
        stopped = _stop(record)
        assert stopped.returncode != 0, stopped.stdout
        assert "workload stop failed" in stopped.stderr
        assert process.poll() is None
        if invalid == "reused":
            assert "reused" in stopped.stderr
            assert record.exists()
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=2)

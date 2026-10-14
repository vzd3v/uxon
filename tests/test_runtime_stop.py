"""Real Linux workload identity and teardown regressions (opt-in)."""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

from uxon.domain.runtime import runtime_pidfile, wrap_agent_for_runtime

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not hasattr(os, "pidfd_open"), reason="Linux pidfd support required"),
]
STOP_HELPER = Path(__file__).resolve().parents[1] / "install" / "runtime_stop.py"


def test_wrapper_exec_preserves_hostile_argv_environment_and_optional_record(tmp_path, subtests):
    session = "uxon-space '; $value@claude"
    argv = ["space arg", ";touch should-not-exist", "$HOME", "'quoted'"]
    program = "import os,sys,json,pathlib; print(json.dumps(dict(argv=sys.argv[1:],session=os.environ['UXON_SESSION'],nonce=os.environ['UXON_LAUNCH_NONCE'],pid=os.getpid(),ticks=int(pathlib.Path('/proc/self/stat').read_text().rsplit(') ',1)[1].split()[19]))))"
    for record_enabled in (False, True):
        with subtests.test(record_enabled=record_enabled):
            record = tmp_path / ("with.pid" if record_enabled else "without.pid")
            result = subprocess.run(
                wrap_agent_for_runtime(
                    [sys.executable, "-c", program, *argv],
                    session=session,
                    launch_nonce="e" * 32,
                    pidfile=str(record) if record_enabled else None,
                ),
                capture_output=True,
                text=True,
                timeout=3,
                check=True,
            )
            actual = json.loads(result.stdout)
            assert (actual["argv"], actual["session"], actual["nonce"]) == (argv, session, "e" * 32)
            assert record.exists() == record_enabled
            if record_enabled:
                assert list(map(int, record.read_text().split())) == [
                    actual["pid"],
                    actual["ticks"],
                ]
                assert record.stat().st_mode & 0o777 == 0o600


def _launch(pidfile: Path, nonce: str) -> subprocess.Popen:
    return subprocess.Popen(
        wrap_agent_for_runtime(
            ["sleep", "30"], session="uxon-same@claude", launch_nonce=nonce, pidfile=str(pidfile)
        )
    )


def _wait_record(pidfile: Path, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 5
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


def test_invalid_workload_record_never_signals_live_process(tmp_path: Path, subtests) -> None:
    record = tmp_path / "workload.pid"
    process = _launch(record, "c" * 32)
    try:
        _wait_record(record, process)
        saved = record.read_bytes()
        victim = tmp_path / "valid-victim.pid"
        victim.write_bytes(saved)
        victim.chmod(0o600)
        for invalid in ("missing", "reused", "fifo", "symlink"):
            with subtests.test(invalid=invalid):
                record.unlink(missing_ok=True)
                if invalid == "reused":
                    pid, ticks = map(int, saved.split())
                    record.write_text(f"{pid} {ticks + 1}\n")
                elif invalid == "fifo":
                    os.mkfifo(record)
                elif invalid == "symlink":
                    record.symlink_to(victim)
                stopped = _stop(record)
                assert stopped.returncode != 0, stopped.stdout
                assert "workload stop failed" in stopped.stderr
                assert process.poll() is None
                assert victim.read_bytes() == saved
                if invalid == "reused":
                    assert "reused" in stopped.stderr
                    assert record.exists()
                if invalid == "symlink":
                    assert record.is_symlink()
        record.unlink()
        record.write_bytes(saved)
        record.chmod(0o600)
        spec = importlib.util.spec_from_file_location("runtime_stopper", STOP_HELPER)
        stopper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(stopper)
        handles = []
        real_open = os.pidfd_open

        def open_handle(pid):
            handle = real_open(pid)
            handles.append(handle)
            return handle

        with (
            mock.patch.object(os, "pidfd_open", side_effect=open_handle) as opened,
            mock.patch.object(signal, "pidfd_send_signal", wraps=signal.pidfd_send_signal) as sent,
            mock.patch.object(
                os, "kill", side_effect=AssertionError("numeric PID signal is unsafe")
            ),
        ):
            stopper.stop_workload(record, 1)
        opened.assert_called_once_with(process.pid)
        sent.assert_called_once_with(handles[0], signal.SIGTERM)
        assert process.wait(timeout=2) < 0 and not record.exists()
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=2)


def test_stop_timeout_preserves_live_child_and_record(tmp_path: Path) -> None:
    record, ready = tmp_path / "resistant.pid", tmp_path / "ready"
    program = "import signal,time,pathlib,sys; signal.signal(signal.SIGTERM,signal.SIG_IGN); pathlib.Path(sys.argv[1]).touch(); time.sleep(30)"
    process = subprocess.Popen(
        wrap_agent_for_runtime(
            [sys.executable, "-c", program, str(ready)],
            session="uxon-resistant@claude",
            launch_nonce="d" * 32,
            pidfile=str(record),
        )
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists():
            assert process.poll() is None
            assert time.monotonic() < deadline, "child never installed TERM handler"
            time.sleep(0.005)
        saved = record.read_bytes()
        result = subprocess.run(
            [sys.executable, str(STOP_HELPER), str(record), "--timeout", "0.05"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        assert result.returncode == 1 and "did not exit after TERM" in result.stderr
        assert process.poll() is None and record.read_bytes() == saved
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)

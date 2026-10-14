# SPDX-License-Identifier: MIT
"""Fixtures for the opt-in real-runtime container suite.

These tests prove the two things a mocked subprocess boundary cannot:
that the agent command actually runs *inside* the container, and that
ending the tmux session reaps the in-container process. Everything here
is data-free and universal — a stock minimal base image plus a tiny
bind-mounted stub stands in for a real agent, so no project image is
ever built.

The suite is gated three ways so it never slows the normal test run:

* every test carries the ``container`` marker, deselected by default in
  ``pyproject.toml`` alongside the separate ``slow`` suite;
* the ``runtime`` fixture is parametrized over ``docker`` and ``podman``
  and skips unavailable optional runtimes; ``UXON_TEST_REQUIRE_DOCKER=1``
  makes missing or unreachable Docker a hard failure in CI;
* the fixtures own teardown of the containers *these tests* create —
  including their uniquely named Compose networks. User containers are
  never in scope.

Run them explicitly with a working runtime::

    pytest -m container
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

# Stock minimal base image. Pulled once on first run (a pull, never a
# build); small enough that the one-time cost is negligible.
BASE_IMAGE = "docker.io/library/python:3.11-alpine"
STOP_HELPER = Path(__file__).resolve().parents[3] / "install" / "runtime_stop.py"

# How long a runtime probe / lifecycle command may take before the test
# treats the runtime as unusable and skips. Keeps a wedged daemon from
# hanging the suite.
PROBE_TIMEOUT_SEC = 20.0

RUNTIMES = ["docker", "podman"]


def _runtime_usable(binary: str) -> bool:
    """True iff the engine and its Compose provider are available.

    Both conditions matter: a host can ship the client without a running
    daemon (or with a daemon it cannot reach). The fixture decides whether
    absence is optional or a required-gate failure. ``info`` is the cheapest call that
    actually round-trips to the daemon.
    """
    if shutil.which(binary) is None:
        return False
    try:
        for argv in ([binary, "info"], [binary, "compose", "version"]):
            cp = subprocess.run(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                timeout=PROBE_TIMEOUT_SEC,
            )
            if cp.returncode != 0:
                return False
    except (OSError, subprocess.TimeoutExpired):
        return False
    return cp.returncode == 0


@pytest.fixture(scope="session")
def runtime_availability() -> dict[str, bool]:
    """Cache only engine/provider checks; every test owns fresh resources."""
    return {binary: _runtime_usable(binary) for binary in RUNTIMES}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    report = (yield).get_result()
    setattr(item, f"runtime_report_{report.when}", report)


@dataclass(frozen=True)
class Runtime:
    """A usable container runtime plus the names this test owns.

    ``binary`` is ``docker`` or ``podman``; ``runtime_name`` is a
    process/uuid-suffixed unique name so a crashed prior run can never
    block a rerun and parallel runs never collide.
    """

    binary: str
    runtime_name: str


@pytest.fixture(params=RUNTIMES)
def runtime(request: pytest.FixtureRequest, runtime_availability: dict[str, bool]) -> Runtime:
    """Yield a usable runtime, or skip this parameter if none is reachable.

    Owns teardown of the container these tests start under the unique
    name — a best-effort ``rm -f`` so nothing the suite created survives,
    even on failure. A user's own container is never in scope here.
    """
    binary = request.param
    if not runtime_availability[binary]:
        if binary == "docker" and os.environ.get("UXON_TEST_REQUIRE_DOCKER") == "1":
            pytest.fail("Docker and Compose are required but unavailable")
        pytest.skip(f"{binary}: engine or Compose provider unavailable")
    # Unique per run: pid + a uuid shard. Stays within the runtime name
    # charset (leading alnum, then alnum/dash/underscore/dot).
    name = f"uxon-it-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    rt = Runtime(binary=binary, runtime_name=name)

    def cleanup():
        errors = _teardown(rt)
        if errors:
            detail = "\n".join(errors)
            request.node.add_report_section("teardown", "runtime cleanup", detail)
            failed = any(
                getattr(request.node, f"runtime_report_{phase}", None) is not None
                and getattr(request.node, f"runtime_report_{phase}").failed
                for phase in ("setup", "call")
            )
            if not failed:
                pytest.fail(detail)

    request.addfinalizer(cleanup)
    return rt


@pytest.fixture(autouse=True)
def _isolated_controller_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep authoritative launch records out of the controller's live state."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def _teardown(rt: Runtime) -> list[str]:
    """Remove anything the suite created for ``rt`` (idempotent, quiet)."""
    errors = []
    for argv in (
        [rt.binary, "rm", "-f", rt.runtime_name],
        [rt.binary, "network", "rm", rt.runtime_name + "_default"],
    ):
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=PROBE_TIMEOUT_SEC,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            errors.append(f"{argv[1:]}: {type(error).__name__}")
            continue
        if result.returncode and not any(
            text in result.stderr.lower() for text in ("not found", "no such")
        ):
            errors.append(f"{argv[1:]}: rc={result.returncode}: {result.stderr.strip()}")
    return errors


# A stand-in agent for the telemetry suite that selects idle vs busy from its
# first argv token (the agent args uxon threads through after the binary). A
# ``busy`` agent spins so its per-session CPU is unmistakably non-idle; any
# other arg idles. The ``$$`` (the exec'd PID) is what the cgroup attribution +
# the UXON_LAUNCH_NONCE environ split must see — proving per-session isolation.
STUB_AGENT_SELECTABLE = """\
#!/bin/sh
# Telemetry stand-in: spin (busy) or idle, selected by the first agent arg.
touch /work/.agent-ran
if [ "$1" = "busy" ]; then
  while :; do :; done
fi
exec python3 -c 'import time; payload = bytearray(2 * 1024 * 1024); time.sleep(300)'
"""

# Stock base + bind mount only — no build. Exercises the
# create_command = ["<runtime>", "compose", "up", "-d"] path. PID 1 idles
# on ``tail`` so ``<runtime> top`` distinguishes container liveness from
# workload liveness; the container stays up across a workload stop.
COMPOSE_TEMPLATE = (
    """\
services:
  agent:
    image: {image}
    container_name: {name}
    user: "{uid}:{gid}"
    command: ["tail", "-f", "/dev/null"]
    volumes:
      - {project_dir}:/work
      - {stub_path}:/usr/local/bin/claude:ro
      - {stopper_path}:/usr/local/libexec/uxon-runtime-stop.py:ro
    working_dir: /work
""".replace("{uid}", str(os.getuid()))
    .replace("{gid}", str(os.getgid()))
    .replace("{stopper_path}", str(STOP_HELPER))
)


def operator_runtime_table(rt: Runtime, project_dir: Path) -> dict[str, object]:
    """A complete operator-owned command runtime using the current schema."""
    return {
        "kind": "command",
        "resource_scope": "per_user",
        "resource_name_template": rt.runtime_name,
        "path_map": {str(project_dir): "/work"},
        "exec_prefix": [rt.binary, "exec", "-i", "-w", "{runtime_dir}", "{resource}"],
        "telemetry": "cgroup",
        "readiness": {
            "ready_command": [rt.binary, "top", "{resource}"],
            "exists_command": [rt.binary, "container", "inspect", "{resource}"],
            "start_command": [rt.binary, "start", "{resource}"],
            "create_command": [rt.binary, "compose", "-p", "{resource}", "up", "-d"],
            "on_missing": "create",
            "approval": "auto",
        },
        "identity": {
            "resolve_command": [
                rt.binary,
                "inspect",
                "--format",
                '{{"id":"{{{{.Id}}}}","host_pid":{{{{.State.Pid}}}},"epoch":"{{{{.State.StartedAt}}}}"}}',
                "{resource}",
            ],
        },
        "session": {
            "stop_command": [
                rt.binary,
                "exec",
                "{resource}",
                "python3",
                "/usr/local/libexec/uxon-runtime-stop.py",
                "{pidfile}",
                "--timeout",
                "5",
            ],
        },
        "timeouts": {"stop_seconds": 10.0},
    }

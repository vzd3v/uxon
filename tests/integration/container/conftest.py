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
  ``pyproject.toml`` (``-m 'not container'``);
* the ``runtime`` fixture is parametrized over ``docker`` and ``podman``
  and skips each independently when its binary is missing **or** its
  daemon is unreachable;
* the fixtures own teardown of the containers *these tests* create —
  they never touch a user's own project container.

Run them explicitly with a working runtime::

    pytest -m container
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

# Stock minimal base image. Pulled once on first run (a pull, never a
# build); small enough that the one-time cost is negligible.
BASE_IMAGE = "docker.io/library/alpine:3.20"

# How long a runtime probe / lifecycle command may take before the test
# treats the runtime as unusable and skips. Keeps a wedged daemon from
# hanging the suite.
PROBE_TIMEOUT_SEC = 20.0

RUNTIMES = ["docker", "podman"]


def _runtime_usable(binary: str) -> bool:
    """True iff ``binary`` is on PATH and its daemon answers ``info``.

    Both conditions matter: a host can ship the client without a running
    daemon (or with a daemon it cannot reach), in which case the suite
    must skip rather than error. ``info`` is the cheapest call that
    actually round-trips to the daemon.
    """
    if shutil.which(binary) is None:
        return False
    try:
        cp = subprocess.run(
            [binary, "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=PROBE_TIMEOUT_SEC,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return cp.returncode == 0


@dataclass(frozen=True)
class Runtime:
    """A usable container runtime plus the names this test owns.

    ``binary`` is ``docker`` or ``podman``; ``container_name`` is a
    process/uuid-suffixed unique name so a crashed prior run can never
    block a rerun and parallel runs never collide.
    """

    binary: str
    container_name: str


@pytest.fixture(params=RUNTIMES)
def runtime(request: pytest.FixtureRequest) -> Iterator[Runtime]:
    """Yield a usable runtime, or skip this parameter if none is reachable.

    Owns teardown of the container these tests start under the unique
    name — a best-effort ``rm -f`` so nothing the suite created survives,
    even on failure. A user's own container is never in scope here.
    """
    binary = request.param
    if not _runtime_usable(binary):
        pytest.skip(f"{binary}: binary absent or daemon unreachable")
    # Unique per run: pid + a uuid shard. Stays within the runtime name
    # charset (leading alnum, then alnum/dash/underscore/dot).
    name = f"uxon-it-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    rt = Runtime(binary=binary, container_name=name)
    try:
        yield rt
    finally:
        _teardown(rt)


def _teardown(rt: Runtime) -> None:
    """Remove anything the suite created for ``rt`` (idempotent, quiet)."""
    for argv in ([rt.binary, "rm", "-f", rt.container_name],):
        subprocess.run(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=PROBE_TIMEOUT_SEC,
            check=False,
        )


# Token the agent's idle process carries so ``<runtime> top`` can tell it
# apart from the container's own PID 1 (which idles on ``tail`` below).
# A reap test must distinguish the agent from the still-alive container.
AGENT_SENTINEL = "sleep"

# A ~5-line stand-in for a real agent: record that it ran (so the test
# can prove in-container exec), then idle on ``sleep`` so the session has
# a live, identifiable process to reap. Any agent flags are ignored.
STUB_AGENT = """\
#!/bin/sh
# Minimal stand-in for an AI agent binary used by the container test
# suite: prove the command ran inside the container, then idle.
touch /work/.agent-ran
exec sleep 300
"""

# Stock base + bind mount only — no build. Exercises the
# create_template = ["<runtime>", "compose", "up", "-d"] path. PID 1 idles
# on ``tail`` (not ``sleep``) so the agent's ``sleep`` is unambiguous in
# ``<runtime> top`` — the container stays up across the agent's reap.
COMPOSE_TEMPLATE = """\
services:
  agent:
    image: {image}
    container_name: {name}
    command: ["tail", "-f", "/dev/null"]
    volumes:
      - {project_dir}:/work
      - {stub_path}:/usr/local/bin/claude:ro
    working_dir: /work
"""


def write_project(project_dir: Path, container_name: str) -> Path:
    """Materialize the synthetic project: stub agent + project .uxon.toml.

    Returns the stub path (bind-mounted to ``/usr/local/bin/claude``).
    The ``.uxon.toml`` carries only the unique container name — the data
    a project layer is trusted to supply — mirroring how an operator
    would pin a per-project container.
    """
    stub = project_dir / "claude-stub"
    stub.write_text(STUB_AGENT)
    stub.chmod(0o755)
    (project_dir / ".uxon.toml").write_text(f'[container]\nname = "{container_name}"\n')
    return stub

# SPDX-License-Identifier: MIT
"""Subprocess adapter: run an external command and translate failure.

Thin wrapper over :func:`uxon.infra.run.run_query` (the single sanctioned
background-spawn primitive) that converts a non-zero exit into the uxon
``fail() -> SystemExit`` convention. Lives in the infra layer because it
shells out; pure callers never touch it.
"""

from __future__ import annotations

import shlex
import subprocess

from uxon.errors import fail
from uxon.infra.run import run_query


def run_cmd(
    cmd: list[str], check: bool = True, *, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    # Always spawn with ``check=False`` and translate here: ``run_cmd``'s
    # ``fail() -> SystemExit`` contract is what its callers expect, not the
    # ``CalledProcessError`` that ``run_query(check=True)`` would raise.
    try:
        cp = run_query(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        fail(f"command timed out after {timeout:g}s: {shlex.join(cmd)}", 1)
        raise AssertionError("unreachable") from None
    if check and cp.returncode != 0:
        stderr = (cp.stderr or cp.stdout).strip()
        fail(stderr or f"command failed: {shlex.join(cmd)}", 1)
    return cp

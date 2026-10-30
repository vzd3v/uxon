# SPDX-License-Identifier: MIT
"""Subprocess adapter: run an external command and translate failure.

Thin wrapper over :func:`uxon.infra.run.run_query` (the single sanctioned
background-spawn primitive) that converts a non-zero exit into the uxon
``fail() -> SystemExit`` convention. Lives in the infra layer because it
shells out; pure callers never touch it.
"""

from __future__ import annotations

import subprocess

from uxon.errors import fail
from uxon.infra.run import run_query

_DIAGNOSTIC_LIMIT = 512


def _diagnostic(stderr: str | bytes | None, stdout: str | bytes | None = None) -> str:
    value = stderr or stdout or ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return value.strip()[-_DIAGNOSTIC_LIMIT:]


def run_cmd(
    cmd: list[str], check: bool = True, *, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a control command, retaining its bounded diagnostic but never argv.

    Interactive workload output belongs to the terminal, not this adapter.
    """
    # Always spawn with ``check=False`` and translate here: ``run_cmd``'s
    # ``fail() -> SystemExit`` contract is what its callers expect, not the
    # ``CalledProcessError`` that ``run_query(check=True)`` would raise.
    try:
        cp = run_query(cmd, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        detail = _diagnostic(exc.stderr)
        fail(f"command timed out after {exc.timeout:g}s" + (f": {detail}" if detail else ""), 1)
        raise AssertionError("unreachable") from None
    if check and cp.returncode != 0:
        detail = _diagnostic(cp.stderr, cp.stdout)
        fail(f"command exited with status {cp.returncode}" + (f": {detail}" if detail else ""), 1)
    return cp

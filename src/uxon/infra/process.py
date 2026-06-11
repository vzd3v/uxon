# SPDX-License-Identifier: MIT
"""Subprocess adapter: run an external command and translate failure.

Thin wrapper over :func:`subprocess.run` that converts a non-zero exit
into the uxon ``fail() -> SystemExit`` convention. Lives in the infra
layer because it shells out; pure callers never touch it.
"""

from __future__ import annotations

import shlex
import subprocess

from uxon.errors import fail


def run_cmd(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    cp = subprocess.run(cmd, text=True, capture_output=True)
    if check and cp.returncode != 0:
        stderr = (cp.stderr or cp.stdout).strip()
        fail(stderr or f"command failed: {shlex.join(cmd)}", 1)
    return cp

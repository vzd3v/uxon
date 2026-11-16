"""Bounded executable diagnostics for explicitly requested doctor checks."""

from __future__ import annotations

import subprocess

from uxon.domain.host_report import AgentAvailability as AgentAvailability
from uxon.infra.execution import ExecutionConfigured, command_prefix
from uxon.infra.run import run_query


def probe_agent(
    cfg: ExecutionConfigured,
    binary: str,
    launch_user: str,
    *,
    version_args: tuple[str, ...],
    timeout_seconds: float,
) -> AgentAvailability:
    """Execute the configured version command through the launch user's backend.

    Presence is checked separately. A present wrapper can fail to execute its
    target; retain that failure instead of reporting a missing installation.
    A timeout establishes neither success nor failure.
    """
    cmd = command_prefix(cfg, launch_user, interactive=False) + [binary, *version_args]
    try:
        cp = run_query(cmd, timeout=timeout_seconds)
    except OSError as exc:
        return AgentAvailability(status="error", error=str(exc))
    except subprocess.TimeoutExpired:
        return AgentAvailability(status="timeout")
    if cp.returncode != 0:
        err = (cp.stderr or cp.stdout or "").strip()
        return AgentAvailability(
            status="error", error=(err or f"exit {cp.returncode}")[:512], returncode=cp.returncode
        )
    lines = (cp.stdout or "").strip().splitlines()
    return AgentAvailability(status="ok", version=lines[0] if lines else None)

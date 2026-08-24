# SPDX-License-Identifier: MIT
"""Structured workload telemetry queries through the execution backend."""

from __future__ import annotations

import json
import subprocess
import sys

from uxon.domain.config import Config
from uxon.infra.execution import wrap_command
from uxon.infra.run import run_query
from uxon.infra.runtime import RUNTIME_CMD_TIMEOUT_SEC


def _query(cfg: Config, user: str, args: list[str]) -> tuple[dict[str, object] | None, int]:
    cmd = wrap_command(
        cfg,
        user,
        [sys.executable, "-m", "uxon.infra.runtime_telemetry_probe", *args],
        interactive=False,
    )
    try:
        cp = run_query(cmd, timeout=RUNTIME_CMD_TIMEOUT_SEC)
        payload = json.loads(cp.stdout)
    except (OSError, subprocess.TimeoutExpired, TypeError, json.JSONDecodeError):
        return None, 1
    return payload if isinstance(payload, dict) else None, cp.returncode


def read_process_cgroup(cfg: Config, user: str, pid: int) -> str:
    payload, returncode = _query(cfg, user, ["--mode", "process-cgroup", "--pid", str(pid)])
    cgroup = payload.get("cgroup") if payload is not None else None
    if (
        returncode != 0
        or payload is None
        or set(payload) != {"ok", "cgroup", "error"}
        or payload.get("ok") is not True
        or not isinstance(cgroup, str)
        or not isinstance(payload.get("error"), str)
    ):
        return ""
    return cgroup


def read_cgroup_members(cfg: Config, user: str, cgroup: str) -> list[int]:
    payload, returncode = _query(cfg, user, ["--mode", "cgroup-members", "--cgroup", cgroup])
    pids = payload.get("pids") if payload is not None else None
    if (
        returncode != 0
        or payload is None
        or set(payload) != {"ok", "pids", "error"}
        or payload.get("ok") is not True
        or not isinstance(pids, list)
        or not all(isinstance(pid, int) and not isinstance(pid, bool) for pid in pids)
        or not isinstance(payload.get("error"), str)
    ):
        return []
    return pids


def read_session_markers(cfg: Config, user: str, pids: list[int]) -> dict[int, str] | None:
    args = ["--mode", "markers"]
    for pid in pids:
        args += ["--pid", str(pid)]
    payload, returncode = _query(cfg, user, args)
    markers = payload.get("markers") if payload is not None else None
    if (
        returncode != 0
        or payload is None
        or set(payload) != {"ok", "markers", "error"}
        or payload.get("ok") is not True
        or not isinstance(markers, dict)
        or not isinstance(payload.get("error"), str)
    ):
        return None
    result: dict[int, str] = {}
    for pid, session in markers.items():
        if not isinstance(pid, str) or not pid.isdecimal() or not isinstance(session, str):
            return None
        result[int(pid)] = session
    return result

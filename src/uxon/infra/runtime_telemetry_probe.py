# SPDX-License-Identifier: MIT
"""Fixed structured probe for workload telemetry inside an execution backend."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from uxon.domain.runtime_usage import parse_cgroup_procs
from uxon.infra.runtime import parse_proc_cgroup


def process_cgroup(pid: int) -> dict[str, object]:
    try:
        content = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "cgroup": "", "error": str(exc)}
    return {"ok": True, "cgroup": parse_proc_cgroup(content), "error": ""}


def cgroup_members(cgroup: str) -> dict[str, object]:
    if not cgroup.startswith("/") or os.path.normpath(cgroup) != cgroup:
        return {"ok": False, "pids": [], "error": "invalid cgroup path"}
    try:
        content = (Path("/sys/fs/cgroup") / cgroup.lstrip("/") / "cgroup.procs").read_text(
            encoding="utf-8"
        )
    except OSError as exc:
        return {"ok": False, "pids": [], "error": str(exc)}
    return {"ok": True, "pids": parse_cgroup_procs(content), "error": ""}


def session_markers(pids: list[int]) -> dict[str, object]:
    markers: dict[str, str] = {}
    for pid in pids:
        try:
            raw = Path(f"/proc/{pid}/environ").read_bytes()
        except OSError:
            continue
        for entry in raw.split(b"\0"):
            if entry.startswith(b"UXON_SESSION="):
                markers[str(pid)] = entry.removeprefix(b"UXON_SESSION=").decode(
                    "utf-8", errors="replace"
                )
                break
    return {"ok": True, "markers": markers, "error": ""}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="uxon runtime-telemetry-probe")
    parser.add_argument("--mode", choices=("process-cgroup", "cgroup-members", "markers"))
    parser.add_argument("--pid", action="append", type=int, default=[])
    parser.add_argument("--cgroup", default="")
    ns = parser.parse_args(argv)
    if ns.mode == "process-cgroup" and len(ns.pid) == 1:
        payload = process_cgroup(ns.pid[0])
    elif ns.mode == "cgroup-members" and ns.cgroup:
        payload = cgroup_members(ns.cgroup)
    elif ns.mode == "markers":
        payload = session_markers(ns.pid)
    else:
        payload = {"ok": False, "error": "invalid probe arguments"}
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

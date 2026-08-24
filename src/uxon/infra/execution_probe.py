# SPDX-License-Identifier: MIT
"""Fixed probe executed inside a command execution backend."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any


def collect(state_dir: Path, sentinel: Path) -> dict[str, Any]:
    state_stat = os.stat(state_dir, follow_symlinks=False)
    digest = hashlib.sha256(sentinel.read_bytes()).hexdigest()
    namespaces = {
        name: os.stat(f"/proc/self/ns/{name}").st_ino
        for name in ("mnt", "uts", "ipc", "net", "pid")
    }
    status: dict[str, str] = {}
    for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition(":")
        if separator:
            status[key] = value.strip()
    capabilities = {
        key: status.get(key, "") for key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
    }
    return {
        "ok": True,
        "identity": {
            "euid": os.geteuid(),
            "egid": os.getegid(),
            "groups": sorted(os.getgroups()),
            "namespaces": namespaces,
            "cgroup": Path("/proc/self/cgroup").read_text(encoding="utf-8").strip(),
            "capabilities": capabilities,
            "no_new_privs": int(status.get("NoNewPrivs", "-1")),
            "state_dev": state_stat.st_dev,
            "state_ino": state_stat.st_ino,
            "state_writable": os.access(state_dir, os.W_OK),
        },
        "sentinel_sha256": digest,
        "sentinel_writable": os.access(sentinel, os.W_OK),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="uxon execution-probe")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--sentinel", required=True)
    ns = parser.parse_args(argv)
    try:
        result = collect(Path(ns.state_dir), Path(ns.sentinel))
    except (OSError, ValueError) as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

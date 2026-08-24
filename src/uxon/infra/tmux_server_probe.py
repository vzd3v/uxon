# SPDX-License-Identifier: MIT
"""Fixed tmux socket probe executed inside an execution backend."""

from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path
from typing import Any

from uxon.infra.run import run_query


def collect(socket_path: Path) -> dict[str, Any]:
    try:
        metadata = os.stat(socket_path, follow_symlinks=False)
    except FileNotFoundError:
        return {"state": "absent", "sessions": [], "error": ""}
    except OSError as exc:
        return {
            "state": "unreachable",
            "sessions": [],
            "error": f"cannot inspect tmux socket: {exc}",
        }
    if not stat.S_ISSOCK(metadata.st_mode):
        return {
            "state": "unreachable",
            "sessions": [],
            "error": "tmux socket path is not a socket",
        }
    try:
        result = run_query(
            ["tmux", "-S", str(socket_path), "list-sessions", "-F", "#{session_name}"],
        )
    except OSError as exc:
        return {
            "state": "unreachable",
            "sessions": [],
            "error": f"cannot execute tmux: {exc}",
        }
    if result.returncode != 0:
        try:
            os.stat(socket_path, follow_symlinks=False)
        except FileNotFoundError:
            return {"state": "absent", "sessions": [], "error": ""}
        except OSError as exc:
            return {
                "state": "unreachable",
                "sessions": [],
                "error": f"cannot recheck tmux socket: {exc}",
            }
        detail = (result.stderr or result.stdout or f"tmux exited {result.returncode}").strip()
        return {"state": "unreachable", "sessions": [], "error": detail}
    sessions = sorted(line for line in result.stdout.splitlines() if line)
    return {"state": "running", "sessions": sessions, "error": ""}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="uxon tmux-server-probe")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--socket")
    target.add_argument("--default-socket", action="store_true")
    ns = parser.parse_args(argv)
    socket_path = (
        Path(ns.socket)
        if ns.socket
        else Path(os.environ.get("TMUX_TMPDIR", "/tmp")) / f"tmux-{os.geteuid()}" / "default"
    )
    payload = collect(socket_path)
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

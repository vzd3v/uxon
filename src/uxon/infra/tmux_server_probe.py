# SPDX-License-Identifier: MIT
"""Fixed tmux socket probe executed inside an execution backend."""

from __future__ import annotations

import argparse
import errno
import json
import os
import socket
import stat
import subprocess
from pathlib import Path
from typing import Any

from uxon.infra.run import run_query
from uxon.infra.tmux_socket import (
    SocketDirectoryAbsentError,
    SocketDirectoryError,
    open_socket_parent,
)

_SOCKET_CONNECT_TIMEOUT_SECONDS = 0.25
_TMUX_QUERY_TIMEOUT_SECONDS = 2.0
_PRIVATE_SOCKET_MODES = frozenset({0o600, 0o700})


def _socket_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_gid,
        stat.S_IFMT(metadata.st_mode),
        metadata.st_ctime_ns,
    )


def _inspect_socket(directory_fd: int, leaf: str) -> os.stat_result:
    metadata = os.stat(leaf, dir_fd=directory_fd, follow_symlinks=False)
    if not stat.S_ISSOCK(metadata.st_mode):
        raise SocketDirectoryError("tmux socket path is not a socket")
    if metadata.st_uid != os.geteuid():
        raise SocketDirectoryError("tmux socket is not owned by the launch user")
    if stat.S_IMODE(metadata.st_mode) not in _PRIVATE_SOCKET_MODES:
        raise SocketDirectoryError("tmux socket permissions must be owner-only (0600 or 0700)")
    return metadata


def _failed_query_state(
    socket_path: Path,
    directory_fd: int,
    leaf: str,
    original: os.stat_result,
) -> str:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(_SOCKET_CONNECT_TIMEOUT_SECONDS)
    try:
        try:
            client.connect(str(socket_path))
        except FileNotFoundError:
            return "absent"
        except ConnectionRefusedError:
            try:
                current = _inspect_socket(directory_fd, leaf)
            except FileNotFoundError:
                return "absent"
            if _socket_identity(current) == _socket_identity(original):
                return "absent"
            return "unreachable"
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return "absent"
            return "unreachable"
        return "unreachable"
    finally:
        client.close()


def collect(socket_path: Path) -> dict[str, Any]:
    try:
        with open_socket_parent(socket_path) as (directory_fd, leaf):
            metadata = _inspect_socket(directory_fd, leaf)
            try:
                result = run_query(
                    ["tmux", "-S", str(socket_path), "list-sessions", "-F", "#{session_name}"],
                    timeout=_TMUX_QUERY_TIMEOUT_SECONDS,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return {
                    "state": "unreachable",
                    "sessions": [],
                    "error": f"cannot execute tmux: {exc}",
                }
            if result.returncode != 0:
                state = _failed_query_state(socket_path, directory_fd, leaf, metadata)
                if state == "absent":
                    return {"state": "absent", "sessions": [], "error": ""}
                detail = (
                    result.stderr or result.stdout or f"tmux exited {result.returncode}"
                ).strip()
                return {"state": "unreachable", "sessions": [], "error": detail}
    except (FileNotFoundError, SocketDirectoryAbsentError):
        return {"state": "absent", "sessions": [], "error": ""}
    except (OSError, SocketDirectoryError) as exc:
        return {
            "state": "unreachable",
            "sessions": [],
            "error": f"cannot inspect tmux socket: {exc}",
        }
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

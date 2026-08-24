# SPDX-License-Identifier: MIT
"""Bootstrap process used as the initial command inside managed tmux launches."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from uxon.infra import launch_records


def wait_then_exec(
    *,
    socket_path: str,
    session_name: str,
    launch_nonce: str,
    record_path: str,
    agent_argv: list[str],
    timeout_seconds: float = 60.0,
) -> int:
    if not agent_argv:
        print("uxon: missing launch command", file=sys.stderr)
        return 2
    record = launch_records.wait_for_finalized_record_path(
        Path(record_path),
        socket_path,
        session_name,
        launch_nonce,
        timeout_seconds=timeout_seconds,
    )
    if record is None:
        print("uxon: launch record was not finalized in time", file=sys.stderr)
        return 124
    os.execvp(agent_argv[0], agent_argv)
    raise AssertionError("unreachable")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="uxon launch-bootstrap")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--record-path", required=True)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("agent_argv", nargs=argparse.REMAINDER)
    ns = parser.parse_args(argv)
    agent_argv = list(ns.agent_argv)
    if agent_argv and agent_argv[0] == "--":
        agent_argv = agent_argv[1:]
    return wait_then_exec(
        socket_path=ns.socket,
        session_name=ns.session,
        launch_nonce=ns.nonce,
        record_path=ns.record_path,
        agent_argv=agent_argv,
        timeout_seconds=ns.timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())

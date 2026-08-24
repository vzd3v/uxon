# SPDX-License-Identifier: MIT
"""Bootstrap process used as the initial command inside managed tmux launches."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

from uxon.infra import launch_records
from uxon.infra.run import run_query

_CLEANUP_TIMEOUT_SECONDS = 5.0


def _kill_exact_session(socket_path: str, session_name: str) -> None:
    """Best-effort cleanup after the release wait cannot complete."""
    try:
        run_query(
            ["tmux", "-S", socket_path, "kill-session", "-t", session_name],
            timeout=_CLEANUP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _release_cleanup_then_kill(socket_path: str, session_name: str, release: str) -> None:
    """Consume/clear the nonce channel once, then kill the exact session."""
    try:
        run_query(
            ["tmux", "-S", socket_path, "wait-for", "-S", release],
            timeout=_CLEANUP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    _kill_exact_session(socket_path, session_name)


def wait_then_exec(
    *,
    socket_path: str,
    session_name: str,
    launch_nonce: str,
    agent_argv: list[str],
    timeout_seconds: float = 60.0,
) -> int:
    if not agent_argv:
        print("uxon: missing launch command", file=sys.stderr)
        return 2
    release = launch_records.handshake_channel(launch_nonce, "release")
    try:
        wait = run_query(
            ["tmux", "-S", socket_path, "wait-for", release],
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        _release_cleanup_then_kill(socket_path, session_name, release)
        print("uxon: launch record was not finalized in time", file=sys.stderr)
        return 124
    except OSError as exc:
        _release_cleanup_then_kill(socket_path, session_name, release)
        print(f"uxon: tmux release wait failed: {exc}", file=sys.stderr)
        return 1
    if wait.returncode != 0:
        _release_cleanup_then_kill(socket_path, session_name, release)
        detail = (wait.stderr or wait.stdout or "tmux release wait failed").strip()
        print(f"uxon: {detail}", file=sys.stderr)
        return 1
    os.execvp(agent_argv[0], agent_argv)
    raise AssertionError("unreachable")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="uxon launch-bootstrap")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--nonce", required=True)
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
        agent_argv=agent_argv,
        timeout_seconds=ns.timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())

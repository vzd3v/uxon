# SPDX-License-Identifier: MIT
"""Bootstrap process used as the initial command inside managed tmux launches."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

from uxon.infra import launch_records
from uxon.infra.run import run_query

_CLEANUP_TIMEOUT_SECONDS = 5.0


def _kill_exact_session(socket_path: str, session_name: str, launch_nonce: str) -> None:
    """Best-effort cleanup after the release wait cannot complete."""
    try:
        from uxon.infra.tmux import guarded_session_commands

        base = ["tmux", "-S", socket_path]
        pane = os.environ.get("TMUX_PANE", "")
        locator = pane if re.fullmatch(r"%[0-9]+", pane) else "=" + session_name + ":"
        result = run_query(
            base
            + [
                "display-message",
                "-p",
                "-t",
                locator,
                "#{session_id}\t#{session_created}\t#{session_name}\t#{E:UXON_LAUNCH_NONCE}",
            ],
            timeout=_CLEANUP_TIMEOUT_SECONDS,
        )
        fields = result.stdout.strip().split("\t")
        if result.returncode or len(fields) != 4 or fields[3] != launch_nonce:
            return
        metadata = launch_records.TmuxSessionMetadata(*fields)
        run_query(
            base + guarded_session_commands(metadata, f"kill-session -t {metadata.session_id}"),
            timeout=_CLEANUP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired, SystemExit):
        pass


def _release_cleanup_then_kill(
    socket_path: str, session_name: str, release: str, launch_nonce: str
) -> None:
    """Consume/clear the nonce channel once, then kill the exact session."""
    try:
        run_query(
            ["tmux", "-S", socket_path, "wait-for", "-S", release],
            timeout=_CLEANUP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    _kill_exact_session(socket_path, session_name, launch_nonce)


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
        _release_cleanup_then_kill(socket_path, session_name, release, launch_nonce)
        print("uxon: launch record was not finalized in time", file=sys.stderr)
        return 124
    except OSError as exc:
        _release_cleanup_then_kill(socket_path, session_name, release, launch_nonce)
        print(f"uxon: tmux release wait failed: {exc}", file=sys.stderr)
        return 1
    if wait.returncode != 0:
        _release_cleanup_then_kill(socket_path, session_name, release, launch_nonce)
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

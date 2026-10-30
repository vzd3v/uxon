"""Terminal failure notices distinguish retained managed output from fast exits."""

import io
from dataclasses import replace
from unittest import mock

from helpers import make_config

from uxon.domain.launch_request import LaunchRequest, ManagedTmuxLaunch, TmuxHandoff
from uxon.tui.launch import pause_on_launch_failure


def test_failure_notice_matrix_and_acknowledgement_reads() -> None:
    managed = ManagedTmuxLaunch(
        create_cmd=("tmux", "new-session"),
        query_cmd=("tmux", "display-message"),
        release_cmd=("tmux", "wait-for"),
        rollback_kill_prefix=("tmux", "kill-session", "-t"),
        record_socket="/tmp/test.sock",
        record_session="uxon-demo@codex",
        record_nonce="nonce",
        record_dir="/tmp/records",
        launch_profile="codex",
        agent="codex",
        launch_user="alice",
    )
    base = LaunchRequest(
        cmd=("tmux", "attach-session"), label="attach demo", prelaunch=(("prepare", "workspace"),)
    )
    requests = {
        "unmanaged": base,
        "managed": replace(base, managed=managed),
        "handoff": replace(base, handoff=TmuxHandoff(make_config(), "alice")),
    }
    for kind, rc, wall, stage, expected in (
        ("managed", 0, 0.1, "cmd", ""),
        ("handoff", 0, 0.1, "cmd", ""),
        (
            "unmanaged",
            0,
            0.1,
            "cmd",
            "\nuxon: attach demo exited immediately (rc=0 in 0.10s, stage=cmd)\n  command: tmux attach-session\n  no diagnostic output was retained\npress Enter to return to the uxon menu...\n",
        ),
        (
            "unmanaged",
            1,
            2.0,
            "cmd",
            "\nuxon: attach demo failed (rc=1, stage=cmd)\n  command: tmux attach-session\n  command output, if any, was written directly above\npress Enter to return to the uxon menu...\n",
        ),
        (
            "managed",
            1,
            0.1,
            "prelaunch",
            "\nuxon: attach demo failed (rc=1, stage=prelaunch)\n  command: prepare workspace\n  command output, if any, was written directly above\npress Enter to return to the uxon menu...\n",
        ),
        (
            "handoff",
            1,
            0.1,
            "cmd",
            "\nuxon: attach demo failed (rc=1, stage=cmd)\n  command: tmux attach-session\n  command output, if any, was written directly above\npress Enter to return to the uxon menu...\n",
        ),
        ("unmanaged", 130, 0.1, "cmd", ""),
        ("unmanaged", 0, 1.0, "cmd", ""),
        ("unmanaged", 0, 2.0, "cmd", ""),
    ):
        output = io.StringIO()
        with mock.patch("sys.stdin.readline", return_value="\n") as readline:
            pause_on_launch_failure(output, requests[kind], rc, stage, wall)
        assert output.getvalue() == expected, (kind, rc, wall, stage)
        assert readline.call_count == int(bool(expected)), (kind, rc, wall, stage)

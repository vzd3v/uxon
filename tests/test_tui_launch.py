from __future__ import annotations

import io
from unittest import mock

from uxon.domain.launch_request import LaunchRequest, ManagedTmuxLaunch
from uxon.tui.launch import pause_on_launch_failure


def test_managed_fast_zero_is_not_reclassified_as_agent_failure() -> None:
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
        launch_user="dana_agent",
    )
    request = LaunchRequest(cmd=("tmux", "attach-session"), managed=managed)
    output = io.StringIO()
    with mock.patch("sys.stdin.readline") as readline:
        pause_on_launch_failure(output, request, 0, "cmd", 0.1)
    assert output.getvalue() == ""
    readline.assert_not_called()


def test_unmanaged_fast_zero_reports_that_no_output_was_retained() -> None:
    request = LaunchRequest(cmd=("tmux", "attach-session"), label="attach demo")
    output = io.StringIO()
    with mock.patch("sys.stdin.readline", return_value="\n"):
        pause_on_launch_failure(output, request, 0, "cmd", 0.1)
    rendered = output.getvalue()
    assert "exited immediately" in rendered
    assert "no diagnostic output was retained" in rendered
    assert "see output above" not in rendered


def test_nonzero_launch_reports_direct_terminal_output() -> None:
    request = LaunchRequest(cmd=("tmux", "attach-session"), label="attach demo")
    output = io.StringIO()
    with mock.patch("sys.stdin.readline", return_value="\n"):
        pause_on_launch_failure(output, request, 1, "cmd", 2.0)
    rendered = output.getvalue()
    assert "failed (rc=1" in rendered
    assert "command output, if any, was written directly above" in rendered

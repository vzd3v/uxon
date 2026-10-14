# SPDX-License-Identifier: MIT
"""Dashboard title output policy at the terminal boundary."""

import io
from unittest import mock

import pytest

from uxon.infra.terminal_title import set_dashboard_title


@pytest.mark.parametrize(
    "enabled,tty,term,expected",
    [
        (True, True, term, "\x1b]0;uxon\x07")
        for term in ("xterm-256color", "tmux-256color", "custom")
    ]
    + [(False, True, "xterm", ""), (True, False, "xterm", "")]
    + [(True, True, term, "") for term in ("", "dumb", "linux")],
)
def test_dashboard_title_output_policy(enabled: bool, tty: bool, term: str, expected: str) -> None:
    output = io.StringIO()
    with mock.patch.object(output, "isatty", return_value=tty):
        set_dashboard_title(output, enabled=enabled, term=term)
    assert output.getvalue() == expected


@pytest.mark.parametrize("operation", ["isatty", "write", "flush"])
def test_disconnected_terminal_does_not_fail_title_update(operation: str) -> None:
    output = mock.Mock(isatty=mock.Mock(return_value=True))
    getattr(output, operation).side_effect = OSError("terminal disconnected")
    set_dashboard_title(output, enabled=True, term="xterm-256color")

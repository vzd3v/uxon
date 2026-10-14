# SPDX-License-Identifier: MIT
"""Best-effort terminal title for the interactive dashboard."""

from __future__ import annotations

from typing import TextIO


def set_dashboard_title(stream: TextIO, *, enabled: bool, term: str) -> None:
    """Claim the dashboard title once, without probing terminal input.

    OSC 0 updates both window and icon titles, matching tmux's title scope.
    Terminal and enclosing multiplexer policies still control presentation.
    """
    if not enabled or not term or term in {"dumb", "linux"}:
        return
    try:
        if stream.isatty():
            stream.write("\x1b]0;uxon\x07")
            stream.flush()
    except OSError:
        # Optional presentation must not fail when a terminal disappears.
        return

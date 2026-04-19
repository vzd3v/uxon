"""Textual-missing error hint for the ccw TUI.

Shown by :func:`ccw_tui.app.run` when ``import textual`` fails. Kept as
a plain string so non-TUI code paths (``bin/ccw list``, etc.) never
import textual at module load.
"""

from __future__ import annotations


TEXTUAL_MISSING_HINT = (
    "ccw: interactive mode requires the 'textual' package.\n"
    "  Install per-user:     pip install --user textual\n"
    "  Or inside a venv:     python3 -m venv .venv && .venv/bin/pip install textual"
)

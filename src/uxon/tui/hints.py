"""Repair guidance for an incomplete Uxon TUI installation.

Shown by :func:`uxon.tui.runner.run` when ``import textual`` fails. Kept as
a plain string so non-TUI code paths (``uxon list``, etc.) never
import textual at module load.
"""

from __future__ import annotations

TEXTUAL_MISSING_HINT = (
    "uxon: interactive mode could not load its Textual dependency.\n"
    "  Repair the existing Uxon installation with its original manager:\n"
    "  uv tool install --reinstall uxon  OR  pipx reinstall uxon\n"
    "  For a virtual environment, reinstall uxon with that environment's Python."
)

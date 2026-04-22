"""ccw interactive TUI — package entry point.

Public API re-exports only. Implementation lives in sibling modules:

  - ``context``  — pure data (``TuiContext``, ``TuiSession``, ``ServerStatus``,
                   ``LaunchRequest``, ``Item``, ``build_items``, ``CallbackError``).
  - ``state``    — pure TUI state decisions (not public-re-exported).
  - ``events``   — JSONL event log (``LOG_DIR``, ``_log_event``).
  - ``launch``   — launch-handoff helpers (runs outside the TUI).
  - ``hints``    — ``TEXTUAL_MISSING_HINT`` install guidance.
  - ``app``      — textual :class:`CcwApp` + :func:`run` outer loop.
  - ``screens/`` — one module per screen (MainScreen, modals, …).
  - ``widgets/`` — two custom widgets (ActionRow, SessionTable).
"""

from __future__ import annotations

from .context import (
    CallbackError,
    Item,
    LaunchRequest,
    ServerStatus,
    TuiContext,
    TuiSession,
    build_items,
)
from .events import LOG_DIR
from .hints import TEXTUAL_MISSING_HINT
from .app import CcwApp, run

__all__ = [
    "CallbackError",
    "CcwApp",
    "Item",
    "LOG_DIR",
    "LaunchRequest",
    "ServerStatus",
    "TEXTUAL_MISSING_HINT",
    "TuiContext",
    "TuiSession",
    "build_items",
    "run",
]

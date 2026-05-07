"""uxon interactive TUI — package entry point.

Public API re-exports only. Implementation lives in sibling modules:

  - ``context``  — pure data (``TuiContext``, ``TuiSession``, ``ServerStatus``,
                   ``LinkHealthStatus``, ``LaunchRequest``, ``Item``,
                   ``build_items``, ``CallbackError``).
  - ``state``    — pure TUI state decisions (not public-re-exported).
  - ``events``   — debug and metrics channels (``debug``,
                   ``metrics_record``).  The audit channel lives in
                   ``uxon.audit`` and goes to journald / syslog directly.
  - ``launch``   — launch-handoff helpers (runs outside the TUI).
  - ``hints``    — ``TEXTUAL_MISSING_HINT`` install guidance.
  - ``app``      — textual :class:`UxonApp` + :func:`run` outer loop.
  - ``screens/`` — one module per screen (MainScreen, modals, …).
  - ``widgets/`` — custom widgets (``ActionRow``,
                   ``DetectedAgentsBanner``, ``SessionDashboardTable``).
  - ``dashboard/``— pure layers behind ``SessionDashboardTable``
                   (row, columns, layout, ui_state, model, reconcile).

Pure-data re-exports load eagerly. Textual-dependent names (``UxonApp``,
``run``) are deferred via ``__getattr__`` so that
``from uxon.tui import TuiContext`` and other pure-data imports do not
pull ``textual`` at import time — required by the AGENTS.md hard rule
that non-TUI subcommands stay textual-free.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .context import (
    CallbackError,
    Item,
    LaunchRequest,
    LinkHealthStatus,
    ServerStatus,
    TuiContext,
    TuiSession,
    build_items,
)
from .hints import TEXTUAL_MISSING_HINT

if TYPE_CHECKING:
    from .app import UxonApp, run

__all__ = [
    "CallbackError",
    "UxonApp",
    "Item",
    "LinkHealthStatus",
    "LaunchRequest",
    "ServerStatus",
    "TEXTUAL_MISSING_HINT",
    "TuiContext",
    "TuiSession",
    "build_items",
    "run",
]


def __getattr__(name: str) -> Any:
    if name in ("UxonApp", "run"):
        from . import app as _app

        return getattr(_app, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

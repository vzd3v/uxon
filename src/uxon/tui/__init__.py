"""uxon interactive TUI — package entry point.

Public API re-exports only. Implementation lives in sibling modules:

  - ``context``  — pure data (``TuiContext``, ``TuiSession``, ``ServerStatus``,
                   ``LinkHealthStatus``, ``LaunchRequest``, ``Item``,
                   ``build_items``, ``CallbackError``).
  - ``state``    — pure TUI state decisions (not public-re-exported).
  - ``events``   — debug and metrics channels (``debug``,
                   ``metrics_record``).  The audit channel lives in
                   ``uxon.infra.audit`` and goes to journald / syslog directly.
  - ``launch``   — launch-handoff helpers (runs outside the TUI).
  - ``hints``    — ``TEXTUAL_MISSING_HINT`` install guidance.
  - ``app``      — textual :class:`UxonApp` host (worker routing + on__*).
  - ``runner``   — :func:`run` outer create/exit/re-create loop (TTY handoff).
  - ``messages`` — worker payload :class:`Message` envelopes (pure data).
  - ``workers``  — :class:`WorkerCoordinator` (probe/source worker bodies).
  - ``source_dispatch`` — landed-result → :class:`TuiState` reducers.
  - ``screens/`` — one module per screen (MainScreen, modals, …).
  - ``widgets/`` — custom widgets (``ActionRow``,
                   ``DetectedAgentsBanner``, ``SessionListView``,
                   ``GatedFooter``).
  - ``dashboard/``— pure layers behind ``SessionListView``
                   (row, columns, layout, ui_state, model, order).

Pure-data re-exports load eagerly. Textual-dependent names (``UxonApp``,
``run``) are deferred via ``__getattr__`` so that
``from uxon.tui import TuiContext`` and other pure-data imports do not
pull ``textual`` at import time — required by the AGENTS.md hard rule
that non-TUI subcommands stay textual-free.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from uxon.domain.launch_request import LaunchRequest
from uxon.domain.session import TuiSession
from uxon.domain.status import LinkHealthStatus, ServerStatus

from .context import (
    CallbackError,
    Item,
    TuiContext,
    build_items,
)
from .hints import TEXTUAL_MISSING_HINT

if TYPE_CHECKING:
    from .app import UxonApp
    from .runner import run

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
    if name == "UxonApp":
        from . import app as _app

        return _app.UxonApp
    if name == "run":
        from . import runner as _runner

        return _runner.run
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

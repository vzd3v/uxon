"""ccw interactive TUI — package entry point.

Public API re-exports only. Implementation lives in sibling modules:

  - ``context``  — pure data (``TuiContext``, ``TuiSession``, ``LaunchRequest``,
                   ``Item``, ``build_items``, ``CallbackError``).
  - ``events``   — JSONL event log (``LOG_DIR``, ``_log_event``).
  - ``launch``   — launch-handoff helpers (runs outside the TUI).
  - ``_legacy``  — the blessed-based implementation, scheduled for removal
                   after the textual migration completes. Pulled in
                   transparently via the ``run`` re-export so ``bin/ccw``
                   continues to work across every intermediate commit.

During the migration, both ``BLESSED_MISSING_HINT`` and
``TEXTUAL_MISSING_HINT`` are exported. ``BLESSED_MISSING_HINT`` is
retired once T20 lands.
"""

from __future__ import annotations

# Pure data.
from .context import (
    ACTION_COUNT,
    CallbackError,
    Item,
    LaunchRequest,
    TuiContext,
    TuiSession,
    _ACTION_KINDS,
    _digit_hinted_indices,
    _segments,
    _total_items,
    build_items,
)
from .events import LOG_DIR, _log_event
from .launch import (
    FAST_EXIT_THRESHOLD_SEC,
    _drain_stdin,
    _format_launch_status,
    _pause_on_launch_failure,
    _run_launch_request,
)

# Hints.
from ._legacy import BLESSED_MISSING_HINT
from .hints import TEXTUAL_MISSING_HINT

# Entry point + legacy helpers. The symbols exported here keep test
# monkey-patching via ``mock.patch.object(ccw_tui, "<name>")`` working
# across the T3 split. In ``_legacy`` each call site resolves these
# helpers via module globals; tests patch the package namespace and
# we mirror the write through to the legacy module (see end of file).
from ._legacy import (
    SCREEN_KEYMAP,
    Screen,
    _activate_item,
    _build_footer,
    _compute_col_widths,
    _confirm_kill,
    _confirm_kill_all,
    _confirm_kill_all_global,
    _interactive_loop,
    _prompt_existing_project,
    _prompt_git_profile,
    _prompt_permissions,
    _prompt_project_name,
    run,
)

# Legacy attribute compatibility for tests that monkey-patch
# ``ccw_tui.subprocess`` / ``ccw_tui.sys`` to intercept subprocess calls
# made from the legacy runner. These proxies remain until T20 when
# _legacy.py is deleted outright.
from ._legacy import subprocess, sys  # noqa: F401

__all__ = [
    "ACTION_COUNT",
    "BLESSED_MISSING_HINT",
    "CallbackError",
    "FAST_EXIT_THRESHOLD_SEC",
    "Item",
    "LOG_DIR",
    "LaunchRequest",
    "SCREEN_KEYMAP",
    "Screen",
    "TEXTUAL_MISSING_HINT",
    "TuiContext",
    "TuiSession",
    "build_items",
    "run",
]

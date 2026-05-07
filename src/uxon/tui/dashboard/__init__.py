"""Unified session dashboard: pure-data layers (row → columns → model).

The widget that consumes these lands in a later commit; this package
keeps the row type, the column registry, and the layout selector
isolated from any Textual import so callers can unit-test them
without an event loop.
"""

from __future__ import annotations

# Lightweight mirror of the column ids in :data:`uxon.tui.dashboard.columns.REGISTRY`.
# Re-exported here so config-loading (``uxon.cli``) can validate user-supplied
# ``tui.table.columns`` / ``tui.table.default_sort_by`` ids without dragging Rich
# (and Textual) into the CLI import path. A drift test in
# ``tests/test_dashboard_columns.py`` keeps the two in lock-step.
KNOWN_COLUMN_IDS: tuple[str, ...] = (
    "host",
    "user",
    "name",
    "agent",
    "cpu",
    "ram",
    "new",
    "last",
    "cmd",
    "path",
    "pid",
    "wins",
)

__all__ = ("KNOWN_COLUMN_IDS",)

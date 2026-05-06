"""Unified session dashboard: pure-data layers (row → columns → model).

The widget that consumes these lands in a later commit; this package
keeps the row type, the column registry, and the layout selector
isolated from any Textual import so callers can unit-test them
without an event loop.
"""

from __future__ import annotations

from __future__ import annotations

from uxon.settings import SCHEMA_KEYS


def test_default_view_registered():
    assert "tui.table.default_view" in SCHEMA_KEYS


def test_search_fields_registered():
    assert "tui.search.fields" in SCHEMA_KEYS


def test_color_palette_registered():
    assert "tui.color_palette" in SCHEMA_KEYS


def test_local_host_color_registered():
    assert "local_host.color" in SCHEMA_KEYS

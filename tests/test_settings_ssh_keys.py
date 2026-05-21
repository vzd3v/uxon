from __future__ import annotations

from uxon.settings import SCHEMA_KEYS


def test_ssh_multiplex_registered():
    assert "ssh_multiplex" in SCHEMA_KEYS


def test_ssh_control_persist_seconds_registered():
    assert "ssh_control_persist_seconds" in SCHEMA_KEYS

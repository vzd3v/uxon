"""Validation tests for the optional ``[[remote_hosts]] color`` field."""

from __future__ import annotations

import pytest

from uxon.remote_hosts import RemoteHostError, load_remote_hosts


def test_color_defaults_to_none():
    hosts = load_remote_hosts([{"name": "a", "ssh_alias": "x"}])
    assert hosts[0].color is None


def test_color_accepted_when_string():
    hosts = load_remote_hosts([{"name": "a", "ssh_alias": "x", "color": "blue"}])
    assert hosts[0].color == "blue"


def test_color_rejects_empty_string():
    with pytest.raises(RemoteHostError, match="color"):
        load_remote_hosts([{"name": "a", "ssh_alias": "x", "color": ""}])

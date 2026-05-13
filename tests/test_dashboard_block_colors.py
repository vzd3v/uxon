"""Tests for the pure ``assign_block_colors`` mapping helper."""

from __future__ import annotations

from uxon.remote_hosts import RemoteHost
from uxon.tui.dashboard.columns import assign_block_colors


def _h(name, color=None):
    return RemoteHost(
        name=name,
        ssh_alias=f"alias-{name}",
        description="",
        remote_uxon="uxon",
        color=color,
    )


def test_locals_get_local_color():
    out = assign_block_colors((), local_color="green", palette=("cyan", "blue"))
    assert out == {None: "green"}


def test_auto_cycle_with_adjacency_skip():
    out = assign_block_colors(
        (_h("a"), _h("b"), _h("c")),
        local_color="green",
        palette=("cyan", "blue"),
    )
    # Local=green; remote-a auto-picks cyan (≠ green prev); remote-b
    # auto-picks blue (≠ cyan prev); remote-c auto-picks cyan (≠ blue prev).
    assert out == {None: "green", "a": "cyan", "b": "blue", "c": "cyan"}


def test_pin_overrides_auto_cycle():
    out = assign_block_colors(
        (_h("a", color="red"), _h("b")),
        local_color="green",
        palette=("cyan", "blue"),
    )
    # Pin always wins; no validation against prev.
    assert out[None] == "green"
    assert out["a"] == "red"
    assert out["b"] in ("cyan", "blue")  # auto-cycle resumes from a fresh idx


def test_pin_equal_prev_allowed():
    out = assign_block_colors(
        (_h("a", color="green"),),
        local_color="green",
        palette=("cyan",),
    )
    # Operator pinned green; visual collision is their choice.
    assert out["a"] == "green"

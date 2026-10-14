"""Host attribution preserves palette order and explicit operator pins."""

from uxon.infra.remote_hosts import RemoteHost
from uxon.tui.dashboard.columns import assign_block_colors


def _h(name, color=None):
    return RemoteHost(name=name, ssh_alias=name, description="", remote_uxon="uxon", color=color)


def test_palette_collisions_pins_and_resume() -> None:
    hosts = (_h("a"), _h("b", "blue"), _h("c"), _h("d", "green"), _h("e"))
    for palette, expected in (
        (
            ("green", "blue", "cyan"),
            {None: "green", "a": "blue", "b": "blue", "c": "cyan", "d": "green", "e": "blue"},
        ),
        (
            ("green",),
            {None: "green", "a": "green", "b": "blue", "c": "green", "d": "green", "e": "green"},
        ),
        ((), {None: "green", "a": "dim", "b": "blue", "c": "dim", "d": "green", "e": "dim"}),
    ):
        assert assign_block_colors(hosts, local_color="green", palette=palette) == expected, palette
    assert assign_block_colors((), local_color="green", palette=("cyan", "blue")) == {None: "green"}
    assert assign_block_colors(
        (_h("a"), _h("b"), _h("c")), local_color="green", palette=("cyan", "blue")
    ) == {None: "green", "a": "cyan", "b": "blue", "c": "cyan"}

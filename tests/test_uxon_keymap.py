"""Physical-key aliases retain actions without duplicating footer entries."""

from textual.binding import Binding

from uxon.tui.keymap import bindings_with_aliases


def test_mixed_bindings_preserve_originals_and_alias_metadata() -> None:
    originals = (
        Binding("q", "quit", "Quit"),
        Binding("D", "kill_all", "Kill all"),
        Binding("h", "toggle_hosts", "Hosts"),
        Binding("d", "kill", "Kill", priority=True),
        Binding("f10", "help", "Help", show=False),
    )
    result = bindings_with_aliases(*originals)
    assert [(b.key, b.action, b.description, b.show, b.priority) for b in result] == [
        ("q", "quit", "Quit", True, False),
        ("й", "quit", "Quit", False, False),
        ("D", "kill_all", "Kill all", True, False),
        ("В", "kill_all", "Kill all", False, False),
        ("h", "toggle_hosts", "Hosts", True, False),
        ("р", "toggle_hosts", "Hosts", False, False),
        ("d", "kill", "Kill", True, True),
        ("в", "kill", "Kill", False, True),
        ("f10", "help", "Help", False, False),
    ]
    assert result[::2] == list(originals)

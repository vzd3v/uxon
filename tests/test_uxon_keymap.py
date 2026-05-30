"""JCUKEN ↔ QWERTY layout-alias contract for ``BINDINGS``."""

from __future__ import annotations

from textual.binding import Binding

from uxon.tui.keymap import LAYOUT_ALIASES, bindings_with_aliases


def test_known_key_gets_a_twin():
    out = bindings_with_aliases(Binding("q", "quit", "Quit", show=True))
    assert {"q", "й"} <= {b.key for b in out}
    quit_actions = {b.action for b in out}
    assert quit_actions == {"quit"}
    twin = next(b for b in out if b.key == "й")
    assert twin.show is False  # twins never duplicate the footer entry


def test_unknown_key_passes_through_no_twin():
    out = bindings_with_aliases(Binding("f10", "help", "", show=False))
    keys = {b.key for b in out}
    assert keys == {"f10"}


def test_uppercase_letter_twin_exists():
    out = bindings_with_aliases(Binding("D", "kill_all", "", show=True))
    keys = {b.key for b in out}
    assert "D" in keys and LAYOUT_ALIASES["D"] in keys


def test_hosts_toggle_key_has_ru_twin():
    # The FleetStatusBar `h` toggle must reach RU-layout operators too.
    out = bindings_with_aliases(Binding("h", "toggle_hosts", "Hosts", show=True))
    assert {"h", "р"} <= {b.key for b in out}

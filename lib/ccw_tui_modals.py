"""Reusable modal dispatcher and a MenuModal helper.

A Modal is any object with:
    render(t)           -- draws the modal surface; called before each inkey()
    handle(key)         -- receives a blessed Keystroke; returns ModalResult
                           to dismiss, or None to stay open.

``run_modal(t, modal)`` is the event loop. It does NOT save/restore the
caller's screen — callers re-render their own screen after dismissal.
This matches how sub-screens already work in ``ccw_tui``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ccw_tui_widgets import dim as _dim

if TYPE_CHECKING:
    from blessed import Terminal


@dataclass
class ModalResult:
    name: str           # e.g. "selected", "cancel", "submit"
    value: Any = None


def run_modal(t: "Terminal", modal) -> ModalResult:
    """Drive a modal to completion and return its ModalResult."""
    while True:
        modal.render(t)
        key = t.inkey(timeout=None)
        result = modal.handle(key)
        if result is not None:
            return result


class MenuModal:
    """Select-one-from-list modal. Keyboard only at this task; mouse is
    wired in a later task."""

    def __init__(self, title: str, rows: list[tuple[Any, str]], detail: str = "") -> None:
        self.title = title
        self.rows = rows  # (value, display_text)
        self.detail = detail
        self.cursor = 0

    def render(self, t: "Terminal") -> None:
        print(t.home + t.clear, end="")
        print(t.bold_white_on_blue(f" {self.title} "))
        print(_dim(t, "─" * t.width))
        if self.detail:
            print(f"  {_dim(t, self.detail)}")
            print()
        for i, (_, label) in enumerate(self.rows):
            prefix = t.bold_cyan("▸ ") if i == self.cursor else "  "
            if i < 9:
                num = _dim(t, f"{i+1} ") if i != self.cursor else f"{i+1} "
            else:
                num = "  "
            line = prefix + num + (t.bold(label) if i == self.cursor else label)
            if i == self.cursor:
                print(t.reverse(t.ljust(line, t.width)))
            else:
                print(line)
        with t.location(0, t.height - 1):
            print(_dim(t, "  ↑↓ navigate · Enter select · Esc cancel"), end="")

    def handle(self, key) -> "ModalResult | None":
        name = getattr(key, "name", None)
        if name == "KEY_ESCAPE":
            return ModalResult("cancel")
        if name == "KEY_UP" or key == "k":
            if self.cursor > 0:
                self.cursor -= 1
            return None
        if name == "KEY_DOWN" or key == "j":
            if self.cursor < len(self.rows) - 1:
                self.cursor += 1
            return None
        if name == "KEY_ENTER" or key == "\n" or key == "\r":
            return ModalResult("selected", self.rows[self.cursor][0])
        is_seq = getattr(key, "is_sequence", False)
        if not is_seq and str(key) in "123456789":
            idx = int(str(key)) - 1
            if 0 <= idx < len(self.rows):
                return ModalResult("selected", self.rows[idx][0])
        return None

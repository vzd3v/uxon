"""Reusable TUI primitives for ccw (blessed-based).

All functions take a blessed Terminal as the first argument and no other
implicit state. Any sub-screen that needs a text input, confirmation, or
dim helper should use these instead of re-implementing them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from blessed import Terminal


def dim(t: "Terminal", text: str) -> str:
    """Portable dim — t.dim(text) fails on some terminals where dim is a
    ParameterizingString. Concatenation always works."""
    return t.dim + text + t.normal


def confirm_phrase(t: "Terminal", prompt: str, phrase: str, y: int, timeout: float = 30.0) -> bool:
    """Blocking prompt that returns True iff the user types `phrase` + Enter."""
    with t.location(0, y):
        print(t.clear_eol + prompt, end="", flush=True)
    buf = ""
    while True:
        key = t.inkey(timeout=timeout)
        if not key:
            return False
        if key.name == "KEY_ENTER" or key == "\n" or key == "\r":
            return buf == phrase
        if key.name == "KEY_ESCAPE":
            return False
        if key.name == "KEY_BACKSPACE" or key == "\x7f":
            if buf:
                buf = buf[:-1]
                with t.location(0, y):
                    print(t.clear_eol + prompt + buf, end="", flush=True)
            continue
        if key.is_sequence:
            continue
        buf += str(key)
        with t.location(0, y):
            print(t.clear_eol + prompt + buf, end="", flush=True)


def confirm_yn(t: "Terminal", prompt: str, y: int, timeout: float = 10.0) -> bool:
    """Single-keystroke y/N prompt. True only for 'y' / 'Y'."""
    with t.location(0, y):
        print(t.clear_eol + prompt, end="", flush=True)
    key = t.inkey(timeout=timeout)
    if not key:
        return False
    return str(key).lower() == "y"


def text_input(
    t: "Terminal",
    title: str,
    current: str = "",
    detail: str = "",
    validator: "Callable[[str], str | None] | None" = None,
) -> "str | None":
    """Modal text input. Returns the string on Enter, None on Esc.

    validator: callable returning None if the buffer is acceptable, or an
    error message to display.
    """
    buf = current
    error = ""
    while True:
        print(t.home + t.clear, end="")
        print(t.bold_white_on_blue(f" {title} "))
        print(dim(t, "─" * t.width))
        print()
        if detail:
            print(f"  {dim(t, detail)}")
            print()
        print(f"  {t.bold('Value:')} {buf}" + t.bold_cyan("█"))
        if error:
            print()
            print(f"  {t.bold_red(error)}")

        footer_y = t.height - 1
        with t.location(0, footer_y):
            print(dim(t, "  Enter confirm · Esc cancel"), end="")

        key = t.inkey(timeout=None)
        if key.name == "KEY_ESCAPE":
            return None
        if key.name == "KEY_ENTER" or key == "\n" or key == "\r":
            if validator is not None:
                err = validator(buf)
                if err:
                    error = err
                    continue
            return buf
        if key.name == "KEY_BACKSPACE" or key == "\x7f":
            if buf:
                buf = buf[:-1]
            error = ""
            continue
        if key.is_sequence:
            continue
        buf += str(key)
        error = ""


def flash_error(t: "Terminal", msg: str, timeout: float = 3.0) -> None:
    """Briefly show an error in the footer-status area."""
    with t.location(0, t.height - 3):
        print(t.clear_eol + "  " + t.red(f"error: {msg}"), end="", flush=True)
    t.inkey(timeout=timeout)

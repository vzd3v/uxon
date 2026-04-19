"""Mouse input for the ccw blessed TUI.

We enable SGR 1006 mouse reporting (DEC private mode) on fullscreen
entry and disable on exit. ``blessed.Terminal.inkey()`` returns
unrecognized escape sequences verbatim, so we parse them in
:func:`parse_mouse_sgr`.

Only three kinds of events matter for the ccw UI:
  * left-button press (button=0, pressed=True) — acts like Enter
  * left-button release (button=0, pressed=False) — used to debounce
  * wheel up/down (buttons 64/65) — acts like ↑/↓

Everything else is ignored.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from blessed import Terminal


ENABLE = "\x1b[?1000h\x1b[?1006h"
DISABLE = "\x1b[?1006l\x1b[?1000l"

_SGR = re.compile(r"^\x1b\[<(\d+);(\d+);(\d+)([Mm])$")


@dataclass
class MouseEvent:
    button: int
    x: int           # 1-based column (like the protocol)
    y: int           # 1-based row
    pressed: bool    # True on 'M', False on 'm'
    wheel: int       # -1 = up, +1 = down, 0 = not a wheel event


def parse_mouse_sgr(seq: str) -> "MouseEvent | None":
    """Parse an SGR-1006 mouse escape sequence.

    Returns ``None`` if ``seq`` is not a well-formed SGR-1006 report.
    """
    m = _SGR.match(seq)
    if not m:
        return None
    b = int(m.group(1))
    x = int(m.group(2))
    y = int(m.group(3))
    pressed = m.group(4) == "M"
    wheel = 0
    if b == 64:
        wheel = -1
    elif b == 65:
        wheel = 1
    return MouseEvent(button=b, x=x, y=y, pressed=pressed, wheel=wheel)


@dataclass
class HitRegion:
    y: int            # 0-based row in rendered output
    action: str       # free-form tag, e.g. "row", "footer", "kill"
    payload: object = None


def hit_test(regions: "list[HitRegion]", y: int) -> "HitRegion | None":
    """Return the first region whose y matches, or None.

    ``y`` is 0-based (converted from the 1-based protocol value by the
    caller).
    """
    for r in regions:
        if r.y == y:
            return r
    return None


def enable(t: "Terminal") -> None:
    """Turn on SGR-1006 mouse reporting. Safe to call repeatedly."""
    sys.stdout.write(ENABLE)
    sys.stdout.flush()


def disable(t: "Terminal") -> None:
    """Turn off SGR-1006 mouse reporting. Safe to call repeatedly."""
    sys.stdout.write(DISABLE)
    sys.stdout.flush()


_MOUSE_TERM = ("M", "m")


def read_input(t: "Terminal", timeout=None):
    """Read a keystroke; if it is an SGR-1006 mouse sequence, return
    :class:`MouseEvent` instead. Otherwise return the blessed Keystroke
    unchanged.

    Return type: ``Keystroke | MouseEvent | None`` (``None`` on timeout).

    In practice ``blessed.Terminal.inkey()`` recognizes the CSI prefix
    ``\\x1b[`` as a partial sequence and, for SGR-1006 mouse reports
    (which blessed has no built-in mapping for), often splits the
    payload across multiple ``inkey()`` calls — we have observed
    ``\\x1b[``, ``<``, ``6``, ``5``, ``;`` arriving as separate
    keystrokes on xterm-256color via tmux/pty. So when the first
    keystroke *starts* a plausible SGR mouse sequence, we drain
    subsequent characters with short zero-timeout calls until we see
    the terminator ``M``/``m``. We never re-enter with a long timeout,
    so a genuine lone ESC keystroke cannot be swallowed.
    """
    key = t.inkey(timeout=timeout)
    if not key:
        return None
    s = str(key)
    # Fast path: full sequence already returned in one keystroke.
    if s.startswith("\x1b[<") and s[-1:] in _MOUSE_TERM:
        ev = parse_mouse_sgr(s)
        if ev is not None:
            return ev
    # Partial SGR-1006: blessed split the CSI sequence. Drain the rest.
    if s in ("\x1b[", "\x1b[<") or (
        s.startswith("\x1b[<") and s[-1:] not in _MOUSE_TERM
    ):
        buf = s
        # Ensure we're past the "<" — if we only have "\x1b[", the next
        # char must be "<" to be an SGR mouse report.
        deadline_reads = 32  # hard cap to prevent infinite loops on noise
        while deadline_reads > 0 and buf[-1:] not in _MOUSE_TERM:
            deadline_reads -= 1
            nxt = t.inkey(timeout=0.1)
            if not nxt:
                break
            ns = str(nxt)
            buf += ns
            # Early abort: the collated buffer is no longer a valid SGR
            # prefix (e.g. second char after "\x1b[" is not "<"). Hand
            # the original keystroke back unchanged so the caller can
            # treat it as a non-mouse sequence.
            if len(buf) >= 3 and not buf.startswith("\x1b[<"):
                return key
        if buf.startswith("\x1b[<") and buf[-1:] in _MOUSE_TERM:
            ev = parse_mouse_sgr(buf)
            if ev is not None:
                return ev
    return key

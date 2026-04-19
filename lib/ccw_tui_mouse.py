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


def read_input(t: "Terminal", timeout=None):
    """Read a keystroke; if it is an SGR-1006 mouse sequence, return
    :class:`MouseEvent` instead. Otherwise return the blessed Keystroke
    unchanged.

    Return type: ``Keystroke | MouseEvent | None`` (``None`` on timeout).

    ASSUMPTION: under ``cbreak`` + SGR-1006 on modern terminals
    (xterm, tmux≥2.4, iTerm2, kitty, Alacritty), ``blessed.inkey()``
    aggregates unknown CSI sequences up to their terminator within its
    internal KEYSTROKE_DELAY (~0.34s) and hands back the full
    ``\\x1b[<b;x;y(M|m)`` as one Keystroke. We do NOT re-enter
    ``inkey()`` with ``timeout=0`` to drain the rest — doing so risks
    consuming the leading ESC of a different keystroke and
    mis-attributing it.

    If in future we see a terminal that splits the sequence, the right
    fix is to read raw bytes via ``os.read(sys.stdin.fileno(), ...)`` in
    the same ``cbreak`` context, NOT to re-enter ``inkey()``.
    """
    key = t.inkey(timeout=timeout)
    if not key:
        return None
    s = str(key)
    if s.startswith("\x1b[<") and (s.endswith("M") or s.endswith("m")):
        ev = parse_mouse_sgr(s)
        if ev is not None:
            return ev
    return key

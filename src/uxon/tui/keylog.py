"""Permanent stdin-read tap — the lowest in-process witness of keystrokes.

For a long time the only key trace uxon had was ``app_received`` (the
message pump) and ``app_unhandled`` (routing). Both sit *above* the
terminal driver and its escape-sequence parser, so a swallowed key was
indistinguishable from "never typed": one witness, nothing to diff.

This module adds the missing witness. Textual's ``LinuxDriver`` input
thread does, per readable event::

    unicode_data = decode(os.read(fileno, 4096))   # one read
    for event in parser.feed(unicode_data): ...     # one feed, same chunk

so ``XTermParser.feed`` is called **exactly once per ``os.read``** with
that read's whole payload. Tapping ``feed`` therefore records, with no
extra syscalls and no UI slowdown, the authoritative *per-read* picture:

  * ``data``    — exactly what Python read from stdin this read (escaped),
  * ``nbytes``  — its byte length,
  * ``gap_ms``  — wall gap since the previous read.

``gap_ms`` + ``nbytes`` are the signature that localises the
keystroke-swallow *below* Python: the kernel tty input buffer is
``N_TTY_BUF_SIZE`` (4096 bytes) and in raw mode it **silently drops**
bytes once full. So a long ``gap_ms`` (reader starved — GIL held by a
render stall) followed by a near-4096-byte read (the buffer had backed
up) is the fingerprint of an overflow drop that an in-process tap can
never see *directly* (the bytes were discarded by the kernel before
``os.read`` ever returned them) but whose precondition is now logged
every session.

Diff this ``stdin_read`` stream against ``app_received`` to catch a
parser-level drop (read but no ``Key`` emitted); diff a near-4096 read
behind a long gap against an external stdin oracle to confirm a kernel
overflow drop. All three witnesses land in the SAME
``tui-debug-{user}-{date}.log``, gated by ``UXON_DEBUG=keys``.

This is deliberately committed and always-available: the operator runs
``UXON_DEBUG=keys uxon``, reproduces the swallow with *any* keys (no
fixed pattern, nothing to memorise), and the log alone tells the whole
story. Off by default; one ``frozenset`` check when disabled.
"""

from __future__ import annotations

import time

from uxon.infra.events import debug as _debug
from uxon.infra.events import is_enabled as _debug_enabled


def install_stdin_tap() -> bool:
    """Tap ``XTermParser.feed`` to log every stdin read. Idempotent.

    Returns ``True`` if the tap is installed (or already was), ``False``
    if it could not be installed (textual missing / unexpected internal
    shape) — never raises, instrumentation must not break startup.

    Only installs when ``UXON_DEBUG=keys`` is active: when disabled the
    real ``feed`` is left untouched so production pays nothing.
    """
    if not _debug_enabled("keys"):
        return False
    try:
        from textual._xterm_parser import XTermParser
    except Exception:
        return False

    # Idempotent: re-creating the app per launch handoff must not stack
    # wrappers (which would multiply the per-read log lines).
    if getattr(XTermParser.feed, "_uxon_stdin_tap", False):
        return True

    _orig_feed = XTermParser.feed
    # Mutable cell for the previous read's monotonic clock. A list, not a
    # closure-rebind, so the nested function can update it without
    # ``nonlocal`` gymnastics across the module boundary.
    _last = [None]  # type: list[float | None]

    def _tapped_feed(self, data, _orig=_orig_feed, _last=_last):
        try:
            now = time.monotonic()
            prev = _last[0]
            _last[0] = now
            # ``data`` is the decoded str of one ``os.read``; its byte
            # length is what counts against the 4096 tty buffer.
            raw = data.encode("utf-8", "replace")
            _debug(
                "keys",
                at="stdin_read",
                # latin-1 keeps every byte visible and JSON-safe, so
                # escape sequences / control bytes survive in the log.
                data=raw.decode("latin-1"),
                nbytes=len(raw),
                nchars=len(data),
                gap_ms=None if prev is None else round((now - prev) * 1000, 1),
                mono=now,
            )
        except Exception:
            # Telemetry only — never let the tap break the input thread.
            pass
        return _orig(self, data)

    _tapped_feed._uxon_stdin_tap = True  # type: ignore[attr-defined]
    try:
        XTermParser.feed = _tapped_feed  # type: ignore[assignment]
    except Exception:
        return False
    return True

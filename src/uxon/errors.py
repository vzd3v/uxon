# SPDX-License-Identifier: MIT
"""Bottom-leaf error primitives: stderr + ``SystemExit``.

Zero internal dependencies (stdlib only) so every layer
(``domain``/``infra``/``app``/``cli``/``tui``) may import it without creating a
cycle.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from io import StringIO
from typing import NoReturn

_stderr_target: ContextVar[StringIO | None] = ContextVar("uxon_stderr_target", default=None)


@contextmanager
def capture_diagnostics() -> Iterator[StringIO]:
    """Capture Uxon diagnostics in this context, preserving the terminal stream."""
    buffer = StringIO()
    token = _stderr_target.set(buffer)
    try:
        yield buffer
    finally:
        _stderr_target.reset(token)


def eprint(msg: str) -> None:
    print(msg, file=_stderr_target.get() or sys.stderr)


def fail(msg: str, code: int = 2) -> NoReturn:
    eprint(f"uxon: {msg}")
    # Stash the human-readable message on the exception object so a
    # ``try/except SystemExit`` upstream (e.g. main()'s ``config.error``
    # audit emit) can recover it. Without this, ``str(ex)`` on a
    # ``SystemExit(int_code)`` yields just ``"1"`` / ``"2"``.
    err = SystemExit(code)
    err.uxon_msg = msg  # type: ignore[attr-defined]
    raise err

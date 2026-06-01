# SPDX-License-Identifier: MIT
"""Bottom-leaf error primitives: stderr + ``SystemExit``.

Zero internal dependencies (stdlib only) so every layer
(``domain``/``infra``/``app``/``cli``/``tui``) may import it without creating a
cycle.
"""

from __future__ import annotations

import sys
from typing import NoReturn


def eprint(msg: str) -> None:
    print(msg, file=sys.stderr)


def fail(msg: str, code: int = 2) -> NoReturn:
    eprint(f"uxon: {msg}")
    # Stash the human-readable message on the exception object so a
    # ``try/except SystemExit`` upstream (e.g. main()'s ``config.error``
    # audit emit) can recover it. Without this, ``str(ex)`` on a
    # ``SystemExit(int_code)`` yields just ``"1"`` / ``"2"``.
    err = SystemExit(code)
    err.uxon_msg = msg  # type: ignore[attr-defined]
    raise err

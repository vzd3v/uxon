# SPDX-License-Identifier: MIT
"""Callback-stderr plumbing for the TUI bridge (stdlib leaf).

Converts uxon's ``fail() → SystemExit`` style into a structured error the
TUI can render in red without the blessed fullscreen context swallowing
the message. Imports nothing from ``uxon`` — the error class is injected
by the caller.
"""

from __future__ import annotations

import contextlib
import io as _io
from typing import Any


def _sanitize_callback_stderr(raw: str) -> str:
    """Strip boilerplate (``uxon:`` prefix, trailing blank lines) from
    captured stderr so it reads cleanly on a TUI status line.

    Keeps multi-line lists intact (e.g. allowed-roots bullets) with their
    indentation normalised to two spaces. Called by
    :func:`_wrap_tui_callback`.
    """
    out: list[str] = []
    for line in raw.splitlines():
        line = line.rstrip()
        if not line:
            continue
        if line.startswith("uxon:   - "):
            out.append("  - " + line[len("uxon:   - ") :])
        elif line.startswith("uxon: "):
            out.append(line[len("uxon: ") :])
        else:
            out.append(line)
    return "\n".join(out)


def _wrap_tui_callback(fn: Any, callback_error_cls: type[Exception]) -> Any:
    """Wrap a callback so exceptions surface on the TUI status line.

    Captures anything the callback writes to ``stderr`` (e.g. the message
    :func:`fail` prints before ``raise SystemExit``), and on exception
    raises ``callback_error_cls`` with the captured text as its payload.
    A plain return is passed through untouched.

    This is the single place that converts uxon's ``fail() → SystemExit``
    style into a structured error the TUI can render in red without the
    blessed fullscreen context swallowing the message.
    """

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        buf = _io.StringIO()
        try:
            with contextlib.redirect_stderr(buf):
                return fn(*args, **kwargs)
        except SystemExit as exc:
            msg = _sanitize_callback_stderr(buf.getvalue())
            if not msg:
                code = exc.code if exc.code is not None else "?"
                msg = f"command exited with code {code}"
            raise callback_error_cls(msg) from exc
        except callback_error_cls:
            raise
        except Exception as exc:  # pragma: no cover — defensive
            detail = _sanitize_callback_stderr(buf.getvalue())
            head = str(exc) or exc.__class__.__name__
            msg = f"{head}\n{detail}" if detail else head
            raise callback_error_cls(msg) from exc

    wrapper.__name__ = getattr(fn, "__name__", "wrapped_callback")
    return wrapper

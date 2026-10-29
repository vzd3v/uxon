# SPDX-License-Identifier: MIT
"""Callback-stderr plumbing for the TUI bridge (stdlib leaf).

Converts uxon's ``fail() → SystemExit`` style into a structured error the
TUI can render without losing the message during fullscreen handoff.
The error class is injected by the caller.
"""

from __future__ import annotations

from typing import Any

from uxon.errors import capture_diagnostics


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

    Captures Uxon diagnostics written through ``eprint`` / ``fail``, and on exception
    raises ``callback_error_cls`` with the captured text as its payload.
    A plain return is passed through untouched. Third-party direct stderr stays
    with the current terminal or Textual capture; no process-global stream is changed.

    This is the single place that converts uxon's ``fail() → SystemExit``
    style into a structured error the TUI can render in red without the
    fullscreen context swallowing the message.
    """

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            with capture_diagnostics() as buf:
                return fn(*args, **kwargs)
        except SystemExit as exc:
            msg = _sanitize_callback_stderr(buf.getvalue()) or getattr(exc, "uxon_msg", "")
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

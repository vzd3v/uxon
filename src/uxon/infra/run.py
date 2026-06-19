# SPDX-License-Identifier: MIT
"""The single sanctioned primitive for background (non-interactive) spawns.

Every piece of background work that shells out — probes, queries, git,
container/remote commands — must go through :func:`run_query`. It detaches the
child from uxon's controlling terminal *by construction* so the child can never
flush the TUI's pending keystrokes.

The swallow this prevents (see
``docs/specs/2026-06-18-tui-subprocess-terminal-isolation.md``): a worker's
``tmux`` / ``ssh`` / ``sudo`` / ``git`` child inherits uxon's controlling
terminal, switches it to raw mode, and on entry or restore-at-exit *flushes its
input queue* (``tcflush``), dropping whatever the user typed while the child
ran — keys already in the kernel pty buffer but not yet ``os.read`` by Textual's
input thread.

Two settings make that impossible here:

- ``start_new_session=True`` — the child leads its own session with **no
  controlling terminal**, so ``open("/dev/tty")`` fails with ``ENXIO`` and it
  physically cannot ``tcflush`` uxon's input.
- ``stdin=DEVNULL`` (or a pipe fed from ``input``) and captured
  ``stdout``/``stderr`` — the child never shares a tty fd.

The std-stream / session knobs are deliberately **not** exposed to callers:
those are the very settings that recreate the swallow. Intent is the function
you call, not the kwargs you pass.

This module imports no ``textual`` (it sits at the ``infra`` layer, below the
TUI) and never blocks on the event loop — callers run it off-loop in a worker.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence

__all__ = ["run_query"]


def run_query(
    argv: Sequence[str],
    *,
    timeout: float | None = None,
    text: bool = True,
    env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    input: str | bytes | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """Run ``argv`` to completion, detached from the controlling terminal.

    Captures stdout/stderr and returns the :class:`subprocess.CompletedProcess`.
    The child has no controlling terminal (``start_new_session=True``) and no
    tty stdin (``DEVNULL``, or a pipe when ``input`` is given), so it cannot
    flush the TUI's buffered keystrokes.

    ``check=True`` raises :class:`subprocess.CalledProcessError` on a non-zero
    exit, exactly like :func:`subprocess.run`.
    """
    # ``subprocess.run`` requires stdin to be unset when ``input`` is given (it
    # wires up the pipe itself); otherwise point stdin at /dev/null so the child
    # can never read — and thereby steal — the user's pending keystrokes.
    stdin = None if input is not None else subprocess.DEVNULL
    return subprocess.run(
        list(argv),
        stdin=stdin,
        input=input,
        capture_output=True,
        start_new_session=True,
        timeout=timeout,
        text=text,
        env=dict(env) if env is not None else None,
        cwd=cwd,
        check=check,
    )

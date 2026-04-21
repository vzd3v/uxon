"""pty-driven TUI test harness for ccw.

Forks a child process running a given Python script under a controlling
pseudo-terminal, writes keystrokes into it, reads back the rendered
frames, and returns a trace that tests can assert against.

Standard library only — ``pty``, ``os``, ``select``, ``re``, ``time``,
``struct``, ``fcntl``, ``termios``, ``signal``. No external deps. Tests
that use this harness must guard with
``@unittest.skipUnless(hasattr(pty, 'fork'), ...)`` so they skip on
platforms without a working pty (pure-Windows builds).
"""

from __future__ import annotations

import fcntl
import os
import re
import select
import signal
import struct
import sys
import termios
import time
from dataclasses import dataclass, field


# ANSI / terminal control sequences we want to strip before matching.
_ANSI_CSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_CHARSET = re.compile(rb"\x1b\([AB0]")
_ANSI_MODE = re.compile(rb"\x1b[=>]")

# Adaptive-drain idle threshold (milliseconds). After the first data byte
# in any drain window, `_drain` returns as soon as the pty has been quiet
# for this many ms instead of sleeping the full budget. Before the first
# byte, the full remaining budget is used so slow-starting processes
# (textual import takes 2–3 s) are not cut short.
_IDLE_MS = 250


def strip_ansi(data: bytes) -> str:
    """Remove ANSI escape sequences and decode to text."""
    data = _ANSI_CSI.sub(b"", data)
    data = _ANSI_CHARSET.sub(b"", data)
    data = _ANSI_MODE.sub(b"", data)
    return data.decode("utf-8", "replace")


@dataclass
class PtyTrace:
    """Transcript of a pty-driven TUI session.

    ``raw`` is the concatenation of every byte we read off the pty.
    ``plain`` is the ANSI-stripped, decoded text — usable for substring
    / regex assertions. ``frames`` is the list of drain boundaries,
    each frame being the cumulative plain text at the point the test
    harness paused for output to settle.
    """

    raw: bytes = b""
    frames: list[str] = field(default_factory=list)
    exit_code: int | None = None

    @property
    def plain(self) -> str:
        return strip_ansi(self.raw)

    def last_frame(self) -> str:
        return self.frames[-1] if self.frames else ""

    def contains(self, needle: str) -> bool:
        return needle in self.plain


def run_pty(
    argv: "list[str]",
    keys: "list[tuple[float, bytes]] | list[bytes]",
    *,
    env: "dict[str, str] | None" = None,
    rows: int = 40,
    cols: int = 140,
    initial_drain: float = 6.0,
    per_key_drain: float = 0.4,
    final_drain: float = 0.8,
    timeout: float = 30.0,
) -> PtyTrace:
    """Spawn ``argv`` under a pty, send each key (with pauses), collect output.

    ``keys`` may be either:
      * a list of ``bytes`` — each is sent with ``per_key_drain`` pause after,
      * or a list of ``(delay_seconds, bytes)`` tuples for fine-grained control.

    ``initial_drain``, ``per_key_drain``, and ``final_drain`` are **upper
    bounds**, not fixed sleeps. Each drain waits for the full remaining budget
    before the first byte arrives (so slow-starting processes like textual,
    which can take 2–3 s to import, are not cut short). Once data has started
    flowing, the drain exits as soon as the pty has been idle for ``_IDLE_MS``
    milliseconds (default 250). All values cap the worst-case wait.

    Returns a :class:`PtyTrace` with the combined raw output, per-frame
    snapshots, and the child's exit code.
    """
    try:
        import pty  # type: ignore[import]
    except ImportError as exc:  # pragma: no cover — Windows only
        raise RuntimeError("pty module unavailable on this platform") from exc

    pid, fd = pty.fork()
    if pid == 0:
        # Child: set TERM and exec the requested command.
        if env:
            for k, v in env.items():
                os.environ[k] = v
        os.environ.setdefault("TERM", "xterm-256color")
        os.environ.setdefault("COLUMNS", str(cols))
        os.environ.setdefault("LINES", str(rows))
        try:
            os.execvp(argv[0], argv)
        except OSError:
            os._exit(127)

    # Parent.
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except Exception:
        pass

    trace = PtyTrace()
    deadline_outer = time.monotonic() + timeout

    def _drain(max_secs: float) -> None:
        idle = _IDLE_MS / 1000.0
        deadline = min(time.monotonic() + max_secs, deadline_outer)
        got_data = False
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if got_data:
                # After first data: apply idle window so we exit quickly
                # once the pty goes quiet instead of sleeping the full budget.
                timeout_for_select = min(idle, remaining)
            else:
                # Before any data: wait up to the full remaining budget so
                # slow-starting processes (e.g. textual import) are not cut
                # short before the TUI has produced any output.
                timeout_for_select = remaining
            rlist, _, _ = select.select([fd], [], [], timeout_for_select)
            if not rlist:
                # select timed out — either truly idle for `idle` (after
                # data was seen), or hit `remaining` near deadline. Stop.
                return
            try:
                chunk = os.read(fd, 8192)
            except OSError:
                return
            if not chunk:
                return
            got_data = True
            trace.raw += chunk

    try:
        _drain(initial_drain)
        trace.frames.append(trace.plain)

        for item in keys:
            if isinstance(item, tuple):
                delay, payload = item
            else:
                delay, payload = per_key_drain, item
            try:
                os.write(fd, payload)
            except OSError:
                break
            _drain(delay)
            trace.frames.append(trace.plain)

        _drain(final_drain)
        trace.frames.append(trace.plain)
    finally:
        # Reap the child. First try a clean kill; if it's already gone,
        # the waitpid below will return quickly.
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            _, status = os.waitpid(pid, os.WNOHANG)
            if status == 0:
                # Still alive; give it a moment, then SIGKILL.
                time.sleep(0.1)
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
                _, status = os.waitpid(pid, 0)
            trace.exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else None
        except ChildProcessError:
            trace.exit_code = None
        try:
            os.close(fd)
        except OSError:
            pass

    return trace


def run_python_snippet(
    code: str,
    keys: "list[bytes]",
    *,
    extra_path: "list[str] | None" = None,
    **kwargs,
) -> PtyTrace:
    """Convenience: spawn ``python3 -c <code>`` under a pty with extra sys.path
    entries prepended. Used by tests to drive ``ccw_tui.run(ctx)`` with a
    fake TuiContext without involving the full ccw binary.
    """
    env = dict(os.environ)
    if extra_path:
        existing = env.get("PYTHONPATH", "")
        prepend = os.pathsep.join(extra_path)
        env["PYTHONPATH"] = prepend + (os.pathsep + existing if existing else "")
    argv = [sys.executable, "-c", code]
    return run_pty(argv, keys, env=env, **kwargs)

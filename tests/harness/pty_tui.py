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
    initial_drain: float = 1.5,
    per_key_drain: float = 0.4,
    final_drain: float = 0.8,
    timeout: float = 30.0,
) -> PtyTrace:
    """Spawn ``argv`` under a pty, send each key (with pauses), collect output.

    ``keys`` may be either:
      * a list of ``bytes`` — each is sent with ``per_key_drain`` pause after,
      * or a list of ``(delay_seconds, bytes)`` tuples for fine-grained control.

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
    deadline = time.monotonic() + timeout

    def _drain(secs: float) -> None:
        end = min(time.monotonic() + secs, deadline)
        while time.monotonic() < end:
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            rlist, _, _ = select.select([fd], [], [], min(0.2, remaining))
            if fd in rlist:
                try:
                    chunk = os.read(fd, 8192)
                except OSError:
                    return
                if not chunk:
                    return
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

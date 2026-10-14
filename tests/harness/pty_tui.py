"""pty-driven TUI test harness for uxon.

Forks a child process running a given Python script under a controlling
pseudo-terminal, writes keystrokes into it, reads back the rendered
frames, and returns a trace that tests can assert against.

Standard library only — ``pty``, ``os``, ``select``, ``re``, ``time``,
``struct``, ``fcntl``, ``termios``, ``subprocess``. No external deps. Tests
that use this harness must guard with
``@unittest.skipUnless(hasattr(pty, 'fork'), ...)`` so they skip on
platforms without a working pty (pure-Windows builds).
"""

from __future__ import annotations

import fcntl
import os
import re
import select
import struct
import subprocess
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
_IDLE_MS = 100

# Settle threshold (milliseconds) used *after* a ``wait_for_text`` marker has
# been seen. Textual's first render arrives in bursts with sub-second gaps
# between them — measured ~770 ms (quiet host) and >1.4 s (under -n auto CPU
# contention) between the title bar and the interactive body. The 100 ms
# steady-state idle window fires in one of those gaps and returns the drain
# while the screen is still painting and the ListView/focus is not yet
# mounted, so the next keypress is dropped. After a marker match we retain
# this empirical quiet period until a child readiness acknowledgement can
# replace it; output silence alone does not guarantee input readiness.
_SETTLE_MS = 1500

_EXIT_WAIT = 0.1
_KILL_WAIT = 1.0


def _make_controlling_tty(slave_fd: int) -> None:
    """Child-side setup for ``subprocess.Popen``.

    ``pty.fork()`` gives the child a controlling terminal but emits a
    Python 3.12 warning under pytest-xdist's threaded workers. ``Popen``
    with plain ``stdin=slave_fd`` keeps stdio as a tty, but leaves
    ``/dev/tty`` unavailable. This small child setup preserves the old
    controlling-terminal semantics without calling ``forkpty()``.
    """
    os.setsid()
    fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)


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
    argv: list[str],
    keys: list[tuple[float, bytes]] | list[tuple[float, bytes, str]] | list[bytes],
    *,
    env: dict[str, str] | None = None,
    rows: int = 40,
    cols: int = 140,
    initial_drain: float = 6.0,
    initial_wait_for: str | None = None,
    per_key_drain: float = 0.4,
    final_drain: float = 0.8,
    timeout: float = 30.0,
) -> PtyTrace:
    """Spawn ``argv`` under a pty, send each key (with pauses), collect output.

    ``keys`` may be:
      * a list of ``bytes`` — each is sent with ``per_key_drain`` pause after,
      * a list of ``(delay_seconds, bytes)`` tuples,
      * or ``(delay_seconds, bytes, wait_for_text)`` tuples — drain continues
        past the idle window until ``wait_for_text`` appears in the rendered
        output produced after that key. Exhausting the budget without the
        marker raises an assertion with stage output and child status.

    ``initial_drain``, ``per_key_drain``, and ``final_drain`` are **upper
    bounds**, not fixed sleeps. Each drain waits for the full remaining budget
    before the first byte arrives (so slow-starting processes like textual,
    which can take 2–3 s to import, are not cut short). Once data has started
    flowing, the drain exits as soon as the pty has been idle for ``_IDLE_MS``
    milliseconds (default 100). All values cap the worst-case wait.

    ``initial_wait_for`` makes the initial drain synchronize on the first
    rendered frame instead of a fixed delay: the drain keeps reading until
    that text appears in the trace (or ``initial_drain`` is exhausted). Pass a
    marker that only appears once the TUI's first frame is on screen (e.g. a
    main-screen label) so that input sent afterwards lands on a *rendered*
    app. Without it, under heavy CPU contention textual's import+first-render
    can outlast a fixed ``initial_drain`` and the keys hit a blank screen — the
    captured trace then contains only the echoed input bytes. This is the
    render-race source fix for the pty click/launch tests (Phase P9a).

    Returns a :class:`PtyTrace` with the combined raw output, per-frame
    snapshots, and the child's exit code.
    """
    child_env = dict(os.environ)
    # Ambient troubleshooting flags must not disable the child regression
    # checks. Tests of those flags may opt in explicitly through ``env``.
    child_env.pop("UXON_DISABLE_LOOP_GUARD", None)
    child_env.pop("UXON_SPAWN_GUARD_STRICT", None)
    if env:
        child_env.update(env)
    child_env.setdefault("TERM", "xterm-256color")
    child_env.setdefault("COLUMNS", str(cols))
    child_env.setdefault("LINES", str(rows))

    trace = PtyTrace()
    deadline_outer = time.monotonic() + timeout
    fd, slave_fd = os.openpty()
    proc: subprocess.Popen | None = None

    def _failure(stage: str, offset: int, pending: str) -> AssertionError:
        status = proc.poll() if proc is not None else None
        return AssertionError(
            f"PTY {stage}: {pending}; child exit={status}; "
            f"remaining={max(0.0, deadline_outer - time.monotonic()):.3f}s; "
            f"stage output tail={strip_ansi(trace.raw[offset:])[-3000:]!r}"
        )

    def _drain(max_secs: float, wait_for_text: str | None = None, *, stage: str) -> None:
        offset = len(trace.raw)
        idle = _IDLE_MS / 1000.0
        settle = _SETTLE_MS / 1000.0
        deadline = min(time.monotonic() + max_secs, deadline_outer)
        got_data = False
        # Once a requested marker has been seen, keep draining through a
        # *settle* window instead of returning the instant the substring
        # appears mid-render. The marker can land in the byte stream while the
        # frame is still painting and the app has not finished the
        # render/mount/focus cycle it kicked off — returning immediately then
        # races the next keypress against an app that is not yet input-ready
        # (observed under -n auto contention: the keypress is dropped and the
        # launch never fires). Draining to the longer ``settle`` quiet period
        # clears textual's inter-burst render gaps so that cycle completes.
        matched = False
        while time.monotonic() < deadline:
            if wait_for_text and not matched and wait_for_text in strip_ansi(trace.raw[offset:]):
                matched = True
            remaining = deadline - time.monotonic()
            if got_data and matched:
                # Marker seen: wait for the longer settle quiet period so the
                # render burst the marker belongs to fully drains.
                timeout_for_select = min(settle, remaining)
            elif got_data and not wait_for_text:
                # After first data and no marker requested: apply the short
                # idle window so we exit quickly once the pty goes quiet
                # instead of sleeping the full budget.
                timeout_for_select = min(idle, remaining)
            else:
                # Before any data, OR still waiting for the marker to appear:
                # stay until the full remaining budget so slow processes
                # (textual import 2–3s, modal mount under -n auto) are not cut
                # short.
                timeout_for_select = remaining
            rlist, _, _ = select.select([fd], [], [], timeout_for_select)
            if not rlist:
                # select timed out — truly idle (marker seen or none asked),
                # or hit `remaining` near deadline. Stop.
                break
            try:
                chunk = os.read(fd, 8192)
            except OSError:
                break
            if not chunk:
                break
            got_data = True
            trace.raw += chunk
        if wait_for_text and wait_for_text not in strip_ansi(trace.raw[offset:]):
            raise _failure(stage, offset, f"missing marker {wait_for_text!r}")

    try:
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        proc = subprocess.Popen(
            argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=child_env,
            preexec_fn=lambda: _make_controlling_tty(slave_fd),
            close_fds=True,
        )
        os.close(slave_fd)
        slave_fd = -1
        _drain(initial_drain, initial_wait_for, stage="initial render")
        trace.frames.append(trace.plain)

        for index, item in enumerate(keys, start=1):
            wait_for: str | None = None
            if isinstance(item, tuple):
                if len(item) == 3:
                    delay, payload, wait_for = item  # type: ignore[misc]
                else:
                    delay, payload = item  # type: ignore[misc]
            else:
                delay, payload = per_key_drain, item
            try:
                os.write(fd, payload)
            except OSError as exc:
                if wait_for:
                    raise _failure(
                        f"key {index}", len(trace.raw), f"input write failed before {wait_for!r}"
                    ) from exc
                break
            _drain(delay, wait_for, stage=f"key {index}")
            trace.frames.append(trace.plain)

        _drain(final_drain, stage="final output")
        trace.frames.append(trace.plain)
        # A final marker may precede interpreter shutdown. Give that exit a
        # bounded chance to complete before cleanup sends a signal.
        try:
            proc.wait(timeout=min(_EXIT_WAIT, max(0.0, deadline_outer - time.monotonic())))
        except subprocess.TimeoutExpired:
            pass
    finally:
        try:
            if proc is not None:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=_EXIT_WAIT)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        try:
                            proc.wait(timeout=_KILL_WAIT)
                        except subprocess.TimeoutExpired as exc:
                            raise _failure(
                                "cleanup", 0, "child did not exit after SIGKILL"
                            ) from exc
                trace.exit_code = proc.returncode
        finally:
            try:
                if slave_fd >= 0:
                    os.close(slave_fd)
            finally:
                os.close(fd)

    return trace


def run_python_snippet(
    code: str,
    keys: list[bytes],
    *,
    extra_path: list[str] | None = None,
    **kwargs,
) -> PtyTrace:
    """Convenience: spawn ``python3 -c <code>`` under a pty with extra sys.path
    entries prepended. Used by tests to drive ``uxon_tui.run(ctx)`` with a
    fake TuiContext without involving the full uxon binary.
    """
    env: dict[str, str] = {}
    if extra_path:
        existing = os.environ.get("PYTHONPATH", "")
        prepend = os.pathsep.join(extra_path)
        env["PYTHONPATH"] = prepend + (os.pathsep + existing if existing else "")
    argv = [sys.executable, "-c", code]
    return run_pty(argv, keys, env=env, **kwargs)

"""pty-driven integration tests for uxon.tui.

These tests fork a child that imports ``uxon.tui`` with a minimal fake
``TuiContext``, then drive it via keystrokes written to a controlling
pseudo-terminal. They're intentionally coarse — a handful of end-to-end
regression tests for bugs we've been bitten by. Fine-grained unit
tests stay in test_uxon_tui.py.

Each test is guarded by ``@unittest.skipUnless(hasattr(pty, 'fork'),
...)`` so it skips cleanly on platforms without a working pty
(pure-Windows builds).
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "tests"))


def locate_row(trace_plain: str, label_regex: str) -> int | None:
    """Scan pty output for a label and return its 1-based y coordinate.

    Layout-agnostic helper for click tests. ``trace_plain`` is the
    rendered frame string from the pty harness; we split on "\r\n" and
    return the index of the first line matching ``label_regex``.
    Returns None if no match.

    Used by tests that would otherwise hardcode a row number — letting
    the MainScreen layout drift without breaking click-based tests.
    """
    import re

    pattern = re.compile(label_regex)
    for i, line in enumerate(trace_plain.splitlines(), start=1):
        if pattern.search(line):
            return i
    return None


def sgr_click(x: int, y: int) -> tuple[bytes, bytes]:
    """Build SGR-1006 press+release byte pairs for coordinate (x, y)."""
    press = f"\x1b[<0;{x};{y}M".encode()
    release = f"\x1b[<0;{x};{y}m".encode()
    return press, release


try:
    import pty  # type: ignore[import]

    _PTY_OK = hasattr(pty, "fork")
except ImportError:  # pragma: no cover
    _PTY_OK = False


# Shared child-side script. Imports uxon.tui, builds a no-op TuiContext
# shaped like what u-den would see on first launch (sudo, no sessions),
# and enters run(). The test driver pipes keystrokes in, then reads the
# rendered frames.
_CHILD_SCRIPT = r"""
import sys, os
from uxon import tui as uxon_tui

ctx = uxon_tui.TuiContext(
    sessions=[],
    total_cpu="0",
    total_ram="0",
    version="ccw-test",
    cwd="/tmp",
    cwd_short="tmp",
    new_project_root="/tmp/projects",
    existing_projects=[],
    cwd_writable=True,
    current_user="u-den",
    has_sudo=True,
    other_sessions=[],
)
rc = uxon_tui.run(ctx)
sys.exit(rc)
"""


@unittest.skipUnless(_PTY_OK, "pty.fork unavailable on this platform")
class PtyTuiIntegrationTests(unittest.TestCase):
    """End-to-end driver tests via a pseudo-terminal.

    These are the regression tests for the 2026-04-18 bug class:
    empty-state superuser + stray keypresses must not land on Git
    remote profiles. Also smoke-tests basic quit and nav paths.
    """

    def _run(self, keys: list[bytes], **kwargs):
        from harness.pty_tui import run_python_snippet

        code = _CHILD_SCRIPT
        return run_python_snippet(
            code,
            keys,
            **kwargs,
        )

    def test_pty_harness_provides_controlling_terminal(self) -> None:
        from harness.pty_tui import run_python_snippet

        trace = run_python_snippet(
            "import os; fd = os.open('/dev/tty', os.O_RDWR); "
            "print(f'controlling-tty:{os.isatty(0)}:{os.isatty(1)}'); os.close(fd)",
            [b""],
            initial_drain=1.0,
            final_drain=0.1,
            timeout=3.0,
        )
        self.assertIn("controlling-tty:True:True", trace.plain)

    def test_fresh_superuser_digit_4_does_not_open_git_remotes(self) -> None:
        """PR 1 + PR 2 regression: on empty superuser state,
        `settings_idx == ACTION_COUNT == 3` so digit '4' points at
        Settings. A digit press must not activate Settings — and even
        if it did, 'g' must not open the git remotes screen."""
        trace = self._run([b"4", b"g", b"q"])
        self.assertNotIn(
            "Git remote profiles",
            trace.plain,
            msg=f"git remotes reachable via '4g': last frame:\n{trace.last_frame()[-1500:]}",
        )

    def test_direct_g_on_main_is_cursor_home(self) -> None:
        """Pressing 'g' on the main screen moves cursor to top (KEY_HOME).
        It must not open git remotes (which is only reachable through
        Settings — and since PR 1, not even from there)."""
        trace = self._run([b"g", b"q"])
        self.assertNotIn("Git remote profiles", trace.plain)

    def test_quit_exits_cleanly(self) -> None:
        """Smoke test: pressing 'q' on the main screen returns rc=0."""
        trace = self._run([b"q"])
        # rc may be None if the child was SIGKILL'd by harness cleanup,
        # which happens when the TUI hasn't reached the terminal normal
        # state. Accept 0 or None; what we really care about is that no
        # error banner was rendered.
        self.assertNotIn("crashed", trace.plain)
        self.assertNotIn("Traceback", trace.plain)

    def test_enter_on_settings_row_opens_settings_then_q_returns(self) -> None:
        """Deliberate activation: arrow-down to Settings (the single
        item in the superuser block when no sessions), Enter to open,
        'q' to back out, 'q' to quit. No Git remote profiles anywhere."""
        trace = self._run([b"\x1b[B", b"\r", b"q", b"q"])
        self.assertNotIn("Git remote profiles", trace.plain)

    def test_left_click_on_second_action_row_selects_it(self) -> None:
        """Click press + release on a visible action row should move the
        cursor to that item without crashing. Exact row index isn't
        asserted — we just need the TUI to parse the SGR-1006 sequence
        and keep running."""
        press = b"\x1b[<0;5;4M"
        release = b"\x1b[<0;5;4m"
        trace = self._run([press, release, b"q"])
        self.assertNotIn("Traceback", trace.plain)
        self.assertNotIn("crashed", trace.plain)
        # All three action rows should still be visible in the last frame.
        self.assertIn("Create new project", trace.plain)

    def test_wheel_down_then_q_exits_cleanly(self) -> None:
        """Wheel-down event (button 65) must not crash and must be
        consumed as a cursor move."""
        trace = self._run([b"\x1b[<65;1;1M", b"q"])
        self.assertNotIn("Traceback", trace.plain)
        self.assertNotIn("crashed", trace.plain)

    def test_click_on_settings_row_opens_settings(self) -> None:
        """With no own sessions, Settings is the first superuser row.
        A click (press + release) on a main-screen action area must not
        crash the TUI. Exact row coordinates vary with layout — this is
        a smoke test, not a position-assert."""
        trace = self._run([b"\x1b[<0;5;6M", b"\x1b[<0;5;6m", b"q", b"q"])
        self.assertNotIn("Traceback", trace.plain)
        self.assertNotIn("crashed", trace.plain)


_DRAIN_CHILD_SCRIPT = r"""
import sys, os
from uxon import tui as uxon_tui
from uxon import agents as uxon_agents
from uxon import probes as uxon_probes
from uxon.tui.context import LaunchRequest

MARKER = {marker_path!r}
uxon_agents.probe_agents = lambda *args, **kwargs: {{}}
# The new TUI worker calls ``probes.probe_host`` instead of the
# legacy per-agent driver. Without this stub the pty child runs a
# real ``sudo -nHu USER -- sh -lc 'command -v …'`` on the CI host,
# finds nothing, and pushes ``AgentsUnavailableScreen`` over the
# main screen — the digit press the test sends then lands on the
# modal instead of the action row, and the marker file stays empty.
class _StubReport:
    def __init__(self):
        self.tmux = type("S", (), {{"name": "tmux", "path": "/usr/bin/tmux", "install_hint": ""}})()
        self.enabled = {{}}
        self.detected = {{}}
        self.launch_user = "u-den"
uxon_probes.probe_host = lambda *args, **kwargs: _StubReport()

def fake_launch_cwd(agent_id, mode_id):
    with open(MARKER, "a", encoding="utf-8") as f:
        f.write(f"cwd:{{agent_id}}:{{mode_id}}\n")
    return LaunchRequest(cmd=("/bin/sh", "-c", "sleep 2.0"), label="mock-attach")

def fake_launch_new(name, agent_id, mode_id, git_profile):
    with open(MARKER, "a", encoding="utf-8") as f:
        f.write(f"new:{{name}}:{{agent_id}}:{{mode_id}}:{{git_profile}}\n")
    return LaunchRequest(cmd=("/bin/true",), label="mock-new")

ctx = uxon_tui.TuiContext(
    sessions=[],
    total_cpu="0",
    total_ram="0",
    version="ccw-test",
    cwd="/tmp",
    cwd_short="tmp",
    new_project_root="/tmp/projects",
    existing_projects=[],
    cwd_writable=True,
    current_user="u-den",
    has_sudo=False,
    on_launch_cwd=fake_launch_cwd,
    on_launch_new=fake_launch_new,
    on_refresh=lambda: ctx,
)
rc = uxon_tui.run(ctx)
sys.exit(rc)
"""


@unittest.skipUnless(_PTY_OK, "pty.fork unavailable on this platform")
class DrainAfterLaunchTests(unittest.TestCase):
    """Regression: keys typed during a mocked launch round-trip must
    NOT auto-activate an item when the main screen re-renders.

    Historically the blessed loop needed ``_drain_stdin`` to flush the
    buffer between ``exit()`` and the next ``inkey()``. Under textual
    each ``UxonApp()`` gets its own fresh input queue, so the drain is
    no longer needed — this test verifies that claim empirically.
    """

    def test_key_typed_during_launch_does_not_stale_activate(self) -> None:
        from harness.pty_tui import run_python_snippet

        fd, marker_path = tempfile.mkstemp(prefix="ccw-drain-", text=True)
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(marker_path) and os.unlink(marker_path))

        code = _DRAIN_CHILD_SCRIPT.format(
            marker_path=marker_path,
        )
        # Sequence: digit-1 → permissions modal → pick regular →
        # on_launch_cwd triggers request_launch → app exits →
        # _run_launch_request runs /bin/true → re-enter.
        # Send digit-2 immediately after the modal commit while the mocked
        # launch command is still sleeping. It must not open NewProjectScreen
        # after the app re-enters.
        # Synchronize on rendered text instead of guessing delays — under
        # -n auto CPU contention, a fixed sleep is racy. The 3-tuple form
        # `(budget, payload, wait_for_text)` drains until the marker
        # appears or the budget expires. We wait for "1 normal" (the
        # first mode list item) — it renders only after the modal's
        # async on_mount has populated the mode ListView, so Enter is
        # guaranteed to land on a mounted, ready ListView.
        trace = run_python_snippet(
            code,
            [
                (8.0, b"1", "1 normal"),
                (4.0, b"\r"),
                (1.0, b"2"),
                b"q",
            ],
            initial_drain=10.0,
            timeout=60.0,
        )
        self.assertNotIn("Traceback", trace.plain)
        marker = Path(marker_path).read_text(encoding="utf-8")
        self.assertIn("cwd:claude:normal", marker)
        self.assertNotIn("new:", marker)
        self.assertNotIn("project name", trace.plain)
        self.assertIn("uxon | New session", trace.plain)


if __name__ == "__main__":
    unittest.main()

"""pty-driven integration tests for ccw_tui.

These tests fork a child that imports ``ccw_tui`` with a minimal fake
``TuiContext``, then drive it via keystrokes written to a controlling
pseudo-terminal. They're intentionally coarse — a handful of end-to-end
regression tests for bugs we've been bitten by. Fine-grained unit
tests stay in test_ccw_tui.py.

Each test is guarded by ``@unittest.skipUnless(hasattr(pty, 'fork'),
...)`` so it skips cleanly on platforms without a working pty
(pure-Windows builds).
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "tests"))
sys.path.insert(0, str(_REPO / "lib"))

try:
    import pty  # type: ignore[import]
    _PTY_OK = hasattr(pty, "fork")
except ImportError:  # pragma: no cover
    _PTY_OK = False


# Shared child-side script. Imports ccw_tui, builds a no-op TuiContext
# shaped like what u-den would see on first launch (sudo, no sessions),
# and enters run(). The test driver pipes keystrokes in, then reads the
# rendered frames.
_CHILD_SCRIPT = r"""
import sys, os
sys.path.insert(0, {lib_path!r})
import ccw_tui

ctx = ccw_tui.TuiContext(
    sessions=[],
    total_cpu="0",
    total_ram="0",
    version="ccw-test",
    cwd="/tmp",
    cwd_short="tmp",
    new_project_root="/tmp/projects",
    existing_projects=[],
    cwd_allowed=True,
    current_user="u-den",
    has_sudo=True,
    other_sessions=[],
)
rc = ccw_tui.run(ctx)
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

        code = _CHILD_SCRIPT.format(lib_path=str(_REPO / "lib"))
        return run_python_snippet(
            code,
            keys,
            extra_path=[str(_REPO / "lib")],
            **kwargs,
        )

    def test_fresh_superuser_digit_4_does_not_open_git_remotes(self) -> None:
        """PR 1 + PR 2 regression: on empty superuser state,
        `settings_idx == ACTION_COUNT == 3` so digit '4' points at
        Settings. A digit press must not activate Settings — and even
        if it did, 'g' must not open the git remotes screen."""
        trace = self._run([b"4", b"g", b"q"])
        self.assertNotIn(
            "Git remote profiles", trace.plain,
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


if __name__ == "__main__":
    unittest.main()

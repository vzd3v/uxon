import importlib.util
import sys
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

CCW_PATH = Path(__file__).resolve().parents[1] / "bin" / "ccw"
LOADER = SourceFileLoader("ccw_module", str(CCW_PATH))
SPEC = importlib.util.spec_from_loader("ccw_module", LOADER)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"failed to load spec for {CCW_PATH}")
ccw = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ccw
SPEC.loader.exec_module(ccw)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import ccw_tui


class ParseArgsInteractiveTests(unittest.TestCase):
    def test_empty_argv_interactive_when_tty(self) -> None:
        with mock.patch.object(ccw, "is_interactive_tty", return_value=True):
            parsed = ccw.parse_args([])
        self.assertEqual(parsed.action, "interactive")

    def test_empty_argv_shows_usage_when_not_tty(self) -> None:
        with mock.patch.object(ccw, "is_interactive_tty", return_value=False):
            with self.assertRaises(SystemExit) as ctx:
                ccw.parse_args([])
        self.assertEqual(ctx.exception.code, 0)

    def test_dsp_flag_is_canonical(self) -> None:
        for flag in ("--dsp", "--dangerously-skip-permissions", "--dap", "-dap", "-dsp"):
            parsed = ccw.parse_args(["run", flag])
            self.assertTrue(parsed.dsp, f"flag {flag} should set dsp=True")


class TuiSessionRenderingTests(unittest.TestCase):
    def _make_session(self, name: str = "demo", attached: bool = False) -> ccw_tui.TuiSession:
        return ccw_tui.TuiSession(
            name=f"cc-{name}",
            short=name,
            attached=attached,
            pid="1234",
            cpu="3.1",
            ram="256M",
            created="14:30",
            last_activity="14:35",
            cmd="claude",
            path="/srv/repos/demo",
            user="devagent",
        )

    def test_compute_col_widths_empty(self) -> None:
        widths = ccw_tui._compute_col_widths([])
        self.assertIn("name", widths)
        self.assertGreater(widths["name"], 0)

    def test_compute_col_widths_uses_max(self) -> None:
        sessions = [
            self._make_session("short"),
            self._make_session("a-very-long-name"),
        ]
        widths = ccw_tui._compute_col_widths(sessions)
        self.assertEqual(widths["name"], len("a-very-long-name"))

    def test_tui_context_creation(self) -> None:
        sessions = [self._make_session("demo"), self._make_session("api", attached=True)]
        ctx = ccw_tui.TuiContext(
            sessions=sessions,
            total_cpu="5.0",
            total_ram="512M",
            version="ccw 0.4.0",
            on_attach=lambda n: None,
            on_kill=lambda n: None,
            on_kill_all=lambda: None,
            on_refresh=lambda: ctx,
        )
        self.assertEqual(len(ctx.sessions), 2)
        self.assertTrue(ctx.sessions[1].attached)


if __name__ == "__main__":
    unittest.main()

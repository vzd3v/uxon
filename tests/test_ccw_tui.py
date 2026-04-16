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


def _noop(*args, **kwargs):
    pass


def _make_session(name: str = "demo", attached: bool = False) -> ccw_tui.TuiSession:
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


def _make_ctx(sessions=None, existing_projects=None) -> ccw_tui.TuiContext:
    if sessions is None:
        sessions = []
    ctx = ccw_tui.TuiContext(
        sessions=sessions,
        total_cpu="5.0",
        total_ram="512M",
        version="ccw 0.5.0",
        cwd="/srv/repos/myproject",
        cwd_short="myproject",
        new_project_root="/srv/agentdev",
        existing_projects=existing_projects or [],
        on_attach=_noop,
        on_kill=_noop,
        on_kill_all=_noop,
        on_refresh=lambda: ctx,
        on_launch_cwd=_noop,
        on_launch_new=_noop,
        on_launch_existing=_noop,
    )
    return ctx


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


class TuiDataTests(unittest.TestCase):
    def test_compute_col_widths_empty(self) -> None:
        widths = ccw_tui._compute_col_widths([])
        self.assertIn("name", widths)
        self.assertGreater(widths["name"], 0)

    def test_compute_col_widths_uses_max(self) -> None:
        sessions = [_make_session("short"), _make_session("a-very-long-name")]
        widths = ccw_tui._compute_col_widths(sessions)
        self.assertEqual(widths["name"], len("a-very-long-name"))

    def test_tui_context_has_launch_fields(self) -> None:
        ctx = _make_ctx(
            sessions=[_make_session("demo"), _make_session("api", attached=True)],
            existing_projects=["proj-a", "proj-b"],
        )
        self.assertEqual(len(ctx.sessions), 2)
        self.assertTrue(ctx.sessions[1].attached)
        self.assertEqual(ctx.cwd, "/srv/repos/myproject")
        self.assertEqual(ctx.new_project_root, "/srv/agentdev")
        self.assertEqual(ctx.existing_projects, ["proj-a", "proj-b"])

    def test_total_items_includes_actions(self) -> None:
        ctx = _make_ctx(sessions=[_make_session("a"), _make_session("b")])
        self.assertEqual(ccw_tui._total_items(ctx), ccw_tui.ACTION_COUNT + 2)

    def test_total_items_no_sessions(self) -> None:
        ctx = _make_ctx()
        self.assertEqual(ccw_tui._total_items(ctx), ccw_tui.ACTION_COUNT)


class ListExistingProjectsTests(unittest.TestCase):
    def test_lists_directories_sorted(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "zebra").mkdir()
            (Path(tmpdir) / "alpha").mkdir()
            (Path(tmpdir) / ".hidden").mkdir()
            (Path(tmpdir) / "somefile.txt").touch()
            result = ccw._list_existing_projects(tmpdir)
        self.assertEqual(result, ["alpha", "zebra"])

    def test_nonexistent_root_returns_empty(self) -> None:
        result = ccw._list_existing_projects("/nonexistent/path/xyz")
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()

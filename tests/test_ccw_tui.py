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


def _noop_launch(*args, **kwargs) -> ccw_tui.LaunchRequest:
    return ccw_tui.LaunchRequest(cmd=("true",), label="noop")


def _make_session(name: str = "demo", attached: bool = False, user: str = "devagent") -> ccw_tui.TuiSession:
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
        user=user,
    )


def _make_ctx(
    sessions=None,
    existing_projects=None,
    has_sudo: bool = False,
    other_sessions=None,
    current_user: str = "devagent",
) -> ccw_tui.TuiContext:
    if sessions is None:
        sessions = []
    ctx = ccw_tui.TuiContext(
        sessions=sessions,
        total_cpu="5.0",
        total_ram="512M",
        version="ccw 0.7.0",
        cwd="/srv/repos/myproject",
        cwd_short="myproject",
        new_project_root="/srv/agentdev",
        existing_projects=existing_projects or [],
        current_user=current_user,
        has_sudo=has_sudo,
        other_sessions=other_sessions or [],
        on_attach=_noop_launch,
        on_kill=_noop,
        on_kill_all=_noop,
        on_kill_all_global=_noop,
        on_refresh=lambda: ctx,
        on_launch_cwd=_noop_launch,
        on_launch_new=_noop_launch,
        on_launch_existing=_noop_launch,
        get_settings_entries=lambda: [],
        on_setting_save=_noop,
        on_setting_remove=_noop,
        on_setting_save_mapping=_noop,
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


class SuperuserSegmentTests(unittest.TestCase):
    def test_no_superuser_block_without_sudo(self) -> None:
        ctx = _make_ctx(
            sessions=[_make_session("mine")],
            other_sessions=[_make_session("theirs", user="alice")],
            has_sudo=False,
        )
        _, _, settings_idx, kill_idx, has_super = ccw_tui._segments(ctx)
        self.assertFalse(has_super)
        self.assertEqual(settings_idx, -1)
        self.assertEqual(kill_idx, -1)
        self.assertEqual(ccw_tui._total_items(ctx), ccw_tui.ACTION_COUNT + 1)

    def test_sudo_with_no_sessions_shows_only_settings(self) -> None:
        ctx = _make_ctx(has_sudo=True)
        own_start, other_start, settings_idx, kill_idx, has_super = ccw_tui._segments(ctx)
        self.assertTrue(has_super)
        self.assertEqual(own_start, ccw_tui.ACTION_COUNT)
        self.assertEqual(other_start, ccw_tui.ACTION_COUNT)
        self.assertEqual(settings_idx, ccw_tui.ACTION_COUNT)
        self.assertEqual(kill_idx, -1)  # no sessions → no kill-all-global
        self.assertEqual(ccw_tui._total_items(ctx), ccw_tui.ACTION_COUNT + 1)

    def test_sudo_with_only_own_sessions_has_settings_and_kill(self) -> None:
        ctx = _make_ctx(sessions=[_make_session("mine")], has_sudo=True)
        _, _, settings_idx, kill_idx, has_super = ccw_tui._segments(ctx)
        self.assertTrue(has_super)
        # actions(3) + 1 own → settings at 4, kill at 5
        self.assertEqual(settings_idx, ccw_tui.ACTION_COUNT + 1)
        self.assertEqual(kill_idx, ccw_tui.ACTION_COUNT + 2)
        self.assertEqual(ccw_tui._total_items(ctx), ccw_tui.ACTION_COUNT + 1 + 2)

    def test_superuser_block_adds_other_sessions_plus_settings_plus_kill(self) -> None:
        own = [_make_session("mine")]
        other = [_make_session("x", user="alice"), _make_session("y", user="bob")]
        ctx = _make_ctx(sessions=own, other_sessions=other, has_sudo=True)
        own_start, other_start, settings_idx, kill_idx, has_super = ccw_tui._segments(ctx)
        self.assertTrue(has_super)
        self.assertEqual(own_start, ccw_tui.ACTION_COUNT)
        self.assertEqual(other_start, ccw_tui.ACTION_COUNT + 1)
        self.assertEqual(settings_idx, ccw_tui.ACTION_COUNT + 1 + 2)
        self.assertEqual(kill_idx, settings_idx + 1)
        # actions + own + other + settings + kill
        self.assertEqual(ccw_tui._total_items(ctx), ccw_tui.ACTION_COUNT + 1 + 2 + 1 + 1)

    def test_compute_col_widths_includes_user_when_requested(self) -> None:
        other = [_make_session("x", user="alice"), _make_session("y", user="bobbie")]
        widths = ccw_tui._compute_col_widths(other, include_user=True)
        self.assertIn("user", widths)
        self.assertEqual(widths["user"], len("bobbie"))

    def test_compute_col_widths_omits_user_by_default(self) -> None:
        widths = ccw_tui._compute_col_widths([_make_session("x")])
        self.assertNotIn("user", widths)


class DetectPasswordlessSudoTests(unittest.TestCase):
    def test_detect_when_euid_is_root(self) -> None:
        with mock.patch.object(ccw.os, "geteuid", return_value=0):
            self.assertTrue(ccw.detect_passwordless_sudo())

    def test_detect_via_sudo_n_true_success(self) -> None:
        fake = mock.Mock(returncode=0)
        captured: dict = {}

        def fake_run(cmd, *args, **kwargs):
            captured["cmd"] = cmd
            return fake

        with mock.patch.object(ccw.os, "geteuid", return_value=1000):
            with mock.patch.object(ccw.subprocess, "run", side_effect=fake_run):
                self.assertTrue(ccw.detect_passwordless_sudo())
        self.assertEqual(captured["cmd"], ["sudo", "-n", "true"])

    def test_detect_returns_false_on_nonzero_exit(self) -> None:
        fake = mock.Mock(returncode=1)
        with mock.patch.object(ccw.os, "geteuid", return_value=1000):
            with mock.patch.object(ccw.subprocess, "run", return_value=fake):
                self.assertFalse(ccw.detect_passwordless_sudo())

    def test_detect_returns_false_on_timeout(self) -> None:
        def raise_timeout(*_args, **_kwargs):
            raise ccw.subprocess.TimeoutExpired(cmd="sudo", timeout=0.5)

        with mock.patch.object(ccw.os, "geteuid", return_value=1000):
            with mock.patch.object(ccw.subprocess, "run", side_effect=raise_timeout):
                self.assertFalse(ccw.detect_passwordless_sudo())

    def test_detect_returns_false_on_oserror(self) -> None:
        with mock.patch.object(ccw.os, "geteuid", return_value=1000):
            with mock.patch.object(ccw.subprocess, "run", side_effect=FileNotFoundError):
                self.assertFalse(ccw.detect_passwordless_sudo())


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


class _FakeTerm:
    """Minimal blessed.Terminal stand-in for unit tests that doesn't touch a real tty."""

    height = 30
    width = 100
    normal = ""
    clear = ""
    home = ""
    clear_eol = ""
    dim = ""

    def __getattr__(self, name: str):
        # Any color/style like t.green, t.bold_red, etc. → identity function
        return lambda s="": s

    def location(self, *args, **kwargs):
        class _CM:
            def __enter__(self_inner):
                return self_inner
            def __exit__(self_inner, *exc):
                return False
        return _CM()


class ActivateItemReturnsLaunchRequestTests(unittest.TestCase):
    """_activate_item should return (status, LaunchRequest) for launch/attach items."""

    def test_activate_own_session_returns_attach_request(self) -> None:
        req = ccw_tui.LaunchRequest(cmd=("tmux", "attach-session", "-t", "cc-demo"), label="attach")
        ctx = _make_ctx(sessions=[_make_session("demo")])
        captured: dict = {}

        def on_attach(user: str, name: str) -> ccw_tui.LaunchRequest:
            captured["user"] = user
            captured["name"] = name
            return req

        ctx.on_attach = on_attach  # type: ignore[assignment]

        t = _FakeTerm()
        msg, got = ccw_tui._activate_item(t, ctx, ccw_tui.ACTION_COUNT)
        self.assertIsNone(msg)
        self.assertIs(got, req)
        self.assertEqual(captured["name"], "cc-demo")

    def test_activate_settings_returns_no_launch_request(self) -> None:
        ctx = _make_ctx(has_sudo=True)
        t = _FakeTerm()
        with mock.patch("ccw_tui_settings.show_settings"):
            msg, req = ccw_tui._activate_item(t, ctx, ccw_tui.ACTION_COUNT)
        self.assertIsNone(req)
        self.assertIsNotNone(msg)


class LaunchRequestTests(unittest.TestCase):
    def test_launch_request_is_hashable_and_immutable(self) -> None:
        req = ccw_tui.LaunchRequest(cmd=("tmux", "attach"), label="x")
        # frozen dataclass: attribute writes should fail
        with self.assertRaises(Exception):
            req.cmd = ("other",)  # type: ignore[misc]
        self.assertEqual(req.cmd[0], "tmux")
        self.assertEqual(req.prelaunch, ())

    def test_format_launch_status_rc0_is_empty(self) -> None:
        t = mock.Mock()
        t.yellow = lambda s: s
        t.red = lambda s: s
        t.green = lambda s: s
        req = ccw_tui.LaunchRequest(cmd=("true",), label="attach demo")
        self.assertEqual(ccw_tui._format_launch_status(t, req, 0, "cmd"), "")

    def test_format_launch_status_rc130_is_cancelled(self) -> None:
        t = mock.Mock()
        t.yellow = lambda s: s
        t.red = lambda s: s
        t.dim = ""
        t.normal = ""
        msg = ccw_tui._format_launch_status(t, ccw_tui.LaunchRequest(cmd=("x",), label="attach demo"), 130, "cmd")
        self.assertIn("cancelled", msg)

    def test_format_launch_status_nonzero_shows_rc(self) -> None:
        t = mock.Mock()
        t.yellow = lambda s: s
        req = ccw_tui.LaunchRequest(cmd=("x",), label="launch demo")
        msg = ccw_tui._format_launch_status(t, req, 3, "cmd")
        self.assertIn("rc=3", msg)

    def test_format_launch_status_prelaunch_failure(self) -> None:
        t = mock.Mock()
        t.red = lambda s: s
        req = ccw_tui.LaunchRequest(cmd=("x",), label="launch demo")
        msg = ccw_tui._format_launch_status(t, req, 2, "prelaunch")
        self.assertIn("prelaunch", msg)
        self.assertIn("rc=2", msg)

    def test_run_launch_request_runs_prelaunch_then_cmd(self) -> None:
        req = ccw_tui.LaunchRequest(
            cmd=("tmux", "attach"),
            prelaunch=(("mkdir", "-p", "/tmp/x"),),
            label="attach demo",
        )
        with mock.patch.object(ccw_tui.subprocess, "call", side_effect=[0, 0]) as call:
            rc, stage = ccw_tui._run_launch_request(req)
        self.assertEqual(rc, 0)
        self.assertEqual(stage, "cmd")
        self.assertEqual(call.call_count, 2)
        self.assertEqual(call.call_args_list[0][0][0], ["mkdir", "-p", "/tmp/x"])
        self.assertEqual(call.call_args_list[1][0][0], ["tmux", "attach"])

    def test_run_launch_request_aborts_on_prelaunch_failure(self) -> None:
        req = ccw_tui.LaunchRequest(
            cmd=("tmux", "attach"),
            prelaunch=(("mkdir", "-p", "/tmp/x"),),
        )
        with mock.patch.object(ccw_tui.subprocess, "call", side_effect=[5]) as call:
            rc, stage = ccw_tui._run_launch_request(req)
        self.assertEqual((rc, stage), (5, "prelaunch"))
        call.assert_called_once()  # main cmd never ran

    def test_tui_context_defaults_return_noop_launch_request(self) -> None:
        ctx = ccw_tui.TuiContext(
            sessions=[],
            total_cpu="0",
            total_ram="0",
            version="test",
            cwd="/",
            cwd_short="/",
            new_project_root="/tmp",
            existing_projects=[],
        )
        req = ctx.on_attach("u", "s")
        self.assertIsInstance(req, ccw_tui.LaunchRequest)
        self.assertEqual(req.cmd, ("true",))


if __name__ == "__main__":
    unittest.main()

import ast
import importlib.util
import inspect
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
    cwd_allowed: bool = True,
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
        cwd_allowed=cwd_allowed,
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

    def test_activate_launch_cwd_disabled_when_cwd_not_allowed(self) -> None:
        """Picking 'New session in current folder' when cwd is not under
        allowed_roots must show a status-line hint and must NOT invoke
        on_launch_cwd (which would otherwise call fail() and silently
        exit the whole TUI)."""
        ctx = _make_ctx(cwd_allowed=False)
        called: dict = {}

        def on_launch_cwd(dsp: bool):
            called["dsp"] = dsp
            return ccw_tui.LaunchRequest(cmd=("true",), label="should-not-fire")

        ctx.on_launch_cwd = on_launch_cwd  # type: ignore[assignment]

        t = _FakeTerm()
        msg, req = ccw_tui._activate_item(t, ctx, 0)
        self.assertIsNone(req)
        self.assertIsNotNone(msg)
        self.assertIn("allowed_roots", msg)
        self.assertNotIn("dsp", called)


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
            rc, stage, wall = ccw_tui._run_launch_request(req)
        self.assertEqual(rc, 0)
        self.assertEqual(stage, "cmd")
        self.assertGreaterEqual(wall, 0.0)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(call.call_args_list[0][0][0], ["mkdir", "-p", "/tmp/x"])
        self.assertEqual(call.call_args_list[1][0][0], ["tmux", "attach"])

    def test_run_launch_request_aborts_on_prelaunch_failure(self) -> None:
        req = ccw_tui.LaunchRequest(
            cmd=("tmux", "attach"),
            prelaunch=(("mkdir", "-p", "/tmp/x"),),
        )
        with mock.patch.object(ccw_tui.subprocess, "call", side_effect=[5]) as call:
            rc, stage, wall = ccw_tui._run_launch_request(req)
        self.assertEqual((rc, stage), (5, "prelaunch"))
        self.assertGreaterEqual(wall, 0.0)
        call.assert_called_once()  # main cmd never ran

    def test_callback_error_is_exception(self) -> None:
        self.assertTrue(issubclass(ccw_tui.CallbackError, Exception))
        err = ccw_tui.CallbackError("boom")
        self.assertEqual(str(err), "boom")

    def test_activate_propagates_callback_error_from_on_attach(self) -> None:
        """A CallbackError raised by on_attach must bubble out of _activate_item
        so the outer loop (which wraps this call) can render it on the status
        line instead of crashing the TUI."""
        ctx = _make_ctx(sessions=[_make_session("demo")])

        def on_attach(user: str, name: str):
            raise ccw_tui.CallbackError("tmux kill-session: no such session")

        ctx.on_attach = on_attach  # type: ignore[assignment]
        t = _FakeTerm()
        with self.assertRaises(ccw_tui.CallbackError) as cm:
            ccw_tui._activate_item(t, ctx, ccw_tui.ACTION_COUNT)
        self.assertIn("no such session", str(cm.exception))

    def test_activate_global_kill_renders_callback_error_message(self) -> None:
        """Kill-all-global raising CallbackError becomes a status-line message
        including the underlying error text (not a generic 'failed')."""
        ctx = _make_ctx(sessions=[_make_session("a")], has_sudo=True)

        def on_kill_all_global() -> None:
            raise ccw_tui.CallbackError("permission denied on socket")

        ctx.on_kill_all_global = on_kill_all_global  # type: ignore[assignment]

        # Compute the kill-all-global item index.
        _, _, _, kill_idx, has_super = ccw_tui._segments(ctx)
        self.assertTrue(has_super)
        self.assertGreater(kill_idx, 0)

        t = _FakeTerm()
        with mock.patch.object(ccw_tui, "_confirm_kill_all_global", return_value=True):
            msg, req = ccw_tui._activate_item(t, ctx, kill_idx)
        self.assertIsNone(req)
        self.assertIsNotNone(msg)
        self.assertIn("permission denied", msg)

    def test_pause_on_launch_failure_skips_on_success_and_cancel(self) -> None:
        """No pause and no output when rc is 0 (success) or 130 (Ctrl-C)."""
        t = _FakeTerm()
        req = ccw_tui.LaunchRequest(cmd=("true",), label="attach demo")
        with mock.patch.object(ccw_tui.sys.stdout, "write") as w:
            ccw_tui._pause_on_launch_failure(t, req, 0, "cmd")
            ccw_tui._pause_on_launch_failure(t, req, 130, "cmd")
        w.assert_not_called()

    def test_pause_on_fast_zero_exit_shows_banner(self) -> None:
        """rc=0 but sub-second wall time → pause with 'exited immediately'
        banner so the user can read anything the launch printed before the
        next fullscreen re-entry wipes it."""
        class _NullCM:
            def __enter__(self): return self
            def __exit__(self, *exc): return False

        t = _FakeTerm()
        t.cbreak = lambda: _NullCM()  # type: ignore[attr-defined]
        t.inkey = lambda timeout=None: "x"  # type: ignore[attr-defined]
        req = ccw_tui.LaunchRequest(cmd=("claude",), label="launch cc-demo")
        with mock.patch.object(ccw_tui.sys.stdout, "write") as w, \
             mock.patch.object(ccw_tui.sys.stdout, "flush"):
            ccw_tui._pause_on_launch_failure(t, req, 0, "cmd", wall_seconds=0.1)
        written = "".join(c.args[0] for c in w.call_args_list)
        self.assertIn("exited immediately", written)
        self.assertIn("press any key", written)

    def test_pause_skips_on_slow_zero_exit(self) -> None:
        """rc=0 with healthy wall time (≥ FAST_EXIT_THRESHOLD_SEC) stays silent."""
        t = _FakeTerm()
        req = ccw_tui.LaunchRequest(cmd=("tmux",), label="attach demo")
        with mock.patch.object(ccw_tui.sys.stdout, "write") as w:
            ccw_tui._pause_on_launch_failure(t, req, 0, "cmd", wall_seconds=5.0)
        w.assert_not_called()

    def test_pause_on_launch_failure_waits_for_key_on_nonzero_rc(self) -> None:
        class _NullCM:
            def __enter__(self): return self
            def __exit__(self, *exc): return False

        t = _FakeTerm()
        # _FakeTerm must provide cbreak() and inkey() for this path. Stub.
        t.cbreak = lambda: _NullCM()  # type: ignore[attr-defined]
        t.inkey = lambda timeout=None: "x"  # type: ignore[attr-defined]
        req = ccw_tui.LaunchRequest(cmd=("tmux", "new-session"), label="launch cc-demo")
        with mock.patch.object(ccw_tui.sys.stdout, "write") as w, \
             mock.patch.object(ccw_tui.sys.stdout, "flush"):
            ccw_tui._pause_on_launch_failure(t, req, 5, "cmd")
        # Should have written the banner (includes the rc) before blocking.
        written = "".join(c.args[0] for c in w.call_args_list)
        self.assertIn("rc=5", written)
        self.assertIn("press any key", written)
        self.assertIn("launch cc-demo", written)

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


class SettingsScreenKeymapTests(unittest.TestCase):
    """Regression tests for the Settings screen keymap.

    Background: the old Settings screen had a hidden `g` shortcut that
    opened the read-only git-remotes sub-screen. Combined with the empty
    superuser-state digit-jump landing users on Settings, `u-den`
    regularly ended up staring at "Git remote profiles (no profiles
    configured)" after an unrelated flow. The shortcut is gone; these
    tests lock that in.
    """

    def test_settings_g_key_is_noop_does_not_open_git_remotes(self) -> None:
        import ccw_tui_settings

        t = _FakeTerm()
        # inkey yields "g" first, then "q" to exit.
        keys = iter(["g", "q"])

        class _K(str):
            @property
            def name(self) -> str:
                return ""

            @property
            def is_sequence(self) -> bool:
                return False

        def fake_inkey(timeout=None):
            return _K(next(keys))

        t.inkey = fake_inkey  # type: ignore[attr-defined]

        cbs = ccw_tui_settings.SettingsCallbacks(
            get_entries=lambda: [],  # empty list returns early
            save_setting=_noop,
            remove_setting=_noop,
            save_mapping=_noop,
            get_git_remote_profile_rows=lambda: [],
        )
        with mock.patch.object(ccw_tui_settings, "_show_git_remotes") as show:
            ccw_tui_settings.show_settings(t, cbs)
        show.assert_not_called()


class DigitJumpGuardTests(unittest.TestCase):
    """Digit 1–9 must move the cursor but never *auto-activate* Settings
    or Kill-ALL. This prevents stray-keystroke disasters when the
    superuser block is empty and `settings_idx == ACTION_COUNT == 3`.
    """

    def test_settings_idx_collapses_to_action_count_for_empty_superuser(self) -> None:
        ctx = _make_ctx(has_sudo=True)  # zero own + zero other sessions
        _own_start, _other_start, settings_idx, kill_idx, has_super = ccw_tui._segments(ctx)
        self.assertTrue(has_super)
        self.assertEqual(settings_idx, ccw_tui.ACTION_COUNT)
        # No sessions → no global Kill-ALL row.
        self.assertEqual(kill_idx, -1)

    def test_activate_settings_via_enter_still_opens_it(self) -> None:
        """Deliberate activation (Enter on the Settings row) is unchanged."""
        ctx = _make_ctx(has_sudo=True)
        t = _FakeTerm()
        with mock.patch("ccw_tui_settings.show_settings") as show:
            msg, req = ccw_tui._activate_item(t, ctx, ccw_tui.ACTION_COUNT)
        show.assert_called_once()
        self.assertIsNone(req)


class DrainStdinTests(unittest.TestCase):
    """`_drain_stdin` must consume buffered keystrokes between a launch
    round-trip and the next screen's inkey(). This kills Candidate 2 of
    the 2026-04-18 bug review: keys typed while the TUI is suspended
    cannot re-animate a stale cursor after fullscreen re-entry.
    """

    def _make_draining_term(self, buffered_keys: list[str]) -> "_FakeTerm":
        t = _FakeTerm()
        queue = list(buffered_keys)

        class _NullCM:
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *exc): return False

        def fake_inkey(timeout=None):
            # timeout=0: non-blocking poll. Return "" when empty.
            if queue:
                return queue.pop(0)
            return ""

        t.cbreak = lambda: _NullCM()  # type: ignore[attr-defined]
        t.inkey = fake_inkey  # type: ignore[attr-defined]
        t._queue = queue  # type: ignore[attr-defined]
        return t

    def test_drain_empties_the_queue(self) -> None:
        t = self._make_draining_term(["a", "b", "c"])
        drained = ccw_tui._drain_stdin(t)
        self.assertEqual(drained, 3)
        self.assertEqual(t._queue, [])  # type: ignore[attr-defined]

    def test_drain_empty_queue_is_zero(self) -> None:
        t = self._make_draining_term([])
        self.assertEqual(ccw_tui._drain_stdin(t), 0)

    def test_drain_is_bounded(self) -> None:
        """Bounded so a pathological fake/real tty cannot wedge the TUI."""
        t = self._make_draining_term(["x"] * 500)
        drained = ccw_tui._drain_stdin(t, max_keys=10)
        self.assertEqual(drained, 10)

    def test_drain_swallows_exceptions(self) -> None:
        """If the tty is broken, drain returns quietly instead of bubbling."""
        t = _FakeTerm()

        def broken_cbreak():
            raise OSError("tty gone")

        t.cbreak = broken_cbreak  # type: ignore[attr-defined]
        # Must not raise.
        self.assertEqual(ccw_tui._drain_stdin(t), 0)


class BuildItemsTests(unittest.TestCase):
    """PR 9 invariant: item identity (kind + label) is stable under
    session-count changes. The cursor used to point at "Open existing
    project" via integer index 2; now it points at an Item whose
    kind == "action-open" regardless of how many sessions are between
    it and Settings. This kills the aliasing class of bug where a
    new session appearing would shift what integer cursor=N means.
    """

    def test_actions_always_first_three_items(self) -> None:
        ctx = _make_ctx()
        items = ccw_tui.build_items(ctx)
        self.assertGreaterEqual(len(items), 3)
        self.assertEqual(items[0].kind, "action-cwd")
        self.assertEqual(items[1].kind, "action-new")
        self.assertEqual(items[2].kind, "action-open")

    def test_action_items_have_digit_hints_1_through_3(self) -> None:
        items = ccw_tui.build_items(_make_ctx())
        self.assertEqual(items[0].digit_hint, 1)
        self.assertEqual(items[1].digit_hint, 2)
        self.assertEqual(items[2].digit_hint, 3)

    def test_settings_item_has_no_digit_hint(self) -> None:
        """PR 2 invariant, now expressed at the Item level: Settings is
        never digit-reachable regardless of ctx shape."""
        shapes = [
            _make_ctx(has_sudo=True),
            _make_ctx(has_sudo=True, sessions=[_make_session("a")]),
            _make_ctx(has_sudo=True, other_sessions=[_make_session("x", user="alice")]),
        ]
        for ctx in shapes:
            items = ccw_tui.build_items(ctx)
            settings_items = [it for it in items if it.kind == "settings"]
            self.assertEqual(len(settings_items), 1)
            self.assertIsNone(settings_items[0].digit_hint)

    def test_kill_all_item_has_no_digit_hint(self) -> None:
        ctx = _make_ctx(has_sudo=True, sessions=[_make_session("a")])
        items = ccw_tui.build_items(ctx)
        killers = [it for it in items if it.kind == "kill-all-global"]
        self.assertEqual(len(killers), 1)
        self.assertIsNone(killers[0].digit_hint)

    def test_action_open_identity_stable_under_session_change(self) -> None:
        """Add a session; the 'action-open' Item must still exist with the
        same kind + label. This is the target invariant of PR 9."""
        ctx_empty = _make_ctx()
        ctx_with_session = _make_ctx(sessions=[_make_session("demo")])
        items_empty = ccw_tui.build_items(ctx_empty)
        items_with = ccw_tui.build_items(ctx_with_session)

        open_empty = next(it for it in items_empty if it.kind == "action-open")
        open_with = next(it for it in items_with if it.kind == "action-open")
        self.assertEqual(open_empty.kind, open_with.kind)
        self.assertEqual(open_empty.label, open_with.label)

    def test_build_items_length_matches_total_items(self) -> None:
        """Bridge-test: the new typed list aligns with the old _total_items()
        so legacy integer-cursor code continues to work during the
        migration."""
        for shape, ctx in [
            ("empty", _make_ctx()),
            ("own sessions", _make_ctx(sessions=[_make_session("a"), _make_session("b")])),
            ("sudo empty", _make_ctx(has_sudo=True)),
            ("sudo + sessions", _make_ctx(
                has_sudo=True,
                sessions=[_make_session("a")],
                other_sessions=[_make_session("b", user="alice")],
            )),
        ]:
            with self.subTest(shape=shape):
                self.assertEqual(len(ccw_tui.build_items(ctx)), ccw_tui._total_items(ctx))

    def test_action_count_derived_from_action_kinds(self) -> None:
        """`ACTION_COUNT` must equal len(_ACTION_KINDS); if a kind is added,
        the constant follows automatically."""
        self.assertEqual(ccw_tui.ACTION_COUNT, len(ccw_tui._ACTION_KINDS))


class LogEventTests(unittest.TestCase):
    """PR 7 invariant: `_log_event` writes JSONL to
    `/srv/work/logs/ccw/tui-{user}-YYYYMMDD.log`, silently on failure,
    and never crashes the caller.
    """

    def _tmp_log_dir(self):
        import tempfile
        return tempfile.mkdtemp(prefix="ccw_log_test_")

    def test_log_event_writes_jsonl_line(self) -> None:
        import json
        import os as _os
        import shutil
        from pathlib import Path

        d = self._tmp_log_dir()
        try:
            with mock.patch.dict(_os.environ, {"CCW_LOG_DIR": d}):
                ccw_tui._log_event(
                    "tui_start",
                    caller_user="u-den",
                    launch_user="u-vz",
                    screen=ccw_tui.Screen.MAIN,
                    extra={"version": "ccw 0.11.0"},
                )
            files = list(Path(d).iterdir())
            self.assertEqual(len(files), 1)
            content = files[0].read_text().strip()
            self.assertTrue(content)
            record = json.loads(content)
            self.assertEqual(record["event"], "tui_start")
            self.assertEqual(record["caller_user"], "u-den")
            self.assertEqual(record["launch_user"], "u-vz")
            self.assertEqual(record["screen"], "main")
            self.assertIn("ts", record)
            self.assertEqual(record["extra"]["version"], "ccw 0.11.0")
            # Filename should encode the launch_user and a date.
            self.assertIn("tui-u-vz-", files[0].name)
            self.assertTrue(files[0].name.endswith(".log"))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_log_event_appends_multiple_records(self) -> None:
        import os as _os
        import shutil
        from pathlib import Path

        d = self._tmp_log_dir()
        try:
            with mock.patch.dict(_os.environ, {"CCW_LOG_DIR": d}):
                ccw_tui._log_event("tui_start", launch_user="u-vz")
                ccw_tui._log_event("launch", launch_user="u-vz", outcome="rc=0")
                ccw_tui._log_event("tui_quit", launch_user="u-vz", outcome="rc=0")
            files = list(Path(d).iterdir())
            self.assertEqual(len(files), 1)
            lines = files[0].read_text().strip().splitlines()
            self.assertEqual(len(lines), 3)

        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_log_event_swallows_errors(self) -> None:
        """Writing to an unwritable location must not raise."""
        import os as _os
        with mock.patch.dict(_os.environ, {"CCW_LOG_DIR": "/proc/nonexistent/cannot_create"}):
            # Must not raise — logging is best-effort.
            ccw_tui._log_event("tui_start", launch_user="u-vz")

    def test_log_event_creates_directory(self) -> None:
        import os as _os
        import shutil
        import tempfile

        base = tempfile.mkdtemp(prefix="ccw_log_parent_")
        nested = _os.path.join(base, "a", "b", "c")
        try:
            with mock.patch.dict(_os.environ, {"CCW_LOG_DIR": nested}):
                ccw_tui._log_event("tui_start", launch_user="u-vz")
            self.assertTrue(_os.path.isdir(nested))
        finally:
            shutil.rmtree(base, ignore_errors=True)


class KeymapRegistryTests(unittest.TestCase):
    """PR 6 invariant: every key binding handled at runtime by any TUI
    screen is declared in :data:`ccw_tui.SCREEN_KEYMAP`. This is the
    active gate the review asked for — silent-overload bugs like the
    old ``g → open git remotes`` shortcut become detectable as an
    undeclared binding instead of hiding in first-match-if chains.
    """

    def test_main_screen_has_quit_activate_and_kill_keys(self) -> None:
        main = ccw_tui.SCREEN_KEYMAP[ccw_tui.Screen.MAIN]
        self.assertIn("quit", main)
        self.assertIn("q", main["quit"])
        self.assertIn("KEY_ESCAPE", main["quit"])
        self.assertIn("activate", main)
        self.assertIn("kill", main)
        self.assertIn("d", main["kill"])

    def test_settings_screen_does_not_bind_g_to_anything(self) -> None:
        """PR 1 invariant, now formalised in the registry: the Settings
        screen has no binding for the literal key ``g`` — it is not in
        the allow-list for cursor_home (only KEY_HOME) nor for any
        other action."""
        settings = ccw_tui.SCREEN_KEYMAP[ccw_tui.Screen.SETTINGS]
        for action, keys in settings.items():
            self.assertNotIn(
                "g", keys,
                f"Settings screen must not bind 'g' to any action, found in '{action}'",
            )

    def test_every_screen_has_activate_or_submit_or_cancel(self) -> None:
        """Every screen must offer at least one exit/activate action."""
        for screen, bindings in ccw_tui.SCREEN_KEYMAP.items():
            exits = set(bindings).intersection({"activate", "submit", "cancel", "quit", "back"})
            self.assertTrue(exits, f"Screen {screen} has no activate/submit/cancel binding")

    def test_no_duplicate_key_within_a_screen(self) -> None:
        """Within a screen, the same key may not appear under two actions —
        otherwise the runtime first-match dispatch is ambiguous."""
        for screen, bindings in ccw_tui.SCREEN_KEYMAP.items():
            seen: dict[str, str] = {}
            for action, keys in bindings.items():
                for k in keys:
                    if k in seen:
                        self.fail(f"Screen {screen}: key {k!r} bound to both {seen[k]!r} and {action!r}")
                    seen[k] = action

    def test_registry_key_matches_runtime_literal_keys(self) -> None:
        """Scan the source of the main interactive loop for ``key == "X"``
        literal comparisons and assert each referenced literal is in the
        MAIN screen's keymap. This catches unauthorised new bindings."""
        src = inspect.getsource(ccw_tui._interactive_loop)
        import re
        # Match: key == "X" or == "KEY_X" (single-char or name-style).
        # Source text has escape sequences as 2-char strings (\n → "\\n").
        literal_eqs = re.findall(r'key == "([^"]+)"', src)
        name_eqs = re.findall(r'key\.name == "([^"]+)"', src)
        main = ccw_tui.SCREEN_KEYMAP[ccw_tui.Screen.MAIN]
        declared = set()
        for keys in main.values():
            declared.update(keys)

        def _decode(lit: str) -> str:
            # In the source text, "\n" is the two chars '\' and 'n'; decode
            # common escapes to their actual keypress value.
            return lit.encode("utf-8").decode("unicode_escape")

        for lit in literal_eqs:
            decoded = _decode(lit)
            self.assertIn(
                decoded, declared,
                f"Runtime literal key {decoded!r} in _interactive_loop is not declared in MAIN keymap",
            )
        for name in name_eqs:
            self.assertIn(
                name, declared,
                f"Runtime KEY_* name {name!r} in _interactive_loop is not declared in MAIN keymap",
            )


class PlanTuiOpenExistingTests(unittest.TestCase):
    """PR 5 invariant: the TUI "Open existing project" planner is
    structurally incapable of calling git-creation code. Verified by AST
    inspection of the function source — _do_create_git_remote must not
    appear as a name anywhere in the function body.
    """

    def test_plan_tui_open_existing_exists(self) -> None:
        self.assertTrue(hasattr(ccw, "_plan_tui_open_existing"))

    def test_plan_tui_open_existing_has_no_git_profile_param(self) -> None:
        sig = inspect.signature(ccw._plan_tui_open_existing)
        self.assertNotIn("git_profile", sig.parameters)
        # Positive: it *does* take the four documented params.
        self.assertEqual(
            list(sig.parameters),
            ["cfg", "launch_user", "name", "dsp"],
        )

    def test_plan_tui_open_existing_does_not_reference_git_create(self) -> None:
        src = inspect.getsource(ccw._plan_tui_open_existing)
        tree = ast.parse(src)
        names = {
            n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name)
        }
        attrs = {
            n.attr for n in ast.walk(tree)
            if isinstance(n, ast.Attribute)
        }
        self.assertNotIn("_do_create_git_remote", names, "Open-existing must not reference _do_create_git_remote")
        self.assertNotIn("_do_create_git_remote", attrs)

    def test_plan_tui_create_new_still_can_create_git(self) -> None:
        """Positive control: the create-new planner keeps its git branch."""
        src = inspect.getsource(ccw._plan_tui_create_new)
        self.assertIn("_do_create_git_remote", src)


class DigitHintedIndicesTests(unittest.TestCase):
    """`_digit_hinted_indices(ctx)` is the single source of truth for which
    item indices a digit keypress may activate. It must never include
    Settings or Kill-ALL in any context shape — that is the invariant
    locking in PR 2 of the 2026-04-18 structural refactor.
    """

    def _all_shapes(self):
        return [
            ("no-sudo empty", _make_ctx()),
            ("no-sudo with sessions", _make_ctx(sessions=[_make_session("a"), _make_session("b")])),
            ("sudo empty", _make_ctx(has_sudo=True)),
            ("sudo only own", _make_ctx(sessions=[_make_session("a")], has_sudo=True)),
            (
                "sudo own+other",
                _make_ctx(
                    sessions=[_make_session("a")],
                    other_sessions=[_make_session("b", user="alice")],
                    has_sudo=True,
                ),
            ),
        ]

    def test_digit_allowed_excludes_settings_and_kill(self) -> None:
        for label, ctx in self._all_shapes():
            allowed = ccw_tui._digit_hinted_indices(ctx)
            _, _, settings_idx, kill_idx, has_super = ccw_tui._segments(ctx)
            if has_super:
                self.assertNotIn(settings_idx, allowed, f"{label}: settings leaked into digit allow-list")
                if kill_idx >= 0:
                    self.assertNotIn(kill_idx, allowed, f"{label}: kill-all leaked into digit allow-list")

    def test_digit_allowed_is_subset_of_valid_items(self) -> None:
        for label, ctx in self._all_shapes():
            allowed = ccw_tui._digit_hinted_indices(ctx)
            total = ccw_tui._total_items(ctx)
            for idx in allowed:
                self.assertLess(idx, total, f"{label}: index {idx} is past total {total}")
                self.assertGreaterEqual(idx, 0, f"{label}: negative index {idx}")

    def test_digit_allowed_actions_always_included(self) -> None:
        """Every action row (0..ACTION_COUNT-1) is digit-reachable."""
        for label, ctx in self._all_shapes():
            allowed = ccw_tui._digit_hinted_indices(ctx)
            for i in range(ccw_tui.ACTION_COUNT):
                self.assertIn(i, allowed, f"{label}: action idx {i} not in digit allow-list")

    def test_digit_allowed_includes_session_rows(self) -> None:
        ctx = _make_ctx(sessions=[_make_session("a"), _make_session("b")])
        allowed = ccw_tui._digit_hinted_indices(ctx)
        # Own sessions start at ACTION_COUNT
        self.assertIn(ccw_tui.ACTION_COUNT, allowed)
        self.assertIn(ccw_tui.ACTION_COUNT + 1, allowed)


if __name__ == "__main__":
    unittest.main()

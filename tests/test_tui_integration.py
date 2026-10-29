"""Real terminal bytes exercise screen entry, folder navigation and fresh-app handoff."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "tests"))


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


# Shared empty-session operator context. A validated profile and installed
# host report keep the initial screen independent of local installations;
# the real runner still owns rendering, probing and key dispatch.
_CHILD_SCRIPT = r"""
import sys, os
from uxon import tui as uxon_tui
from uxon.domain.agents import DEFAULT_AGENT_CATALOG
from uxon.domain.host_report import BinaryStatus, HostReport
from uxon.domain.sudo import SudoCapability
from uxon.infra import probes
from uxon.tui.context import LaunchProfileOption

probes.probe_host = lambda cfg, user, catalog: HostReport(
    tmux=BinaryStatus('tmux', '/usr/bin/tmux', ''),
    agents={aid: BinaryStatus(spec.binary, f'/usr/bin/{spec.binary}', '')
            for aid, spec in catalog.items()},
    launch_user=user,
)

ctx = uxon_tui.TuiContext(
    sessions=[],
    total_cpu="0",
    total_ram="0",
    version="uxon-test",
    cwd="/tmp",
    cwd_short="tmp",
    new_project_root="/tmp/projects",
    existing_projects=[],
    cwd_writable=True,
    current_user="alice",
    agents=DEFAULT_AGENT_CATALOG,
    enabled_profiles=("claude",),
    default_profile="claude",
    launch_profiles={"claude": LaunchProfileOption("claude", "Claude", "claude", "alice")},
    sudo_caps=SudoCapability(
        reachable_users=frozenset({"bob"})
    ),
    other_sessions=[],
)
rc = uxon_tui.run(ctx)
sys.exit(rc)
"""


@unittest.skipUnless(_PTY_OK, "pty.fork unavailable on this platform")
class PtyTuiIntegrationTests(unittest.TestCase):
    """Real-byte input, controlling-terminal safety and explicit screen readiness."""

    def test_pty_harness_controls_terminal_and_bounds_failures(self) -> None:
        from harness.pty_tui import run_pty, run_python_snippet

        from uxon.infra.loop_guard import handoff_spawn

        with (
            mock.patch.dict(
                os.environ, {"UXON_DISABLE_LOOP_GUARD": "1", "UXON_SPAWN_GUARD_STRICT": "1"}
            ),
            handoff_spawn(),
        ):
            trace = run_python_snippet(
                "import os; fd = os.open('/dev/tty', os.O_RDWR); "
                "print(f'controlling-tty:{os.isatty(0)}:{os.isatty(1)}'); os.close(fd); "
                "print('guards:', os.getenv('UXON_DISABLE_LOOP_GUARD'), "
                "os.getenv('UXON_SPAWN_GUARD_STRICT'))",
                [],
                initial_drain=1.0,
                final_drain=0.1,
                timeout=3.0,
            )
        self.assertIn("controlling-tty:True:True", trace.plain)
        self.assertIn("guards: None None", trace.plain)
        self.assertEqual(trace.exit_code, 0, trace.plain)

        with tempfile.TemporaryDirectory() as directory:
            finalized = Path(directory) / "finalized"
            # Closing the terminal streams need not finish the interpreter.
            # Honor the scenario deadline while bounded finalization completes.
            trace = run_python_snippet(
                "import os, time; from pathlib import Path; "
                "print('FINALIZING', flush=True); "
                "os.close(0); os.close(1); os.close(2); time.sleep(0.3); "
                f"Path({str(finalized)!r}).write_text('complete')",
                [],
                initial_wait_for="FINALIZING",
                initial_drain=1.0,
                final_drain=0.1,
                timeout=3.0,
            )
            self.assertEqual(trace.exit_code, 0, trace.plain)
            self.assertEqual(finalized.read_text(), "complete")

            consumed = Path(directory) / "consumed"
            pid_file = Path(directory) / "pid"
            script = f"""
import os, signal, tty
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
tty.setraw(0)
Path({str(pid_file)!r}).write_text(str(os.getpid()))
print('READY', flush=True)
Path({str(consumed)!r}).write_bytes(os.read(0, 1))
print('CONSUMED', flush=True)
os.read(0, 1)
"""
            # READY from the initial stage cannot satisfy the next stage.
            # A later key must never be sent after that stage fails; the
            # SIGTERM-resistant child must still be killed and reaped.
            with self.assertRaisesRegex(AssertionError, "key 1: missing marker 'READY'") as error:
                run_python_snippet(
                    script,
                    [(0.5, b"x", "READY"), (0.5, b"y", "CONSUMED")],
                    initial_wait_for="READY",
                    initial_drain=1.0,
                    timeout=3.0,
                )
            self.assertIn("CONSUMED", str(error.exception))
            self.assertEqual(consumed.read_bytes(), b"x")
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 0)

            for fault in ("missing initial marker", "process start failure"):
                with self.subTest(fault=fault):
                    descriptors = set(os.listdir("/proc/self/fd"))
                    if fault == "missing initial marker":
                        with self.assertRaisesRegex(
                            AssertionError, "initial render: missing marker"
                        ):
                            run_python_snippet(
                                "print('EARLY_EXIT', flush=True); raise SystemExit(7)",
                                [(0.5, b"x", "UNREACHABLE")],
                                initial_wait_for="MISSING",
                                initial_drain=1.0,
                                timeout=3.0,
                            )
                    else:
                        with self.assertRaises(FileNotFoundError):
                            run_pty([str(Path(directory) / "missing-command")], [])
                    self.assertEqual(set(os.listdir("/proc/self/fd")), descriptors)

    @pytest.mark.slow
    def test_project_picker_decodes_plain_folder_arrows_with_search(self) -> None:
        from harness.pty_tui import run_python_snippet

        script = r"""
from pathlib import Path
from tempfile import TemporaryDirectory
from helpers import make_config
from textual.widgets import Input
from uxon.app.project_browsing import browse_project_directories, list_project_directories
from uxon.infra.identity import process_user
import uxon.tui.app as app_module
from uxon.tui.app import UxonApp
from uxon.tui.callback_wrap import _wrap_tui_callback
from uxon.tui.context import CallbackError, TuiContext
from uxon.tui.screens.existing import ExistingProjectScreen

with TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "folder-key-sentinel" / "inner-key-sentinel" / "leaf-key-sentinel").mkdir(parents=True)
    cfg = make_config(new_project_root=tmp, allowed_roots=[tmp])
    user = process_user()
    ctx = TuiContext(
        sessions=[], total_cpu="0", total_ram="0", version="test",
        cwd=tmp, cwd_short="projects", new_project_root=tmp,
        existing_projects=list_project_directories(cfg, user, tmp),
        current_user=user, launch_user=user, cwd_writable=True, refresh_sources=[],
    )
    class ProjectApp(UxonApp):
        CSS_PATH = Path(app_module.__file__).with_name("styles.tcss")
        selected = None
        def on_mount(self):
            super().on_mount()
            self.call_later(self.open_picker)
        async def open_picker(self):
            callback = _wrap_tui_callback(
                lambda path: browse_project_directories(cfg, user, path), CallbackError
            )
            await self.push_screen(
                ExistingProjectScreen(ctx.existing_projects, tmp, list_directories=callback),
                self.picked,
            )
            # Output settling must follow navigation, not caret animation.
            self.screen.query_one(Input).cursor_blink = False
        def picked(self, value):
            self.selected = value
            self.exit()
    app = ProjectApp(ctx, probe_agents=False)
    app.run()
    print(f"picked:{app.selected}")
"""
        trace = run_python_snippet(
            script,
            [
                (10.0, b"sentinel", "1 match"),
                (10.0, b"\x1b[C", "inner-key-sentinel"),
                (10.0, b"inner", "1 match"),
                (10.0, b"\x1b[C", "leaf-key-sentinel"),
                (10.0, b"\x1b[D", "inner-key-sentinel"),
                (10.0, b"\r", "picked:"),
            ],
            extra_path=[str(_REPO / "tests")],
            initial_wait_for="folder-key-sentinel",
            initial_drain=20.0,
            timeout=60.0,
        )
        self.assertEqual(trace.exit_code, 0, trace.plain[-3000:])
        self.assertIn("picked:folder-key-sentinel/inner-key-sentinel", trace.plain)
        self.assertNotIn("Traceback", trace.plain)

    def test_keyboard_and_mouse_open_settings_return_and_exit_cleanly(self):
        from harness.pty_tui import run_python_snippet

        with tempfile.TemporaryDirectory() as directory:
            coordinates = Path(directory) / "settings-position.json"
            script = _CHILD_SCRIPT.replace(
                "rc = uxon_tui.run(ctx)",
                _READY_CHILD.format(coordinates=str(coordinates))
                + '\nrc = uxon_tui.run(ctx)\nprint(f"PTY_EXIT:{rc}", flush=True)',
            )

            def keys():
                # Reverse traversal reaches Settings through the actual focus chain.
                yield (10.0, b"\x1b[A\x1b[A\r", "READY_SETTINGS_1")
                yield (10.0, b"q", "READY_MAIN_2")
                x, y = json.loads(coordinates.read_text())
                press, release = sgr_click(x, y)
                yield (10.0, press + release, "READY_SETTINGS_2")
                yield (10.0, b"q", "READY_MAIN_3")
                yield (10.0, b"q", "PTY_EXIT:0")

            # These ACKs run after paint and explicitly verify live focus. Other
            # terminal scenarios retain the harness's longer empirical settle.
            with mock.patch("harness.pty_tui._SETTLE_MS", 100):
                trace = run_python_snippet(
                    script, keys(), initial_wait_for="READY_MAIN_1", initial_drain=20, timeout=60
                )
            self.assertEqual(trace.exit_code, 0, trace.plain[-3000:])
            self.assertIn("Settings", trace.plain)
            self.assertIn("READY_SETTINGS_1", trace.plain)
            self.assertIn("READY_SETTINGS_2", trace.plain)
            self.assertIn("READY_MAIN_3", trace.plain)


_READY_CHILD = r"""
import json
from pathlib import Path
import uxon.tui.app as app_module
from uxon.tui.screens.main import MainScreen
from uxon.tui.screens.settings import SettingsScreen
from uxon.tui.screens.launch_options import LaunchOptionsScreen
readiness_counts = {{"MAIN":0,"SETTINGS":0,"OPTIONS":0}}
app_generations = [0]
class ReadyApp(app_module.UxonApp):
    CSS_PATH = Path(app_module.__file__).with_name("styles.tcss")
    def on_mount(self):
        super().on_mount()
        app_generations[0] += 1
        self.generation = app_generations[0]
        self.acknowledged_app = False
        self.previous_role = None
        self.guard_verified = False
        self.set_interval(0.02, lambda: self.call_after_refresh(self.ack))
    def ack(self):
        screen = self.screen
        role = "MAIN" if isinstance(screen, MainScreen) else "SETTINGS" if isinstance(screen, SettingsScreen) else "OPTIONS" if isinstance(screen, LaunchOptionsScreen) else None
        if role is None or role == self.previous_role or self.focused is None or screen.size.height <= 0:
            return
        if role == "MAIN":
            if not self.guard_verified:
                from uxon.infra.loop_guard import EventLoopBlockedError
                from uxon.infra.run import run_query
                try:
                    run_query([sys.executable, "-c", "pass"],timeout=1)
                except EventLoopBlockedError:
                    self.guard_verified = True
                else:
                    raise AssertionError("real runner failed to install event-loop spawn guard")
            row = screen.query_one("#action-settings")
            if row.region.height <= 0 or not row.display:
                return
            Path({coordinates!r}).write_text(json.dumps([row.region.x+3,row.region.y+1]))
        elif role == "SETTINGS" and getattr(self.focused, "id", None) != "settings-table":
            return
        elif role == "OPTIONS" and (getattr(self.focused, "id", None) != "mode-list" or screen.query_one("#mode-list").index is None):
            return
        # A dismissed launch picker can briefly reveal Main while the old
        # App prepares its handoff. Only a fresh App can acknowledge reentry.
        if role == "MAIN" and not self.acknowledged_app:
            self.acknowledged_app = True
            os.write(1, f"READY_APP_{{self.generation}}\n".encode())
        self.previous_role = role
        readiness_counts[role] += 1
        os.write(1, f"READY_{{role}}_{{readiness_counts[role]}}\n".encode())
app_module.UxonApp = ReadyApp
"""


@unittest.skipUnless(_PTY_OK, "pty unavailable on this platform")
class DrainAfterLaunchTests(unittest.TestCase):
    def test_key_typed_during_launch_does_not_stale_activate(self):
        from harness.pty_tui import run_python_snippet

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release, marker = root / "release", root / "launches"
            workload = f"""
import time
from pathlib import Path
print("WORKLOAD_STARTED", flush=True)
deadline = time.monotonic()+15
while not Path({str(release)!r}).exists():
    if time.monotonic() >= deadline:
        raise TimeoutError("workload release missing")
    time.sleep(0.01)
print("WORKLOAD_FINISHED", flush=True)
"""
            setup = f"""
from uxon.tui.context import LaunchRequest
def launch(profile, mode, target_dir=None):
    with open({str(marker)!r}, 'a') as stream:
        stream.write(f"cwd:{{profile}}:{{mode}}\\n")
    return LaunchRequest((sys.executable,'-c',{workload!r}), label="workload")
ctx.on_launch_cwd = launch
ctx.on_refresh = lambda: ctx
"""
            ready = _READY_CHILD.format(coordinates=str(root / "position"))
            script = _CHILD_SCRIPT.replace(
                "rc = uxon_tui.run(ctx)",
                setup + ready + '\nrc = uxon_tui.run(ctx)\nprint(f"PTY_EXIT:{rc}", flush=True)',
            )

            def keys():
                yield (10.0, b"\r", "READY_OPTIONS_1")
                yield (10.0, b"\r", "WORKLOAD_STARTED")
                self.assertFalse(release.exists())
                # Enter would open a new launch modal if delivered after reentry.
                yield (0.2, b"\r")
                release.touch()
                yield (10.0, b"", "READY_APP_2")
                yield (10.0, b"q", "PTY_EXIT:0")

            with mock.patch("harness.pty_tui._SETTLE_MS", 100):
                trace = run_python_snippet(
                    script, keys(), initial_wait_for="READY_APP_1", initial_drain=20, timeout=60
                )
            self.assertEqual(trace.exit_code, 0, trace.plain[-3000:])
            self.assertEqual(marker.read_text(), "cwd:claude:normal\n")
            self.assertIn("WORKLOAD_FINISHED", trace.plain)
            self.assertNotIn("READY_OPTIONS_2", trace.plain)

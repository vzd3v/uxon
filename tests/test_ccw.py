import importlib.util
import io
import sys
import tempfile
import textwrap
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


CCW_PATH = Path(__file__).resolve().parents[1] / "bin" / "ccw"
_LIB_PATH = str(Path(__file__).resolve().parents[1] / "lib")
if _LIB_PATH not in sys.path:
    sys.path.insert(0, _LIB_PATH)
LOADER = SourceFileLoader("ccw_module", str(CCW_PATH))
SPEC = importlib.util.spec_from_loader("ccw_module", LOADER)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"failed to load spec for {CCW_PATH}")
ccw = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ccw
SPEC.loader.exec_module(ccw)


class _StubsChain:
    """Tiny helper to combine multiple ``mock.patch`` context managers into
    one ``with`` statement for readability in tests."""

    def __init__(self, *patches):
        self._patches = patches
        self._entered = []

    def __enter__(self):
        for p in self._patches:
            self._entered.append(p.__enter__())
        return self

    def __exit__(self, exc_type, exc, tb):
        for p in reversed(self._patches):
            p.__exit__(exc_type, exc, tb)
        return False


class CcwTests(unittest.TestCase):
    def make_config(self, **overrides) -> ccw.Config:
        defaults = dict(
            runtime_user="",
            default_launch_mode="caller",
            enable_all_users_list=False,
            launch_user_by_caller={},
            session_users=[],
            allowed_roots=["/srv/repos"],
            session_prefix="ccw-",
            enabled_agents=("claude",),
            default_agent="claude",
            agent_default_args={"claude": (), "codex": (), "cursor": ()},
            new_project_root="/srv/repos",
            repeat_noninteractive_mode="fail",
            tmux_socket_template="/tmp/ccw-{user}.sock",
            tui_refresh_interval_seconds=2.0,
            git_create_enabled=False,
            default_git_remote_profile="",
            git_remote_profiles=[],
        )
        defaults.update(overrides)
        return ccw.Config(**defaults)

    def make_session(
        self,
        name: str,
        path: str,
        *,
        attached: str = "0",
    ) -> ccw.SessionInfo:
        return ccw.SessionInfo(
            user="u-vz",
            name=name,
            attached=attached,
            windows="1",
            created="2026-04-15T06:00:00+00:00",
            last_attached="2026-04-15T06:00:00+00:00",
            pane_pids=(),
            active_pid=1234,
            active_cmd="claude",
            active_path=path,
        )

    def test_resolve_caller_user_prefers_current_non_root_user(self) -> None:
        with mock.patch.object(ccw, "process_user", return_value="u-vz"):
            with mock.patch.dict(ccw.os.environ, {"SUDO_USER": "remdepl"}, clear=False):
                self.assertEqual(ccw.resolve_caller_user(), "u-vz")

    def test_parse_args_supports_version_flags(self) -> None:
        parsed_long = ccw.parse_args(["--version"])
        self.assertEqual(parsed_long.action, "version")

        parsed_short = ccw.parse_args(["-V"])
        self.assertEqual(parsed_short.action, "version")

        parsed_subcommand = ccw.parse_args(["version"])
        self.assertEqual(parsed_subcommand.action, "version")

    def test_parse_args_supports_doctor(self) -> None:
        parsed = ccw.parse_args(["doctor"])
        self.assertEqual(parsed.action, "doctor")

    def test_parse_args_supports_kill_all_force(self) -> None:
        parsed = ccw.parse_args(["kill-all", "--force"])
        self.assertEqual(parsed.action, "kill-all")
        self.assertTrue(parsed.force)

    def _make_config_explicit(self, **kw) -> ccw.Config:
        """Make a Config with explicit fields (no make_config helper)."""
        return ccw.Config(
            runtime_user=kw.get("runtime_user", ""),
            default_launch_mode=kw.get("default_launch_mode", "caller"),
            enable_all_users_list=kw.get("enable_all_users_list", False),
            launch_user_by_caller=kw.get("launch_user_by_caller", {}),
            session_users=kw.get("session_users", []),
            allowed_roots=kw.get("allowed_roots", ["/srv"]),
            session_prefix=kw.get("session_prefix", "ccw-"),
            enabled_agents=kw.get("enabled_agents", ("claude",)),
            default_agent=kw.get("default_agent", "claude"),
            agent_default_args=kw.get("agent_default_args", {"claude": (), "codex": (), "cursor": ()}),
            new_project_root=kw.get("new_project_root", "/srv/agentdev"),
            repeat_noninteractive_mode=kw.get("repeat_noninteractive_mode", "fail"),
            tmux_socket_template=kw.get("tmux_socket_template", "/tmp/ccw-{user}.sock"),
            tui_refresh_interval_seconds=kw.get("tui_refresh_interval_seconds", 2.0),
            git_create_enabled=kw.get("git_create_enabled", False),
            default_git_remote_profile=kw.get("default_git_remote_profile", ""),
            git_remote_profiles=kw.get("git_remote_profiles", []),
        )

    def test_resolve_launch_user_fixed_mode_uses_runtime_user(self) -> None:
        cfg = self._make_config_explicit(runtime_user="devagent", default_launch_mode="fixed",
                                         session_users=["devagent"])
        self.assertEqual(ccw.resolve_launch_user(cfg, "remdepl"), "devagent")

    def test_resolve_launch_user_caller_mode_uses_caller(self) -> None:
        cfg = self._make_config_explicit(runtime_user="devagent", default_launch_mode="caller",
                                         session_users=["devagent", "remdepl"])
        self.assertEqual(ccw.resolve_launch_user(cfg, "remdepl"), "remdepl")

    def test_resolve_launch_user_mapping_overrides_default(self) -> None:
        cfg = self._make_config_explicit(runtime_user="devagent", default_launch_mode="caller",
                                         enable_all_users_list=True,
                                         launch_user_by_caller={"remdepl": "devagent"},
                                         session_users=["devagent", "remdepl"])
        self.assertEqual(ccw.resolve_launch_user(cfg, "remdepl"), "devagent")

    def test_resolve_all_session_users_keeps_current_user_present(self) -> None:
        cfg = self._make_config_explicit(runtime_user="devagent", default_launch_mode="fixed",
                                         enable_all_users_list=True,
                                         session_users=["devagent"])
        self.assertEqual(ccw.resolve_all_session_users(cfg, "remdepl"), ["devagent", "remdepl"])

    def test_parse_args_supports_all_users_listing(self) -> None:
        parsed = ccw.parse_args(["list", "--all-users"])
        self.assertEqual(parsed.action, "list")
        self.assertTrue(parsed.all_users)

        parsed_short = ccw.parse_args(["-l", "--all-users"])
        self.assertEqual(parsed_short.action, "list")
        self.assertTrue(parsed_short.all_users)

    def test_parse_args_supports_repeat_mode_flags_for_new(self) -> None:
        parsed_attach = ccw.parse_args(["-n", "demo", "--attach-existing"])
        self.assertEqual(parsed_attach.action, "new")
        self.assertEqual(parsed_attach.repeat_mode, "attach")

        parsed_new = ccw.parse_args(["new", "demo", "--new-session"])
        self.assertEqual(parsed_new.action, "new")
        self.assertEqual(parsed_new.repeat_mode, "new")

    def _write_and_load_cfg(self, toml_content: str, tmpdir: str) -> ccw.Config:
        """Helper: write a config.toml in tmpdir and load_config from there."""
        tmp_path = Path(tmpdir)
        cwd = tmp_path / "workspace"
        cwd.mkdir(exist_ok=True)
        repo_cfg = tmp_path / "repo-config.toml"
        repo_cfg.write_text(toml_content, encoding="utf-8")

        def fake_load_toml(path: Path) -> dict[str, object]:
            if path == tmp_path / "config" / "config.toml":
                with repo_cfg.open("rb") as fh:
                    return ccw.tomllib.load(fh)
            return {}

        with mock.patch.object(ccw, "repo_root", return_value=tmp_path):
            with mock.patch.object(ccw, "find_project_config", return_value=None):
                with mock.patch.object(ccw, "canonical", side_effect=lambda v: str(v)):
                    with mock.patch.object(ccw, "load_toml", side_effect=fake_load_toml):
                        return ccw.load_config(str(cwd))

    def test_load_config_reads_new_multi_user_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._write_and_load_cfg(
                textwrap.dedent("""
                    runtime_user = "devagent"
                    default_launch_mode = "caller"
                    enable_all_users_list = true
                    session_users = ["devagent", "remdepl"]
                    allowed_roots = ["/srv", "/tmp"]
                    session_prefix = "ccw-"
                    repeat_noninteractive_mode = "attach"
                    tmux_socket_template = "/tmp/ccw-{user}-{uid}.sock"

                    [agents]
                    enabled = ["claude"]
                    default = "claude"

                    [agents.claude]
                    default_args = ["--model", "sonnet"]

                    [launch_user_by_caller]
                    remdepl = "devagent"
                """).strip() + "\n",
                tmpdir,
            )

        self.assertEqual(cfg.runtime_user, "devagent")
        self.assertEqual(cfg.default_launch_mode, "caller")
        self.assertTrue(cfg.enable_all_users_list)
        self.assertEqual(cfg.session_users, ["devagent", "remdepl"])
        self.assertEqual(cfg.launch_user_by_caller, {"remdepl": "devagent"})
        self.assertEqual(cfg.agent_default_args["claude"], ("--model", "sonnet"))
        self.assertEqual(cfg.enabled_agents, ("claude",))
        self.assertEqual(cfg.default_agent, "claude")
        self.assertEqual(cfg.repeat_noninteractive_mode, "attach")
        self.assertEqual(cfg.tmux_socket_template, "/tmp/ccw-{user}-{uid}.sock")

    def test_load_config_reads_tui_refresh_interval(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._write_and_load_cfg("tui_refresh_interval_seconds = 5\n", tmpdir)
        self.assertEqual(cfg.tui_refresh_interval_seconds, 5.0)

    def test_load_config_rejects_invalid_tui_refresh_interval(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit):
                self._write_and_load_cfg("tui_refresh_interval_seconds = 0\n", tmpdir)

    def test_load_config_reads_git_remote_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._write_and_load_cfg(
                textwrap.dedent("""
                    git_create_enabled = true
                    default_git_remote_profile = "vzd3v-gh"

                    [[git_remote_profiles]]
                    name = "vzd3v-gh"
                    host = "github.com"
                    owner = "vzd3v"
                    auth = "gh"
                    creds_user = "remdepl"
                    visibility = "private"

                    [[git_remote_profiles]]
                    name = "acme-tok"
                    host = "github.com"
                    owner = "acme"
                    auth = "token"
                    creds_user = "remdepl"
                    token_file = "/home/remdepl/.secrets/acme.token"
                """).strip() + "\n",
                tmpdir,
            )

        self.assertTrue(cfg.git_create_enabled)
        self.assertEqual(cfg.default_git_remote_profile, "vzd3v-gh")
        self.assertEqual([p.name for p in cfg.git_remote_profiles], ["vzd3v-gh", "acme-tok"])
        self.assertEqual(cfg.git_remote_profiles[1].token_file, "/home/remdepl/.secrets/acme.token")

    def test_load_config_rejects_default_pointing_to_missing_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit):
                self._write_and_load_cfg(
                    'default_git_remote_profile = "missing"\n',
                    tmpdir,
                )

    def test_load_config_defaults_enable_claude_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._write_and_load_cfg("", tmpdir)
        self.assertEqual(cfg.enabled_agents, ("claude",))
        self.assertEqual(cfg.default_agent, "claude")
        self.assertEqual(cfg.agent_default_args["claude"], ())

    def test_load_config_multi_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._write_and_load_cfg(
                textwrap.dedent("""
                    [agents]
                    enabled = ["claude", "cursor"]
                    default = "cursor"

                    [agents.claude]
                    default_args = ["--verbose"]

                    [agents.cursor]
                    default_args = []
                """).strip() + "\n",
                tmpdir,
            )
        self.assertEqual(cfg.enabled_agents, ("claude", "cursor"))
        self.assertEqual(cfg.default_agent, "cursor")
        self.assertEqual(cfg.agent_default_args["claude"], ("--verbose",))

    def test_load_config_rejects_legacy_flat_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit):
                self._write_and_load_cfg(
                    'default_claude_args = ["--verbose"]\n',
                    tmpdir,
                )

    def test_load_config_rejects_unknown_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit):
                self._write_and_load_cfg(
                    '[agents]\nenabled = ["nosuch"]\ndefault = "nosuch"\n',
                    tmpdir,
                )

    def test_load_config_rejects_default_not_in_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit):
                self._write_and_load_cfg(
                    '[agents]\nenabled = ["claude"]\ndefault = "codex"\n',
                    tmpdir,
                )

    def test_parse_new_with_git_remote(self) -> None:
        parsed = ccw.parse_subcommand(
            ["new", "demo", "--git-remote", "prof-a", "--git-visibility", "public"]
        )
        self.assertEqual(parsed.action, "new")
        self.assertEqual(parsed.target_id, "demo")
        self.assertEqual(parsed.git_remote, "prof-a")
        self.assertEqual(parsed.git_visibility, "public")
        self.assertFalse(parsed.no_git)

    def test_parse_new_no_git(self) -> None:
        parsed = ccw.parse_subcommand(["new", "demo", "--no-git"])
        self.assertTrue(parsed.no_git)
        self.assertIsNone(parsed.git_remote)

    def test_parse_new_git_remote_default(self) -> None:
        parsed = ccw.parse_subcommand(["new", "demo", "--git-remote", "default"])
        self.assertEqual(parsed.git_remote, "default")

    def test_parse_new_rejects_git_remote_with_no_git(self) -> None:
        with self.assertRaises(SystemExit):
            ccw.parse_subcommand(["new", "demo", "--git-remote", "p", "--no-git"])

    def test_parse_new_rejects_bad_visibility(self) -> None:
        with self.assertRaises(SystemExit):
            ccw.parse_subcommand(["new", "demo", "--git-visibility", "secret"])

    def test_do_new_git_remote_dry_run_invokes_orchestrator(self) -> None:
        profile = {
            "name": "prof-a",
            "host": "github.com",
            "owner": "vzd3v",
            "auth": "gh",
            "creds_user": "remdepl",
            "visibility": "private",
        }
        import sys as _sys
        _sys.path.insert(0, str(CCW_PATH.parent.parent / "lib"))
        import ccw_git_profiles
        cfg = self.make_config(
            allowed_roots=["/srv/repos"],
            git_create_enabled=True,
            default_git_remote_profile="prof-a",
            git_remote_profiles=ccw_git_profiles.load_profiles([profile]),
        )
        args = ccw.ParsedArgs(
            action="new",
            target_id="demo",
            dry_run=True,
            git_remote="prof-a",
            agent_args=[],
        )

        calls = []

        def fake_create(profile_arg, repo_name, project_dir, **kwargs):
            calls.append(
                {
                    "name": profile_arg.name,
                    "repo": repo_name,
                    "dir": project_dir,
                    "dry_run": kwargs.get("dry_run"),
                    "launch_user": kwargs.get("launch_user"),
                    "current_user": kwargs.get("current_user"),
                }
            )
            import ccw_git_create
            return ccw_git_create.CreationResult(
                profile_name=profile_arg.name,
                ssh_url=f"git@github.com:vzd3v/{repo_name}.git",
                commands=["would run: git init"],
            )

        import ccw_git_create
        with mock.patch.object(ccw_git_create, "create_project_remote", side_effect=fake_create):
            with mock.patch.object(ccw, "collect_sessions", return_value=[]):
                with mock.patch.object(ccw, "launch_in_tmux", return_value=0):
                    with mock.patch.object(ccw, "is_interactive_tty", return_value=False):
                        ccw.do_new(args, cfg, "devagent")

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "prof-a")
        self.assertEqual(calls[0]["repo"], "demo")
        self.assertEqual(calls[0]["dir"], "/srv/repos/demo")
        self.assertTrue(calls[0]["dry_run"])
        self.assertEqual(calls[0]["launch_user"], "devagent")

    def test_do_new_git_remote_rejects_disabled_feature(self) -> None:
        cfg = self.make_config(git_create_enabled=False)
        args = ccw.ParsedArgs(
            action="new",
            target_id="demo",
            git_remote="default",
            dry_run=True,
            agent_args=[],
        )
        with mock.patch.object(ccw, "is_interactive_tty", return_value=False):
            with self.assertRaisesRegex(SystemExit, "2"):
                ccw.do_new(args, cfg, "devagent")

    def test_do_new_git_remote_with_worktree_fails(self) -> None:
        import sys as _sys
        _sys.path.insert(0, str(CCW_PATH.parent.parent / "lib"))
        import ccw_git_profiles
        cfg = self.make_config(
            git_create_enabled=True,
            default_git_remote_profile="prof-a",
            git_remote_profiles=ccw_git_profiles.load_profiles(
                [
                    {
                        "name": "prof-a",
                        "host": "github.com",
                        "owner": "vzd3v",
                        "auth": "gh",
                        "creds_user": "remdepl",
                        "visibility": "private",
                    }
                ]
            ),
        )
        args = ccw.ParsedArgs(
            action="new",
            target_id="demo",
            worktree_branch="feature",
            git_remote="prof-a",
            dry_run=True,
            agent_args=[],
        )
        with mock.patch.object(ccw, "os", wraps=ccw.os) as m_os:
            m_os.path.isdir.return_value = True
            with mock.patch.object(ccw, "git_repo_root_as_user", return_value="/srv/repos/demo"):
                with self.assertRaises(SystemExit):
                    ccw.do_new(args, cfg, "devagent")

    def test_parse_run_rejects_git_flags(self) -> None:
        with self.assertRaises(SystemExit):
            ccw.parse_subcommand(["run", "--git-remote", "p"])
        with self.assertRaises(SystemExit):
            ccw.parse_subcommand(["run", "--no-git"])
        with self.assertRaises(SystemExit):
            ccw.parse_subcommand(["run", "--git-visibility", "private"])

    def test_format_version_reads_version_file_and_commit(self) -> None:
        with mock.patch.object(ccw, "read_repo_version", return_value="0.2.0"):
            with mock.patch.object(ccw, "read_git_commit_short", return_value="abc1234"):
                with mock.patch.object(ccw, "repo_is_dirty", return_value=False):
                    self.assertEqual(ccw.format_version(), "ccw 0.2.0 (abc1234)")

    def test_format_version_marks_dirty_checkout(self) -> None:
        with mock.patch.object(ccw, "read_repo_version", return_value="0.2.0"):
            with mock.patch.object(ccw, "read_git_commit_short", return_value="abc1234"):
                with mock.patch.object(ccw, "repo_is_dirty", return_value=True):
                    self.assertEqual(ccw.format_version(), "ccw 0.2.0 (abc1234-dirty)")

    def test_do_new_allows_call_from_outside_allowed_roots(self) -> None:
        cfg = self.make_config()
        args = ccw.ParsedArgs(action="new", target_id="demo", dry_run=True, agent_args=[])

        with mock.patch.object(ccw.os, "getcwd", return_value="/home/u-vz"):
            with mock.patch.object(ccw, "canonical", side_effect=lambda value: str(value)):
                with mock.patch.object(ccw, "collect_sessions", return_value=[]):
                    with mock.patch.object(ccw, "allocate_session_name", return_value="ccw-demo"):
                        with mock.patch.object(ccw, "launch_in_tmux", return_value=0) as launch:
                            result = ccw.do_new(args, cfg, "u-vz")

        self.assertEqual(result, 0)
        launch.assert_called_once()

    def test_do_new_existing_session_defaults_to_attach_in_tty(self) -> None:
        cfg = self.make_config()
        args = ccw.ParsedArgs(action="new", target_id="demo", agent_args=[])
        existing = [self.make_session("ccw-demo@claude", "/srv/repos/demo")]

        with mock.patch.object(ccw, "canonical", side_effect=lambda value: str(value)):
            with mock.patch.object(ccw, "run_cmd") as run_cmd:
                with mock.patch.object(ccw, "collect_sessions", return_value=existing):
                    with mock.patch.object(ccw, "is_interactive_tty", return_value=True):
                        with mock.patch("builtins.input", return_value=""):
                            with mock.patch.object(ccw, "attach_session", return_value=0) as attach:
                                with mock.patch.object(ccw, "launch_in_tmux", return_value=0) as launch:
                                    result = ccw.do_new(args, cfg, "u-vz")

        self.assertEqual(result, 0)
        run_cmd.assert_called_once()
        attach.assert_called_once()
        launch.assert_not_called()

    def test_do_new_existing_session_force_new_bypasses_prompt(self) -> None:
        cfg = self.make_config()
        args = ccw.ParsedArgs(action="new", target_id="demo", repeat_mode="new", agent_args=[])
        existing = [self.make_session("ccw-demo@claude", "/srv/repos/demo")]

        with mock.patch.object(ccw, "canonical", side_effect=lambda value: str(value)):
            with mock.patch.object(ccw, "run_cmd") as run_cmd:
                with mock.patch.object(ccw, "collect_sessions", return_value=existing):
                    with mock.patch.object(ccw, "allocate_session_name", return_value="ccw-demo-2") as allocate:
                        with mock.patch.object(ccw, "launch_in_tmux", return_value=0) as launch:
                            result = ccw.do_new(args, cfg, "u-vz")

        self.assertEqual(result, 0)
        run_cmd.assert_called_once()
        allocate.assert_called_once()
        launch.assert_called_once()

    def test_do_new_existing_session_without_tty_fails_with_guidance(self) -> None:
        cfg = self.make_config()
        args = ccw.ParsedArgs(action="new", target_id="demo", agent_args=[])
        existing = [self.make_session("ccw-demo@claude", "/srv/repos/demo")]

        with mock.patch.object(ccw, "canonical", side_effect=lambda value: str(value)):
            with mock.patch.object(ccw, "run_cmd") as run_cmd:
                with mock.patch.object(ccw, "collect_sessions", return_value=existing):
                    with mock.patch.object(ccw, "is_interactive_tty", return_value=False):
                        with mock.patch.object(ccw, "eprint") as eprint:
                            with self.assertRaises(SystemExit) as ctx:
                                ccw.do_new(args, cfg, "u-vz")

        self.assertEqual(ctx.exception.code, 2)
        run_cmd.assert_called_once()
        eprint.assert_called()
        self.assertIn("--attach-existing", eprint.call_args[0][0])
        self.assertIn("--new-session", eprint.call_args[0][0])

    def test_do_new_existing_worktree_session_defaults_to_attach_in_tty(self) -> None:
        cfg = self.make_config()
        args = ccw.ParsedArgs(action="new", target_id="demo", worktree_branch="feature-x", agent_args=[])
        existing = [self.make_session("ccw-demo-feature-x@claude", "/srv/repos/demo")]

        with mock.patch.object(ccw.os.path, "isdir", return_value=True):
            with mock.patch.object(ccw, "git_repo_root_as_user", return_value="/srv/repos/demo"):
                with mock.patch.object(ccw, "collect_sessions", return_value=existing):
                    with mock.patch.object(ccw, "is_interactive_tty", return_value=True):
                        with mock.patch("builtins.input", return_value=""):
                            with mock.patch.object(ccw, "attach_session", return_value=0) as attach:
                                with mock.patch.object(ccw, "launch_in_tmux", return_value=0) as launch:
                                    result = ccw.do_new(args, cfg, "u-vz")

        self.assertEqual(result, 0)
        attach.assert_called_once()
        launch.assert_not_called()

    def test_do_new_existing_worktree_session_uses_configured_noninteractive_new(self) -> None:
        cfg = self.make_config()
        cfg.repeat_noninteractive_mode = "new"
        args = ccw.ParsedArgs(action="new", target_id="demo", worktree_branch="feature-x", agent_args=[])
        existing = [self.make_session("ccw-demo-feature-x@claude", "/srv/repos/demo")]

        with mock.patch.object(ccw.os.path, "isdir", return_value=True):
            with mock.patch.object(ccw, "git_repo_root_as_user", return_value="/srv/repos/demo"):
                with mock.patch.object(ccw, "collect_sessions", return_value=existing):
                    with mock.patch.object(ccw, "is_interactive_tty", return_value=False):
                        with mock.patch.object(ccw, "allocate_session_name", return_value="ccw-demo-feature-x-2") as allocate:
                            with mock.patch.object(ccw, "launch_in_tmux", return_value=0) as launch:
                                result = ccw.do_new(args, cfg, "u-vz")

        self.assertEqual(result, 0)
        allocate.assert_called_once()
        launch.assert_called_once()

    def test_do_new_legacy_socket_guardrail_fails(self) -> None:
        cfg = self.make_config()
        args = ccw.ParsedArgs(action="new", target_id="demo", agent_args=[])
        legacy = [self.make_session("ccw-demo@claude", "/srv/repos/demo")]

        with mock.patch.object(ccw, "canonical", side_effect=lambda value: str(value)):
            with mock.patch.object(ccw, "run_cmd"):
                with mock.patch.object(ccw, "collect_sessions", return_value=[]):
                    with mock.patch.object(ccw, "collect_sessions_for_user", return_value=legacy):
                        with mock.patch.object(ccw, "tmux_socket_path", return_value="/tmp/ccw-u-vz.sock"):
                            with mock.patch.object(ccw, "eprint") as eprint:
                                with self.assertRaises(SystemExit) as ctx:
                                    ccw.do_new(args, cfg, "u-vz")

        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("legacy default tmux socket", eprint.call_args[0][0])

    def test_resolve_repeat_decision_prefers_env_override(self) -> None:
        cfg = self.make_config()
        cfg.repeat_noninteractive_mode = "fail"
        session = self.make_session("ccw-demo", "/srv/repos/demo")

        with mock.patch.object(ccw, "is_interactive_tty", return_value=False):
            with mock.patch.dict(ccw.os.environ, {"CCW_REPEAT_NONINTERACTIVE_POLICY": "attach"}, clear=False):
                decision = ccw.resolve_repeat_decision("none" if False else None, cfg, "/srv/repos/demo", session, [session])

        self.assertEqual(decision, "attach")

    def test_tmux_socket_path_expands_template(self) -> None:
        cfg = self.make_config()
        cfg.tmux_socket_template = "/tmp/ccw-{user}-{uid}.sock"

        with mock.patch.object(ccw.pwd, "getpwnam") as getpwnam:
            getpwnam.return_value = mock.Mock(pw_uid=1001)
            path = ccw.tmux_socket_path(cfg, "u-vz")

        self.assertEqual(path, "/tmp/ccw-u-vz-1001.sock")

    def test_doctor_reports_socket_and_config(self) -> None:
        import ccw_agents

        cfg = self.make_config()
        output = io.StringIO()
        ok_avail = ccw_agents.AgentAvailability(status="ok", version="1.2.3")

        def _command_path_side_effect(command, user):
            if command == "tmux":
                return "/usr/bin/tmux"
            return "/usr/local/bin/claude"

        with mock.patch.object(ccw, "resolve_config_layers", return_value=({}, [Path("/srv/apps/vz_devagent_cli_tool/config/config.toml")])):
            with mock.patch.object(ccw, "tmux_socket_path", return_value="/tmp/ccw-u-vz.sock"):
                with mock.patch.object(ccw, "command_path_for_user", side_effect=_command_path_side_effect):
                    with mock.patch.object(ccw_agents, "probe_agents", return_value={"claude": ok_avail}):
                        with mock.patch.object(ccw, "collect_sessions", return_value=[self.make_session("ccw-demo@claude", "/srv/repos/demo")]):
                            with mock.patch.object(ccw, "collect_sessions_for_user", return_value=[]):
                                with mock.patch.object(ccw, "user_can_write_dir", return_value=True):
                                    with mock.patch.object(ccw, "format_version", return_value="ccw 0.4.0 (abc1234)"):
                                        with mock.patch("sys.stdout", output):
                                            rc = ccw.do_doctor(cfg, "remdepl", "u-vz", "/srv/repos/demo")

        self.assertEqual(rc, 0)
        rendered = output.getvalue()
        self.assertIn("ccw doctor", rendered)
        self.assertIn("config_paths=/srv/apps/vz_devagent_cli_tool/config/config.toml", rendered)
        self.assertIn("tmux_socket=/tmp/ccw-u-vz.sock", rendered)
        self.assertIn("claude:", rendered)
        self.assertIn("ok (1.2.3)", rendered)

    def test_doctor_reports_missing_agent(self) -> None:
        import ccw_agents

        cfg = self.make_config()
        output = io.StringIO()
        missing_avail = ccw_agents.AgentAvailability(status="missing", error="not found")

        with mock.patch.object(ccw, "resolve_config_layers", return_value=({}, [])):
            with mock.patch.object(ccw, "tmux_socket_path", return_value="/tmp/ccw-u-vz.sock"):
                with mock.patch.object(ccw, "command_path_for_user", return_value="/usr/bin/tmux"):
                    with mock.patch.object(ccw_agents, "probe_agents", return_value={"claude": missing_avail}):
                        with mock.patch.object(ccw, "collect_sessions", return_value=[]):
                            with mock.patch.object(ccw, "collect_sessions_for_user", return_value=[]):
                                with mock.patch.object(ccw, "user_can_write_dir", return_value=True):
                                    with mock.patch.object(ccw, "format_version", return_value="ccw 0.4.0"):
                                        with mock.patch("sys.stdout", output):
                                            rc = ccw.do_doctor(cfg, "u-vz", "u-vz", "/srv/repos/demo")

        rendered = output.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("MISSING", rendered)
        self.assertIn("claude:", rendered)

    def test_do_kill_all_requires_force_without_tty(self) -> None:
        cfg = self.make_config()
        args = ccw.ParsedArgs(action="kill-all", force=False)
        sessions = [self.make_session("ccw-demo", "/srv/repos/demo")]

        with mock.patch.object(ccw, "collect_sessions", return_value=sessions):
            with mock.patch.object(ccw, "is_interactive_tty", return_value=False):
                with mock.patch.object(ccw, "eprint") as eprint:
                    with self.assertRaises(SystemExit) as ctx:
                        ccw.do_kill_all(args, cfg, "u-vz")

        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("--force", eprint.call_args[0][0])

    def _stub_socket_path(self):
        # Classic tests run as if the process is NOT inside tmux so the
        # build_request helpers stay on the execvp / attach-session /
        # new-session path.
        return _StubsChain(
            mock.patch.object(ccw, "tmux_socket_path", return_value="/tmp/ccw-test.sock"),
            mock.patch.object(ccw, "tmux_host_socket", return_value=None),
        )

    def test_build_tmux_attach_request_produces_expected_argv(self) -> None:
        cfg = self.make_config()
        target = self.make_session("ccw-demo", "/srv/repos/demo")
        with self._stub_socket_path():
            req = ccw._build_tmux_attach_request(target, cfg, "u-vz")
        self.assertIn("attach-session", req.cmd)
        self.assertIn("ccw-demo", req.cmd)
        self.assertEqual(req.prelaunch, ())
        self.assertIn("attach", req.label)

    def test_build_tmux_launch_request_includes_claude_and_mkdir(self) -> None:
        cfg = self.make_config(agent_default_args={"claude": ("--model", "sonnet"), "codex": (), "cursor": ()})
        args = ccw.ParsedArgs(action="run", permission_mode="yolo", agent_args=["--foo"])
        with self._stub_socket_path():
            req = ccw._build_tmux_launch_request("/srv/repos/demo", "ccw-demo@claude", args, cfg, None, "u-vz")
        self.assertIn("new-session", req.cmd)
        self.assertIn("-As", req.cmd)
        self.assertIn("ccw-demo@claude", req.cmd)
        # agent_default_args + yolo flag + caller's agent_args all flow through
        self.assertIn("claude", req.cmd)
        self.assertIn("--model", req.cmd)
        self.assertIn("sonnet", req.cmd)
        self.assertIn("--dangerously-skip-permissions", req.cmd)
        self.assertIn("--foo", req.cmd)
        # prelaunch mkdir for the socket parent
        self.assertEqual(len(req.prelaunch), 1)
        pre = req.prelaunch[0]
        self.assertIn("mkdir", pre)
        self.assertIn("-p", pre)

    def test_attach_session_blocking_uses_subprocess_not_execvp(self) -> None:
        cfg = self.make_config()
        target = self.make_session("ccw-demo", "/srv/repos/demo")
        with self._stub_socket_path():
            with mock.patch.object(ccw.subprocess, "call", return_value=0) as call:
                with mock.patch.object(ccw.os, "execvp") as execvp:
                    rc = ccw.attach_session_blocking(target, cfg, "u-vz")
        self.assertEqual(rc, 0)
        call.assert_called_once()
        execvp.assert_not_called()

    def test_launch_in_tmux_blocking_runs_prelaunch_then_cmd(self) -> None:
        cfg = self.make_config()
        args = ccw.ParsedArgs(action="run", agent_args=[])
        with self._stub_socket_path():
            with mock.patch.object(ccw.subprocess, "call", side_effect=[0, 0]) as call:
                with mock.patch.object(ccw.os, "execvp") as execvp:
                    rc = ccw.launch_in_tmux_blocking("/srv/repos/demo", "ccw-demo", args, cfg, None, "u-vz")
        self.assertEqual(rc, 0)
        self.assertEqual(call.call_count, 2)
        first_cmd = call.call_args_list[0][0][0]
        second_cmd = call.call_args_list[1][0][0]
        self.assertIn("mkdir", first_cmd)
        self.assertIn("new-session", second_cmd)
        execvp.assert_not_called()

    def test_launch_in_tmux_blocking_aborts_on_prelaunch_failure(self) -> None:
        cfg = self.make_config()
        args = ccw.ParsedArgs(action="run", agent_args=[])
        with self._stub_socket_path():
            with mock.patch.object(ccw.subprocess, "call", side_effect=[7]) as call:
                rc = ccw.launch_in_tmux_blocking("/srv/repos/demo", "ccw-demo", args, cfg, None, "u-vz")
        self.assertEqual(rc, 7)
        call.assert_called_once()  # main cmd never ran

    def test_attach_session_cli_still_calls_execvp(self) -> None:
        cfg = self.make_config()
        target = self.make_session("ccw-demo", "/srv/repos/demo")
        with self._stub_socket_path():
            with mock.patch.object(ccw.os, "execvp") as execvp:
                ccw.attach_session(target, cfg, "u-vz")
        execvp.assert_called_once()
        argv = execvp.call_args[0][1]
        self.assertIn("attach-session", argv)
        self.assertIn("ccw-demo", argv)

    def test_launch_in_tmux_cli_still_calls_execvp_after_mkdir(self) -> None:
        cfg = self.make_config()
        args = ccw.ParsedArgs(action="run", agent_args=[])
        with self._stub_socket_path():
            with mock.patch.object(ccw, "run_cmd") as run_cmd:
                with mock.patch.object(ccw.os, "execvp") as execvp:
                    ccw.launch_in_tmux("/srv/repos/demo", "ccw-demo", args, cfg, None, "u-vz")
        run_cmd.assert_called_once()
        execvp.assert_called_once()

    def test_plan_tui_run_returns_launch_request_without_execvp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self.make_config(allowed_roots=[tmpdir])
            project_dir = Path(tmpdir) / "demo"
            project_dir.mkdir()
            with self._stub_socket_path():
                with mock.patch.object(ccw, "probe_cwd_writable", return_value=True):
                    with mock.patch.object(ccw, "collect_sessions", return_value=[]):
                        with mock.patch.object(ccw, "allocate_session_name", return_value="ccw-demo"):
                            with mock.patch.object(ccw.os, "execvp") as execvp:
                                req = ccw._plan_tui_run(cfg, "u-vz", str(project_dir), dsp=False)
        self.assertIn("new-session", req.cmd)
        self.assertIn("ccw-demo", req.cmd)
        execvp.assert_not_called()

    def test_plan_tui_create_new_forces_attach_when_existing_session(self) -> None:
        cfg = self.make_config(allowed_roots=["/srv/repos"], new_project_root="/srv/repos")
        existing = [self.make_session("ccw-demo@claude", "/srv/repos/demo")]
        with self._stub_socket_path():
            with mock.patch.object(ccw, "canonical", side_effect=lambda v: str(v)):
                with mock.patch.object(ccw, "run_cmd"):
                    with mock.patch.object(ccw, "collect_sessions", return_value=existing):
                        with mock.patch.object(ccw.os, "execvp") as execvp:
                            req = ccw._plan_tui_create_new(cfg, "u-vz", "demo", dsp=False, git_profile="")
        # Existing session → attach request, not launch
        self.assertIn("attach-session", req.cmd)
        self.assertIn("ccw-demo@claude", req.cmd)
        execvp.assert_not_called()

    def test_plan_tui_open_existing_forces_attach_when_existing_session(self) -> None:
        """Open-existing uses the same attach-on-compatible-session path as
        create-new, minus any git side effects."""
        cfg = self.make_config(allowed_roots=["/srv/repos"], new_project_root="/srv/repos")
        existing = [self.make_session("ccw-demo@claude", "/srv/repos/demo")]
        with self._stub_socket_path():
            with mock.patch.object(ccw, "canonical", side_effect=lambda v: str(v)):
                with mock.patch.object(ccw, "run_cmd"):
                    with mock.patch.object(ccw, "collect_sessions", return_value=existing):
                        with mock.patch.object(ccw, "_do_create_git_remote") as git_create:
                            req = ccw._plan_tui_open_existing(cfg, "u-vz", "demo", dsp=False)
        self.assertIn("attach-session", req.cmd)
        self.assertIn("ccw-demo@claude", req.cmd)
        git_create.assert_not_called()

    def test_find_project_config_ignores_permission_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            allowed = [str(root)]
            target = root / "a" / "b"
            target.mkdir(parents=True)

            def fake_exists(self: Path) -> bool:
                if self == root / "a" / ".ccw.toml":
                    raise PermissionError("denied")
                return False

            with mock.patch.object(Path, "exists", fake_exists):
                self.assertIsNone(ccw.find_project_config(str(target), allowed))

    # ── launch_allowed_roots (user home is implicit) ─────────────────

    def test_launch_allowed_roots_adds_user_home(self) -> None:
        cfg = self.make_config(allowed_roots=["/srv/repos"])
        fake_pw = mock.Mock(pw_dir="/home/u-ed")
        with mock.patch.object(ccw.pwd, "getpwnam", return_value=fake_pw):
            with mock.patch.object(ccw, "canonical", side_effect=lambda v: str(v)):
                roots = ccw.launch_allowed_roots(cfg, "u-ed")
        self.assertEqual(roots, ["/srv/repos", "/home/u-ed"])

    def test_launch_allowed_roots_does_not_duplicate_existing_entry(self) -> None:
        cfg = self.make_config(allowed_roots=["/srv/repos", "/home/u-ed"])
        fake_pw = mock.Mock(pw_dir="/home/u-ed")
        with mock.patch.object(ccw.pwd, "getpwnam", return_value=fake_pw):
            with mock.patch.object(ccw, "canonical", side_effect=lambda v: str(v)):
                roots = ccw.launch_allowed_roots(cfg, "u-ed")
        self.assertEqual(roots, ["/srv/repos", "/home/u-ed"])

    def test_launch_allowed_roots_falls_back_gracefully_when_user_missing(self) -> None:
        cfg = self.make_config(allowed_roots=["/srv/repos"])
        with mock.patch.object(ccw.pwd, "getpwnam", side_effect=KeyError("nope")):
            roots = ccw.launch_allowed_roots(cfg, "nosuchuser")
        self.assertEqual(roots, ["/srv/repos"])

    # ── TUI callback error surfacing (0.10.3) ────────────────────────

    def test_sanitize_callback_stderr_strips_ccw_prefix_and_list_indent(self) -> None:
        raw = (
            "ccw: directory must be under one of:\n"
            "ccw:   - /srv/repos\n"
            "ccw:   - /home/u-ed\n"
            "ccw: got: /tmp\n"
        )
        expected = (
            "directory must be under one of:\n"
            "  - /srv/repos\n"
            "  - /home/u-ed\n"
            "got: /tmp"
        )
        self.assertEqual(ccw._sanitize_callback_stderr(raw), expected)

    def test_sanitize_callback_stderr_passes_through_non_ccw_lines(self) -> None:
        raw = "random warning\nccw: the real error\n\n"
        self.assertEqual(
            ccw._sanitize_callback_stderr(raw),
            "random warning\nthe real error",
        )

    def test_list_existing_projects_returns_name_and_mtime(self) -> None:
        """Smoke test against a real temp dir — guards against regressions
        like mistaking Path objects for os.DirEntry (no ``.path`` attr)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "alpha").mkdir()
            (root / "beta").mkdir()
            (root / ".hidden").mkdir()  # dot-prefixed must be skipped
            (root / "not_a_dir.txt").write_text("x")
            entries = ccw._list_existing_projects(str(root))
        names = [n for n, _ in entries]
        self.assertEqual(names, ["alpha", "beta"])
        for _, mtime in entries:
            # Either HH:MM (today) or MM-DD. Never blank on a fresh mkdir.
            self.assertRegex(mtime, r"^\d\d[:-]\d\d$")

    def test_list_existing_projects_missing_root_returns_empty(self) -> None:
        self.assertEqual(
            ccw._list_existing_projects("/nonexistent/path/for/ccw/test"), []
        )

    def test_wrap_tui_callback_passes_return_value(self) -> None:
        class _Err(Exception):
            pass

            # pragma: no cover — marker only
        wrapped = ccw._wrap_tui_callback(lambda x, y: x + y, _Err)
        self.assertEqual(wrapped(2, 3), 5)

    def test_wrap_tui_callback_captures_fail_message(self) -> None:
        class _Err(Exception):
            pass

        def inner() -> None:
            ccw.fail("directory must be under one of:\n" "ccw:   - /srv/repos")

        wrapped = ccw._wrap_tui_callback(inner, _Err)
        with self.assertRaises(_Err) as cm:
            wrapped()
        # Leading "ccw: " prefix must be stripped; list indent normalised.
        self.assertIn("directory must be under one of:", str(cm.exception))
        self.assertIn("/srv/repos", str(cm.exception))
        self.assertNotIn("ccw:", str(cm.exception))

    def test_wrap_tui_callback_falls_back_to_exit_code_when_stderr_empty(self) -> None:
        class _Err(Exception):
            pass

        def inner() -> None:
            raise SystemExit(7)

        wrapped = ccw._wrap_tui_callback(inner, _Err)
        with self.assertRaises(_Err) as cm:
            wrapped()
        self.assertIn("7", str(cm.exception))

    # ── tmux nesting detection ───────────────────────────────────────

    def test_tmux_host_socket_returns_none_without_env(self) -> None:
        with mock.patch.dict(ccw.os.environ, {}, clear=True):
            self.assertIsNone(ccw.tmux_host_socket())

    def test_tmux_host_socket_parses_socket_from_tmux_env(self) -> None:
        env = {"TMUX": "/tmp/ccw-u-vz.sock,12345,0"}
        with mock.patch.dict(ccw.os.environ, env, clear=True):
            self.assertEqual(ccw.tmux_host_socket(), "/tmp/ccw-u-vz.sock")

    def test_tmux_nesting_mode_execvp_when_not_in_tmux(self) -> None:
        with mock.patch.object(ccw, "tmux_host_socket", return_value=None):
            self.assertEqual(ccw.tmux_nesting_mode("/tmp/ccw-u-vz.sock"), "execvp")

    def test_tmux_nesting_mode_switch_when_same_socket(self) -> None:
        with mock.patch.object(ccw, "tmux_host_socket", return_value="/tmp/ccw-u-vz.sock"):
            self.assertEqual(ccw.tmux_nesting_mode("/tmp/ccw-u-vz.sock"), "switch")

    def test_tmux_nesting_mode_fails_when_foreign_socket(self) -> None:
        with mock.patch.object(ccw, "tmux_host_socket", return_value="/tmp/tmux-1000/default"):
            with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                with self.assertRaises(SystemExit) as ctx:
                    ccw.tmux_nesting_mode("/tmp/ccw-u-vz.sock")
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertIn("different socket", stderr.getvalue())

    def test_build_tmux_attach_request_uses_switch_client_when_nested(self) -> None:
        cfg = self.make_config()
        target = self.make_session("ccw-demo", "/srv/repos/demo")
        stubs = _StubsChain(
            mock.patch.object(ccw, "tmux_socket_path", return_value="/tmp/ccw-test.sock"),
            mock.patch.object(ccw, "tmux_host_socket", return_value="/tmp/ccw-test.sock"),
        )
        with stubs:
            req = ccw._build_tmux_attach_request(target, cfg, "u-vz")
        self.assertIn("switch-client", req.cmd)
        self.assertNotIn("attach-session", req.cmd)
        self.assertIn("ccw-demo", req.cmd)
        self.assertEqual(req.prelaunch, ())
        self.assertIn("switch-client", req.label)

    def test_build_tmux_launch_request_uses_switch_client_when_nested(self) -> None:
        cfg = self.make_config(agent_default_args={"claude": ("--model", "sonnet"), "codex": (), "cursor": ()})
        args = ccw.ParsedArgs(action="run", permission_mode="yolo", agent_args=["--foo"])
        stubs = _StubsChain(
            mock.patch.object(ccw, "tmux_socket_path", return_value="/tmp/ccw-test.sock"),
            mock.patch.object(ccw, "tmux_host_socket", return_value="/tmp/ccw-test.sock"),
        )
        with stubs:
            req = ccw._build_tmux_launch_request(
                "/srv/repos/demo", "ccw-demo@claude", args, cfg, None, "u-vz"
            )
        # Main cmd is the switch; creation happens in prelaunch.
        self.assertIn("switch-client", req.cmd)
        self.assertIn("ccw-demo@claude", req.cmd)
        # Two prelaunches: mkdir + detached create-or-noop with claude args.
        self.assertEqual(len(req.prelaunch), 2)
        mkdir_pre, create_pre = req.prelaunch
        self.assertIn("mkdir", mkdir_pre)
        self.assertIn("new-session", create_pre)
        self.assertIn("-dA", create_pre)
        self.assertIn("ccw-demo@claude", create_pre)
        self.assertIn("claude", create_pre)
        self.assertIn("--dangerously-skip-permissions", create_pre)
        self.assertIn("--foo", create_pre)
        self.assertIn("--model", create_pre)
        self.assertIn("sonnet", create_pre)
        self.assertIn("nested", req.label)

    # ── Task 5: --agent / --auto / permission_mode ───────────────────

    def test_parse_dsp_and_auto_mutually_exclusive(self) -> None:
        with self.assertRaises(SystemExit):
            ccw.parse_run_like(["--dsp", "--auto"], "run")
        with self.assertRaises(SystemExit):
            ccw.parse_run_like(["--auto", "--dsp"], "run")

    def test_parse_agent_flag(self) -> None:
        p = ccw.parse_run_like(["--agent", "codex", "--dsp"], "run")
        self.assertEqual(p.agent, "codex")
        self.assertEqual(p.permission_mode, "yolo")

    def test_parse_unknown_flag_goes_to_agent_args(self) -> None:
        p = ccw.parse_run_like(["--some-claude-flag", "x"], "run")
        self.assertEqual(p.agent_args, ["--some-claude-flag", "x"])

    def test_launch_builder_cursor_yolo(self) -> None:
        cfg = self.make_config(enabled_agents=("cursor",), default_agent="cursor")
        args = ccw.ParsedArgs(action="run", permission_mode="yolo")
        with self._stub_socket_path():
            req = ccw._build_tmux_launch_request("/tmp/x", "ccw-x@cursor", args, cfg, None, "u-vz")
        self.assertIn("cursor-agent", req.cmd)
        self.assertIn("--yolo", req.cmd)

    def test_launch_builder_cursor_auto_errors(self) -> None:
        cfg = self.make_config(enabled_agents=("cursor",), default_agent="cursor")
        args = ccw.ParsedArgs(action="run", permission_mode="auto", agent="cursor")
        with self._stub_socket_path():
            with self.assertRaises(SystemExit):
                ccw._build_tmux_launch_request("/tmp/x", "ccw-x@cursor", args, cfg, None, "u-vz")

    def test_launch_builder_codex_full_auto(self) -> None:
        cfg = self.make_config(enabled_agents=("codex",), default_agent="codex")
        args = ccw.ParsedArgs(action="run", permission_mode="auto", agent="codex")
        with self._stub_socket_path():
            req = ccw._build_tmux_launch_request("/tmp/x", "ccw-x@codex", args, cfg, None, "u-vz")
        self.assertIn("--full-auto", req.cmd)

    def test_launch_builder_worktree_rejected_for_non_claude(self) -> None:
        cfg = self.make_config(enabled_agents=("codex",), default_agent="codex")
        args = ccw.ParsedArgs(action="run", agent="codex")
        with self._stub_socket_path():
            with self.assertRaises(SystemExit):
                ccw._build_tmux_launch_request("/tmp/x", "ccw-x@codex", args, cfg, branch="b", launch_user="u-vz")

    def test_auto_with_cursor_default_fails_at_launch(self) -> None:
        # Full-stack check: parser accepts --auto without knowing the resolved agent;
        # launch builder must reject it when the resolved agent has no auto mode.
        cfg = self.make_config(enabled_agents=("cursor",), default_agent="cursor")
        parsed = ccw.parse_run_like(["--auto"], "run")
        self.assertEqual(parsed.permission_mode, "auto")
        self.assertIsNone(parsed.agent)  # not explicitly set
        with self._stub_socket_path():
            with self.assertRaises(SystemExit):
                ccw._build_tmux_launch_request("/tmp/x", "ccw-x@cursor", parsed, cfg, None, "u-vz")


def _mk_session(name: str, path: str = "/srv/repos/x", agent: str = "claude", legacy: bool = False) -> ccw.SessionInfo:
    return ccw.SessionInfo(
        user="u",
        name=name,
        attached="0",
        windows="1",
        created="-",
        last_attached="-",
        pane_pids=(),
        active_pid=None,
        active_cmd="",
        active_path=path,
        agent=agent,
        legacy=legacy,
    )


class SessionNamingTests(unittest.TestCase):
    """Tests for the new ccw-<stem>@<agent> session naming scheme."""

    def test_parse_session_name_new(self) -> None:
        self.assertEqual(ccw.parse_session_name("ccw-foo@codex"), ("foo", "codex", 1, False))
        self.assertEqual(ccw.parse_session_name("ccw-foo@codex-3"), ("foo", "codex", 3, False))
        self.assertEqual(ccw.parse_session_name("ccw-my-repo-branch@claude"), ("my-repo-branch", "claude", 1, False))

    def test_parse_session_name_legacy(self) -> None:
        self.assertEqual(ccw.parse_session_name("cc-foo"), ("foo", "claude", 1, True))
        self.assertEqual(ccw.parse_session_name("cc-foo-2"), ("foo", "claude", 2, True))

    def test_parse_session_name_rejects_garbage(self) -> None:
        self.assertIsNone(ccw.parse_session_name("random-x"))
        self.assertIsNone(ccw.parse_session_name("ccw-foo"))  # missing @agent

    def test_candidate_session_name(self) -> None:
        self.assertEqual(ccw.candidate_session_name("foo", 1, "cursor"), "ccw-foo@cursor")
        self.assertEqual(ccw.candidate_session_name("foo", 2, "cursor"), "ccw-foo@cursor-2")

    def test_compatible_indexed_sessions_agent_specific(self) -> None:
        # Two sessions same stem different agents are NOT siblings.
        compat_root = "/srv/repos/foo"
        s_claude = _mk_session("ccw-foo@claude", compat_root, agent="claude")
        s_codex = _mk_session("ccw-foo@codex", compat_root, agent="codex")
        matches = ccw.compatible_indexed_sessions("foo", "claude", compat_root, [s_claude, s_codex])
        self.assertEqual([m.name for m in matches], ["ccw-foo@claude"])

    def test_resolve_full_new(self) -> None:
        sessions = [_mk_session("ccw-foo@claude"), _mk_session("ccw-foo@codex", agent="codex")]
        self.assertEqual(
            ccw.resolve_session("ccw-foo@codex", sessions, "ccw-").name,
            "ccw-foo@codex",
        )

    def test_resolve_suffixed_without_prefix(self) -> None:
        sessions = [_mk_session("ccw-foo@codex", agent="codex")]
        self.assertEqual(
            ccw.resolve_session("foo@codex", sessions, "ccw-").name,
            "ccw-foo@codex",
        )

    def test_resolve_stem_unique(self) -> None:
        sessions = [_mk_session("ccw-foo@codex", agent="codex")]
        self.assertEqual(
            ccw.resolve_session("foo", sessions, "ccw-").name,
            "ccw-foo@codex",
        )

    def test_resolve_stem_ambiguous(self) -> None:
        sessions = [_mk_session("ccw-foo@claude"), _mk_session("ccw-foo@codex", agent="codex")]
        with self.assertRaises(SystemExit):
            ccw.resolve_session("foo", sessions, "ccw-")

    def test_resolve_legacy(self) -> None:
        sessions = [_mk_session("cc-foo", legacy=True)]
        self.assertEqual(ccw.resolve_session("cc-foo", sessions, "ccw-").name, "cc-foo")
        self.assertEqual(ccw.resolve_session("foo", sessions, "ccw-").name, "cc-foo")


class DoInteractiveTextualMissingTests(unittest.TestCase):
    """With textual unavailable, ``ccw`` (interactive) must print a single
    install hint on stderr, no traceback, and return 1."""

    def test_prints_install_hint_when_textual_missing(self) -> None:
        # Force `import ccw_tui` to raise ImportError even though lib/ is
        # on sys.path.
        saved_ccw_tui = sys.modules.get("ccw_tui")
        sys.modules["ccw_tui"] = None  # type: ignore[assignment]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfg = ccw.load_config(tmp)
                buf_err = io.StringIO()
                buf_out = io.StringIO()
                with mock.patch.object(sys, "stderr", buf_err), \
                     mock.patch.object(sys, "stdout", buf_out):
                    rc = ccw.do_interactive(cfg, "nobody")
                self.assertEqual(rc, 1)
                err_text = buf_err.getvalue()
                self.assertIn("requires", err_text)
                self.assertIn("textual", err_text)
                self.assertNotIn("Traceback", err_text)
        finally:
            if saved_ccw_tui is None:
                sys.modules.pop("ccw_tui", None)
            else:
                sys.modules["ccw_tui"] = saved_ccw_tui


if __name__ == "__main__":
    unittest.main()

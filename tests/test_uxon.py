import io
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import uxon.cli as uxon

UXON_PATH = Path(uxon.__file__).resolve()


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


class UxonTests(unittest.TestCase):
    def setUp(self) -> None:
        # ``ensure_new_project_target_allowed`` (introduced when the
        # ``allowed_roots`` semantics were unified) calls
        # ``probe_cwd_writable`` on the parent of every new project
        # path. The fixtures use placeholder paths like ``/srv/repos``
        # that don't exist on CI/dev hosts, so default the probe to
        # True here. Tests that need to assert the unwritable path is
        # rejected wrap their own ``mock.patch.object(uxon,
        # "probe_cwd_writable", return_value=False)`` block — the
        # inner ``with`` overrides this default for its scope.
        patcher = mock.patch.object(uxon, "probe_cwd_writable", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_config(self, **overrides) -> uxon.Config:
        defaults = dict(
            runtime_user="",
            default_launch_mode="caller",
            enable_all_users_list=False,
            launch_user_by_caller={},
            session_users=[],
            allowed_roots=["/srv/repos"],
            session_prefix="uxon-",
            legacy_session_prefixes=(),
            enabled_agents=("claude",),
            default_agent="claude",
            agent_default_args={"claude": (), "codex": (), "cursor": ()},
            new_project_root="/srv/repos",
            repeat_noninteractive_mode="fail",
            tmux_socket_template="/tmp/uxon-{user}.sock",
            tui_refresh_interval_seconds=2.0,
            git_create_enabled=False,
            default_git_remote_profile="",
            git_remote_profiles=[],
        )
        defaults.update(overrides)
        return uxon.Config(**defaults)

    def make_session(
        self,
        name: str,
        path: str,
        *,
        attached: str = "0",
    ) -> uxon.SessionInfo:
        return uxon.SessionInfo(
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
        with mock.patch.object(uxon, "process_user", return_value="u-vz"):
            with mock.patch.dict(uxon.os.environ, {"SUDO_USER": "remdepl"}, clear=False):
                self.assertEqual(uxon.resolve_caller_user(), "u-vz")

    def test_parse_args_supports_version_flags(self) -> None:
        parsed_long = uxon.parse_args(["--version"])
        self.assertEqual(parsed_long.action, "version")

        parsed_short = uxon.parse_args(["-V"])
        self.assertEqual(parsed_short.action, "version")

        parsed_subcommand = uxon.parse_args(["version"])
        self.assertEqual(parsed_subcommand.action, "version")

    def test_parse_args_supports_doctor(self) -> None:
        parsed = uxon.parse_args(["doctor"])
        self.assertEqual(parsed.action, "doctor")

    def test_parse_args_supports_kill_all_force(self) -> None:
        parsed = uxon.parse_args(["kill-all", "--force"])
        self.assertEqual(parsed.action, "kill-all")
        self.assertTrue(parsed.force)

    def _make_config_explicit(self, **kw) -> uxon.Config:
        """Make a Config with explicit fields (no make_config helper)."""
        return uxon.Config(
            runtime_user=kw.get("runtime_user", ""),
            default_launch_mode=kw.get("default_launch_mode", "caller"),
            enable_all_users_list=kw.get("enable_all_users_list", False),
            launch_user_by_caller=kw.get("launch_user_by_caller", {}),
            session_users=kw.get("session_users", []),
            allowed_roots=kw.get("allowed_roots", ["/srv"]),
            session_prefix=kw.get("session_prefix", "uxon-"),
            legacy_session_prefixes=kw.get("legacy_session_prefixes", ()),
            enabled_agents=kw.get("enabled_agents", ("claude",)),
            default_agent=kw.get("default_agent", "claude"),
            agent_default_args=kw.get(
                "agent_default_args", {"claude": (), "codex": (), "cursor": ()}
            ),
            new_project_root=kw.get("new_project_root", "/srv/agentdev"),
            repeat_noninteractive_mode=kw.get("repeat_noninteractive_mode", "fail"),
            tmux_socket_template=kw.get("tmux_socket_template", "/tmp/uxon-{user}.sock"),
            tui_refresh_interval_seconds=kw.get("tui_refresh_interval_seconds", 2.0),
            git_create_enabled=kw.get("git_create_enabled", False),
            default_git_remote_profile=kw.get("default_git_remote_profile", ""),
            git_remote_profiles=kw.get("git_remote_profiles", []),
        )

    def test_resolve_launch_user_fixed_mode_uses_runtime_user(self) -> None:
        cfg = self._make_config_explicit(
            runtime_user="devagent", default_launch_mode="fixed", session_users=["devagent"]
        )
        self.assertEqual(uxon.resolve_launch_user(cfg, "remdepl"), "devagent")

    def test_resolve_launch_user_caller_mode_uses_caller(self) -> None:
        cfg = self._make_config_explicit(
            runtime_user="devagent",
            default_launch_mode="caller",
            session_users=["devagent", "remdepl"],
        )
        self.assertEqual(uxon.resolve_launch_user(cfg, "remdepl"), "remdepl")

    def test_resolve_launch_user_mapping_overrides_default(self) -> None:
        cfg = self._make_config_explicit(
            runtime_user="devagent",
            default_launch_mode="caller",
            enable_all_users_list=True,
            launch_user_by_caller={"remdepl": "devagent"},
            session_users=["devagent", "remdepl"],
        )
        self.assertEqual(uxon.resolve_launch_user(cfg, "remdepl"), "devagent")

    def test_resolve_all_session_users_keeps_current_user_present(self) -> None:
        cfg = self._make_config_explicit(
            runtime_user="devagent",
            default_launch_mode="fixed",
            enable_all_users_list=True,
            session_users=["devagent"],
        )
        self.assertEqual(uxon.resolve_all_session_users(cfg, "remdepl"), ["devagent", "remdepl"])

    def test_parse_args_supports_all_users_listing(self) -> None:
        parsed = uxon.parse_args(["list", "--all-users"])
        self.assertEqual(parsed.action, "list")
        self.assertTrue(parsed.all_users)

        parsed_short = uxon.parse_args(["-l", "--all-users"])
        self.assertEqual(parsed_short.action, "list")
        self.assertTrue(parsed_short.all_users)

    def test_parse_args_supports_repeat_mode_flags_for_new(self) -> None:
        parsed_attach = uxon.parse_args(["-n", "demo", "--attach-existing"])
        self.assertEqual(parsed_attach.action, "new")
        self.assertEqual(parsed_attach.repeat_mode, "attach")

        parsed_new = uxon.parse_args(["new", "demo", "--new-session"])
        self.assertEqual(parsed_new.action, "new")
        self.assertEqual(parsed_new.repeat_mode, "new")

    def _write_and_load_cfg(self, toml_content: str, tmpdir: str) -> uxon.Config:
        """Helper: write a config.toml in tmpdir and load_config from there."""
        tmp_path = Path(tmpdir)
        cwd = tmp_path / "workspace"
        cwd.mkdir(exist_ok=True)
        repo_cfg = tmp_path / "repo-config.toml"
        repo_cfg.write_text(toml_content, encoding="utf-8")

        def fake_load_toml(path: Path) -> dict[str, object]:
            if path == tmp_path / "config" / "config.toml":
                with repo_cfg.open("rb") as fh:
                    return uxon.tomllib.load(fh)
            return {}

        with mock.patch.object(uxon, "repo_root", return_value=tmp_path):
            with mock.patch.object(uxon, "find_project_config", return_value=None):
                with mock.patch.object(uxon, "canonical", side_effect=lambda v: str(v)):
                    with mock.patch.object(uxon, "load_toml", side_effect=fake_load_toml):
                        return uxon.load_config(str(cwd))

    def test_load_config_reads_new_multi_user_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._write_and_load_cfg(
                textwrap.dedent("""
                    runtime_user = "devagent"
                    default_launch_mode = "caller"
                    enable_all_users_list = true
                    session_users = ["devagent", "remdepl"]
                    allowed_roots = ["/srv", "/tmp"]
                    session_prefix = "uxon-"
                    repeat_noninteractive_mode = "attach"
                    tmux_socket_template = "/tmp/uxon-{user}-{uid}.sock"

                    [agents]
                    enabled = ["claude"]
                    default = "claude"

                    [agents.claude]
                    default_args = ["--model", "sonnet"]

                    [launch_user_by_caller]
                    remdepl = "devagent"
                """).strip()
                + "\n",
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
        self.assertEqual(cfg.tmux_socket_template, "/tmp/uxon-{user}-{uid}.sock")

    def test_load_config_reads_legacy_session_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._write_and_load_cfg(
                'session_prefix = "uxon-"\nlegacy_session_prefixes = ["ccw-", "cc-"]\n',
                tmpdir,
            )
        self.assertEqual(cfg.legacy_session_prefixes, ("ccw-", "cc-"))

    def test_load_config_legacy_session_prefixes_default_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._write_and_load_cfg("", tmpdir)
        self.assertEqual(cfg.legacy_session_prefixes, ())

    def test_load_config_legacy_session_prefixes_dedupes_active_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._write_and_load_cfg(
                'session_prefix = "uxon-"\nlegacy_session_prefixes = ["uxon-", "ccw-"]\n',
                tmpdir,
            )
        self.assertEqual(cfg.legacy_session_prefixes, ("ccw-",))

    def test_load_config_rejects_non_list_legacy_session_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit):
                self._write_and_load_cfg('legacy_session_prefixes = "ccw-"\n', tmpdir)

    def test_load_config_reads_tui_refresh_interval(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._write_and_load_cfg("tui_refresh_interval_seconds = 5\n", tmpdir)
        self.assertEqual(cfg.tui_refresh_interval_seconds, 5.0)

    def test_load_config_rejects_invalid_tui_refresh_interval(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit):
                self._write_and_load_cfg("tui_refresh_interval_seconds = 0\n", tmpdir)

    def test_ssh_control_persist_seconds_must_be_positive(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit) as cm:
                self._write_and_load_cfg("ssh_control_persist_seconds = 0\n", tmpdir)
        # ``fail()`` stashes the human-readable message on the
        # exception; assert against that rather than ``str(SystemExit)``
        # (which is just the rc).
        self.assertIn("ssh_control_persist_seconds", getattr(cm.exception, "uxon_msg", ""))

    def test_ssh_control_persist_seconds_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._write_and_load_cfg("", tmpdir)
        self.assertEqual(cfg.ssh_control_persist_seconds, 300)

    def test_load_config_tui_table_defaults_when_section_absent(self) -> None:
        # No ``[tui.table]`` block — defaults must hold and the columns
        # signal must be ``None`` (not ``()``), since ``None`` is the
        # contract that ``build_active_columns`` uses to mean
        # "fall back to the registry defaults".
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._write_and_load_cfg("", tmpdir)
        self.assertIsNone(cfg.tui_table_columns)

    def test_load_config_tui_table_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._write_and_load_cfg(
                textwrap.dedent("""
                    [tui.table]
                    columns         = ["name", "user", "cpu", "ram", "last"]
                """).strip()
                + "\n",
                tmpdir,
            )
        self.assertEqual(cfg.tui_table_columns, ("name", "user", "cpu", "ram", "last"))

    def test_load_config_tui_table_empty_columns_collapses_to_none(self) -> None:
        # Explicit empty list and absent key both signal "use registry
        # defaults"; we never expose an empty-tuple state.
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._write_and_load_cfg(
                textwrap.dedent("""
                    [tui.table]
                    columns = []
                """).strip()
                + "\n",
                tmpdir,
            )
        self.assertIsNone(cfg.tui_table_columns)

    def test_load_config_tui_table_rejects_non_list_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit):
                self._write_and_load_cfg(
                    textwrap.dedent("""
                        [tui.table]
                        columns = "name"
                    """).strip()
                    + "\n",
                    tmpdir,
                )

    def test_load_config_worktree_keys_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._write_and_load_cfg("", tmpdir)
        self.assertEqual(cfg.worktree_root, "")
        self.assertEqual(cfg.worktree_base, "local")

    def test_load_config_reads_worktree_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._write_and_load_cfg(
                'worktree_root = "/data/wt"\nworktree_base = "remote"\n', tmpdir
            )
        self.assertEqual(cfg.worktree_root, "/data/wt")
        self.assertEqual(cfg.worktree_base, "remote")

    def test_load_config_rejects_invalid_worktree_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit):
                self._write_and_load_cfg('worktree_base = "origin"\n', tmpdir)

    def test_load_config_tui_table_default_sort_by_ignored_with_debug_log(self) -> None:
        # ``tui.table.default_sort_by`` was removed in 3.4 — sort is
        # now a hard contract. Any value carried over from older
        # configs is silently ignored; the loader emits one
        # ``UXON_DEBUG=tui`` line so operators can spot the fossil.
        from uxon.tui import events as _events

        seen: list[tuple[str, dict]] = []

        def _spy(topic: str, **fields: object) -> None:
            seen.append((topic, dict(fields)))

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(_events, "debug", _spy):
                cfg = self._write_and_load_cfg(
                    textwrap.dedent("""
                        [tui.table]
                        default_sort_by = "ram"
                    """).strip()
                    + "\n",
                    tmpdir,
                )
        self.assertFalse(hasattr(cfg, "tui_table_default_sort_by"))
        self.assertEqual(len(seen), 1)
        topic, fields = seen[0]
        self.assertEqual(topic, "tui")
        self.assertEqual(fields.get("reason"), "ignored_default_sort_by")
        self.assertEqual(fields.get("id"), "ram")

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
                """).strip()
                + "\n",
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

    def test_load_config_reads_remote_hosts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._write_and_load_cfg(
                textwrap.dedent("""
                    [[remote_hosts]]
                    name = "vz-prod1"
                    ssh_alias = "vz-prod1"
                    description = "primary EU"

                    [[remote_hosts]]
                    name = "edge.eu"
                    ssh_alias = "edge-eu"
                    remote_uxon = "/opt/uxon/bin/uxon"
                """).strip()
                + "\n",
                tmpdir,
            )
        self.assertEqual([h.name for h in cfg.remote_hosts], ["vz-prod1", "edge.eu"])
        self.assertEqual(cfg.remote_hosts[0].description, "primary EU")
        self.assertEqual(cfg.remote_hosts[1].remote_uxon, "/opt/uxon/bin/uxon")

    def test_load_config_remote_hosts_default_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._write_and_load_cfg("", tmpdir)
        self.assertEqual(cfg.remote_hosts, [])

    def test_load_config_rejects_invalid_remote_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit):
                self._write_and_load_cfg(
                    textwrap.dedent("""
                        [[remote_hosts]]
                        name = "vz prod"
                        ssh_alias = "vz-prod"
                    """).strip()
                    + "\n",
                    tmpdir,
                )

    def test_skeleton_ctx_carries_main_ctx_rebuild_source(self) -> None:
        # MainScreen.on_mount fans out across ctx.refresh_sources only.
        # If the skeleton ctx ships an empty list the "Loading sessions…"
        # placeholder never gets replaced — the worker that produces the
        # real ctx is never spawned. Pin that the skeleton carries the
        # ``main_ctx_rebuild`` source so the initial fan-out kicks the
        # rebuild even before any periodic timer fires.
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._write_and_load_cfg(
                textwrap.dedent("""
                    [[remote_hosts]]
                    name = "peer1"
                    ssh_alias = "peer1"
                """).strip()
                + "\n",
                tmpdir,
            )
            ctx = uxon._build_tui_context(cfg, "devagent", tmpdir, skeleton=True)
        self.assertTrue(ctx.loading)
        names = [s.name for s in ctx.refresh_sources]
        self.assertIn("main_ctx_rebuild", names)
        self.assertIn("remote:peer1", names)

    def test_load_config_defaults_auto_mode(self) -> None:
        """No ``[agents]`` block → auto-mode: empty enabled / default."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._write_and_load_cfg("", tmpdir)
        self.assertEqual(cfg.enabled_agents, ())
        self.assertEqual(cfg.default_agent, "")
        self.assertEqual(cfg.agent_default_args["claude"], ())

    def test_load_config_empty_enabled_is_auto_mode(self) -> None:
        """``enabled = []`` is equivalent to absent — auto-mode."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._write_and_load_cfg(
                textwrap.dedent("""
                    [agents]
                    enabled = []
                """).strip()
                + "\n",
                tmpdir,
            )
        self.assertEqual(cfg.enabled_agents, ())
        self.assertEqual(cfg.default_agent, "")

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
                """).strip()
                + "\n",
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
        parsed = uxon.parse_subcommand(
            ["new", "demo", "--git-remote", "prof-a", "--git-visibility", "public"]
        )
        self.assertEqual(parsed.action, "new")
        self.assertEqual(parsed.target_id, "demo")
        self.assertEqual(parsed.git_remote, "prof-a")
        self.assertEqual(parsed.git_visibility, "public")
        self.assertFalse(parsed.no_git)

    def test_parse_new_no_git(self) -> None:
        parsed = uxon.parse_subcommand(["new", "demo", "--no-git"])
        self.assertTrue(parsed.no_git)
        self.assertIsNone(parsed.git_remote)

    def test_parse_new_git_remote_default(self) -> None:
        parsed = uxon.parse_subcommand(["new", "demo", "--git-remote", "default"])
        self.assertEqual(parsed.git_remote, "default")

    def test_parse_new_rejects_git_remote_with_no_git(self) -> None:
        with self.assertRaises(SystemExit):
            uxon.parse_subcommand(["new", "demo", "--git-remote", "p", "--no-git"])

    def test_parse_new_rejects_bad_visibility(self) -> None:
        with self.assertRaises(SystemExit):
            uxon.parse_subcommand(["new", "demo", "--git-visibility", "secret"])

    def test_do_new_git_remote_dry_run_invokes_orchestrator(self) -> None:
        profile = {
            "name": "prof-a",
            "host": "github.com",
            "owner": "vzd3v",
            "auth": "gh",
            "creds_user": "remdepl",
            "visibility": "private",
        }
        from uxon import git_profiles as uxon_git_profiles

        cfg = self.make_config(
            allowed_roots=["/srv/repos"],
            git_create_enabled=True,
            default_git_remote_profile="prof-a",
            git_remote_profiles=uxon_git_profiles.load_profiles([profile]),
        )
        args = uxon.ParsedArgs(
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
            from uxon import git_create as uxon_git_create

            return uxon_git_create.CreationResult(
                profile_name=profile_arg.name,
                ssh_url=f"git@github.com:vzd3v/{repo_name}.git",
                commands=["would run: git init"],
            )

        from uxon import git_create as uxon_git_create

        with mock.patch.object(uxon_git_create, "create_project_remote", side_effect=fake_create):
            with mock.patch.object(uxon, "collect_sessions", return_value=[]):
                with mock.patch.object(uxon, "launch_in_tmux", return_value=0):
                    with mock.patch.object(uxon, "is_interactive_tty", return_value=False):
                        uxon.do_new(args, cfg, "devagent")

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "prof-a")
        self.assertEqual(calls[0]["repo"], "demo")
        self.assertEqual(calls[0]["dir"], "/srv/repos/demo")
        self.assertTrue(calls[0]["dry_run"])
        self.assertEqual(calls[0]["launch_user"], "devagent")

    def test_do_new_git_remote_rejects_disabled_feature(self) -> None:
        cfg = self.make_config(git_create_enabled=False)
        args = uxon.ParsedArgs(
            action="new",
            target_id="demo",
            git_remote="default",
            dry_run=True,
            agent_args=[],
        )
        with mock.patch.object(uxon, "is_interactive_tty", return_value=False):
            with self.assertRaisesRegex(SystemExit, "2"):
                uxon.do_new(args, cfg, "devagent")

    def test_do_new_git_remote_with_worktree_fails(self) -> None:
        from uxon import git_profiles as uxon_git_profiles

        cfg = self.make_config(
            git_create_enabled=True,
            default_git_remote_profile="prof-a",
            git_remote_profiles=uxon_git_profiles.load_profiles(
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
        args = uxon.ParsedArgs(
            action="new",
            target_id="demo",
            worktree_branch="feature",
            git_remote="prof-a",
            dry_run=True,
            agent_args=[],
        )
        with mock.patch.object(uxon, "os", wraps=uxon.os) as m_os:
            m_os.path.isdir.return_value = True
            with mock.patch.object(uxon, "git_repo_root_as_user", return_value="/srv/repos/demo"):
                with self.assertRaises(SystemExit):
                    uxon.do_new(args, cfg, "devagent")

    def test_parse_run_rejects_git_flags(self) -> None:
        with self.assertRaises(SystemExit):
            uxon.parse_subcommand(["run", "--git-remote", "p"])
        with self.assertRaises(SystemExit):
            uxon.parse_subcommand(["run", "--no-git"])
        with self.assertRaises(SystemExit):
            uxon.parse_subcommand(["run", "--git-visibility", "private"])

    def test_format_version_reads_version_file_and_commit(self) -> None:
        with mock.patch.object(uxon, "read_repo_version", return_value="0.2.0"):
            with mock.patch.object(uxon, "read_git_commit_short", return_value="abc1234"):
                with mock.patch.object(uxon, "repo_is_dirty", return_value=False):
                    self.assertEqual(uxon.format_version(), "uxon 0.2.0 (abc1234)")

    def test_format_version_marks_dirty_checkout(self) -> None:
        with mock.patch.object(uxon, "read_repo_version", return_value="0.2.0"):
            with mock.patch.object(uxon, "read_git_commit_short", return_value="abc1234"):
                with mock.patch.object(uxon, "repo_is_dirty", return_value=True):
                    self.assertEqual(uxon.format_version(), "uxon 0.2.0 (abc1234-dirty)")

    def test_do_new_allows_call_from_outside_allowed_roots(self) -> None:
        cfg = self.make_config()
        args = uxon.ParsedArgs(action="new", target_id="demo", dry_run=True, agent_args=[])

        with mock.patch.object(uxon.os, "getcwd", return_value="/home/u-vz"):
            with mock.patch.object(uxon, "canonical", side_effect=lambda value: str(value)):
                with mock.patch.object(uxon, "collect_sessions", return_value=[]):
                    with mock.patch.object(uxon, "allocate_session_name", return_value="uxon-demo"):
                        with mock.patch.object(uxon, "launch_in_tmux", return_value=0) as launch:
                            result = uxon.do_new(args, cfg, "u-vz")

        self.assertEqual(result, 0)
        launch.assert_called_once()

    def test_do_new_existing_session_defaults_to_attach_in_tty(self) -> None:
        cfg = self.make_config()
        args = uxon.ParsedArgs(action="new", target_id="demo", agent_args=[])
        existing = [self.make_session("uxon-demo@claude", "/srv/repos/demo")]

        with mock.patch.object(uxon, "canonical", side_effect=lambda value: str(value)):
            with mock.patch.object(uxon, "run_cmd") as run_cmd:
                with mock.patch.object(uxon, "collect_sessions", return_value=existing):
                    with mock.patch.object(uxon, "is_interactive_tty", return_value=True):
                        with mock.patch("builtins.input", return_value=""):
                            with mock.patch.object(
                                uxon, "attach_session", return_value=0
                            ) as attach:
                                with mock.patch.object(
                                    uxon, "launch_in_tmux", return_value=0
                                ) as launch:
                                    result = uxon.do_new(args, cfg, "u-vz")

        self.assertEqual(result, 0)
        run_cmd.assert_called_once()
        attach.assert_called_once()
        launch.assert_not_called()

    def test_do_new_existing_session_force_new_bypasses_prompt(self) -> None:
        cfg = self.make_config()
        args = uxon.ParsedArgs(action="new", target_id="demo", repeat_mode="new", agent_args=[])
        existing = [self.make_session("uxon-demo@claude", "/srv/repos/demo")]

        with mock.patch.object(uxon, "canonical", side_effect=lambda value: str(value)):
            with mock.patch.object(uxon, "run_cmd") as run_cmd:
                with mock.patch.object(uxon, "collect_sessions", return_value=existing):
                    with mock.patch.object(
                        uxon, "allocate_session_name", return_value="uxon-demo-2"
                    ) as allocate:
                        with mock.patch.object(uxon, "launch_in_tmux", return_value=0) as launch:
                            result = uxon.do_new(args, cfg, "u-vz")

        self.assertEqual(result, 0)
        run_cmd.assert_called_once()
        allocate.assert_called_once()
        launch.assert_called_once()

    def test_do_new_existing_session_without_tty_fails_with_guidance(self) -> None:
        cfg = self.make_config()
        args = uxon.ParsedArgs(action="new", target_id="demo", agent_args=[])
        existing = [self.make_session("uxon-demo@claude", "/srv/repos/demo")]

        with mock.patch.object(uxon, "canonical", side_effect=lambda value: str(value)):
            with mock.patch.object(uxon, "run_cmd") as run_cmd:
                with mock.patch.object(uxon, "collect_sessions", return_value=existing):
                    with mock.patch.object(uxon, "is_interactive_tty", return_value=False):
                        with mock.patch.object(uxon, "eprint") as eprint:
                            with self.assertRaises(SystemExit) as ctx:
                                uxon.do_new(args, cfg, "u-vz")

        self.assertEqual(ctx.exception.code, 2)
        run_cmd.assert_called_once()
        eprint.assert_called()
        self.assertIn("--attach-existing", eprint.call_args[0][0])
        self.assertIn("--new-session", eprint.call_args[0][0])

    def test_do_new_existing_worktree_session_defaults_to_attach_in_tty(self) -> None:
        # uxon-managed worktree sessions live at the worktree path (§2.5),
        # so the compatible session's active_path is the worktree dir, not
        # the repo root. The attach-vs-new decision itself is unchanged.
        cfg = self.make_config()
        args = uxon.ParsedArgs(
            action="new", target_id="demo", worktree_branch="feature-x", agent_args=[]
        )
        wt = "/srv/repos/demo/.uxon/worktrees/feature-x"
        existing = [self.make_session("uxon-demo-feature-x@claude", wt)]

        with (
            mock.patch.object(uxon.os.path, "isdir", return_value=True),
            mock.patch.object(uxon, "probe_cwd_writable", return_value=True),
            mock.patch.object(uxon, "git_repo_root_as_user", return_value="/srv/repos/demo"),
            mock.patch.object(uxon, "git_common_dir_root_as_user", return_value="/srv/repos/demo"),
            mock.patch.object(uxon, "collect_sessions", return_value=existing),
            mock.patch.object(uxon, "is_interactive_tty", return_value=True),
            mock.patch("builtins.input", return_value=""),
            mock.patch.object(uxon, "attach_session", return_value=0) as attach,
            mock.patch.object(uxon, "plan_worktree_launch") as plan,
        ):
            result = uxon.do_new(args, cfg, "u-vz")

        self.assertEqual(result, 0)
        attach.assert_called_once()
        plan.assert_not_called()  # attach decision → no worktree creation

    def test_do_new_existing_worktree_session_uses_configured_noninteractive_new(self) -> None:
        # Same §2.5 worktree-path compatibility root; with the noninteractive
        # mode forced to "new", the planner is invoked (creation now lives
        # inside plan_worktree_launch, not the old allocate + launch_in_tmux).
        cfg = self.make_config()
        cfg.repeat_noninteractive_mode = "new"
        args = uxon.ParsedArgs(
            action="new", target_id="demo", worktree_branch="feature-x", agent_args=[]
        )
        wt = "/srv/repos/demo/.uxon/worktrees/feature-x"
        existing = [self.make_session("uxon-demo-feature-x@claude", wt)]
        fake_req = uxon._tui_launch_request_cls()(cmd=("true",), label="launch x")

        with (
            mock.patch.object(uxon.os.path, "isdir", return_value=True),
            mock.patch.object(uxon, "probe_cwd_writable", return_value=True),
            mock.patch.object(uxon, "git_repo_root_as_user", return_value="/srv/repos/demo"),
            mock.patch.object(uxon, "git_common_dir_root_as_user", return_value="/srv/repos/demo"),
            mock.patch.object(uxon, "collect_sessions", return_value=existing),
            mock.patch.object(uxon, "is_interactive_tty", return_value=False),
            mock.patch.object(uxon, "plan_worktree_launch", return_value=fake_req) as plan,
            mock.patch.object(uxon, "run_cmd"),
            mock.patch.object(uxon.os, "execvp", return_value=None) as execvp,
        ):
            result = uxon.do_new(args, cfg, "u-vz")

        self.assertEqual(result, 0)
        plan.assert_called_once()
        execvp.assert_called_once()

    def test_do_new_legacy_socket_guardrail_fails(self) -> None:
        cfg = self.make_config()
        args = uxon.ParsedArgs(action="new", target_id="demo", agent_args=[])
        legacy = [self.make_session("uxon-demo@claude", "/srv/repos/demo")]

        with mock.patch.object(uxon, "canonical", side_effect=lambda value: str(value)):
            with mock.patch.object(uxon, "run_cmd"):
                with mock.patch.object(uxon, "collect_sessions", return_value=[]):
                    with mock.patch.object(uxon, "collect_sessions_for_user", return_value=legacy):
                        with mock.patch.object(
                            uxon, "tmux_socket_path", return_value="/tmp/uxon-u-vz.sock"
                        ):
                            with mock.patch.object(uxon, "eprint") as eprint:
                                with self.assertRaises(SystemExit) as ctx:
                                    uxon.do_new(args, cfg, "u-vz")

        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("legacy default tmux socket", eprint.call_args[0][0])

    def test_resolve_repeat_decision_prefers_env_override(self) -> None:
        cfg = self.make_config()
        cfg.repeat_noninteractive_mode = "fail"
        session = self.make_session("uxon-demo", "/srv/repos/demo")

        with mock.patch.object(uxon, "is_interactive_tty", return_value=False):
            with mock.patch.dict(
                uxon.os.environ, {"UXON_REPEAT_NONINTERACTIVE_POLICY": "attach"}, clear=False
            ):
                decision = uxon.resolve_repeat_decision(
                    "none" if False else None, cfg, "/srv/repos/demo", session, [session]
                )

        self.assertEqual(decision, "attach")

    def test_tmux_socket_path_expands_template(self) -> None:
        cfg = self.make_config()
        cfg.tmux_socket_template = "/tmp/uxon-{user}-{uid}.sock"

        with mock.patch.object(uxon.pwd, "getpwnam") as getpwnam:
            getpwnam.return_value = mock.Mock(pw_uid=1001)
            path = uxon.tmux_socket_path(cfg, "u-vz")

        self.assertEqual(path, "/tmp/uxon-u-vz-1001.sock")

    def test_doctor_reports_socket_and_config(self) -> None:
        from uxon import agents as uxon_agents

        cfg = self.make_config()
        output = io.StringIO()
        ok_avail = uxon_agents.AgentAvailability(status="ok", version="1.2.3")

        from uxon import probes as uxon_probes

        host_report = uxon_probes.HostReport(
            tmux=uxon_probes.BinaryStatus("tmux", "/usr/bin/tmux", "apt"),
            agents={
                "claude": uxon_probes.BinaryStatus("claude", "/usr/local/bin/claude", "npm"),
                "codex": uxon_probes.BinaryStatus("codex", None, ""),
                "cursor": uxon_probes.BinaryStatus("cursor-agent", None, ""),
            },
            launch_user="u-vz",
        )

        with mock.patch.object(
            uxon,
            "resolve_config_layers",
            return_value=({}, [Path("/srv/apps/uxon/config/config.toml")]),
        ):
            with mock.patch.object(uxon, "tmux_socket_path", return_value="/tmp/uxon-u-vz.sock"):
                with mock.patch("uxon.probes.probe_host", return_value=host_report):
                    with mock.patch.object(uxon_agents, "_probe_one", return_value=ok_avail):
                        with mock.patch.object(
                            uxon,
                            "collect_sessions",
                            return_value=[self.make_session("uxon-demo@claude", "/srv/repos/demo")],
                        ):
                            with mock.patch.object(
                                uxon, "collect_sessions_for_user", return_value=[]
                            ):
                                with mock.patch.object(
                                    uxon, "user_can_write_dir", return_value=True
                                ):
                                    with mock.patch.object(
                                        uxon, "format_version", return_value="uxon 0.4.0 (abc1234)"
                                    ):
                                        with mock.patch("sys.stdout", output):
                                            rc = uxon.do_doctor(
                                                cfg, "remdepl", "u-vz", "/srv/repos/demo"
                                            )

        self.assertEqual(rc, 0)
        rendered = output.getvalue()
        self.assertIn("uxon doctor", rendered)
        self.assertIn("config_paths=/srv/apps/uxon/config/config.toml", rendered)
        self.assertIn("tmux_socket=/tmp/uxon-u-vz.sock", rendered)
        self.assertIn("claude:", rendered)
        self.assertIn("ok (1.2.3)", rendered)

    def test_doctor_reports_missing_agent(self) -> None:
        from uxon import probes as uxon_probes

        cfg = self.make_config()
        output = io.StringIO()
        host_report = uxon_probes.HostReport(
            tmux=uxon_probes.BinaryStatus("tmux", "/usr/bin/tmux", "apt"),
            agents={
                "claude": uxon_probes.BinaryStatus("claude", None, "npm install ..."),
                "codex": uxon_probes.BinaryStatus("codex", None, ""),
                "cursor": uxon_probes.BinaryStatus("cursor-agent", None, ""),
            },
            launch_user="u-vz",
        )

        with mock.patch.object(uxon, "resolve_config_layers", return_value=({}, [])):
            with mock.patch.object(uxon, "tmux_socket_path", return_value="/tmp/uxon-u-vz.sock"):
                with mock.patch("uxon.probes.probe_host", return_value=host_report):
                    with mock.patch.object(uxon, "collect_sessions", return_value=[]):
                        with mock.patch.object(uxon, "collect_sessions_for_user", return_value=[]):
                            with mock.patch.object(uxon, "user_can_write_dir", return_value=True):
                                with mock.patch.object(
                                    uxon, "format_version", return_value="uxon 0.4.0"
                                ):
                                    with mock.patch("sys.stdout", output):
                                        rc = uxon.do_doctor(cfg, "u-vz", "u-vz", "/srv/repos/demo")

        rendered = output.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("MISSING", rendered)
        self.assertIn("claude:", rendered)

    def test_do_kill_all_requires_force_without_tty(self) -> None:
        cfg = self.make_config()
        args = uxon.ParsedArgs(action="kill-all", force=False)
        sessions = [self.make_session("uxon-demo", "/srv/repos/demo")]

        with mock.patch.object(uxon, "collect_sessions", return_value=sessions):
            with mock.patch.object(uxon, "is_interactive_tty", return_value=False):
                with mock.patch.object(uxon, "eprint") as eprint:
                    with self.assertRaises(SystemExit) as ctx:
                        uxon.do_kill_all(args, cfg, "u-vz")

        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("--force", eprint.call_args[0][0])

    def _stub_socket_path(self):
        # Classic tests run as if the process is NOT inside tmux so the
        # build_request helpers stay on the execvp / attach-session /
        # new-session path.
        return _StubsChain(
            mock.patch.object(uxon, "tmux_socket_path", return_value="/tmp/uxon-test.sock"),
            mock.patch.object(uxon, "tmux_host_socket", return_value=None),
        )

    def test_build_tmux_attach_request_produces_expected_argv(self) -> None:
        cfg = self.make_config()
        target = self.make_session("uxon-demo", "/srv/repos/demo")
        with self._stub_socket_path():
            req = uxon._build_tmux_attach_request(target, cfg, "u-vz")
        self.assertIn("attach-session", req.cmd)
        self.assertIn("uxon-demo", req.cmd)
        self.assertEqual(req.prelaunch, ())
        self.assertIn("attach", req.label)

    def test_build_tmux_launch_request_includes_claude_and_mkdir(self) -> None:
        cfg = self.make_config(
            agent_default_args={"claude": ("--model", "sonnet"), "codex": (), "cursor": ()}
        )
        args = uxon.ParsedArgs(action="run", permission_mode="yolo", agent_args=["--foo"])
        with self._stub_socket_path():
            req = uxon._build_tmux_launch_request(
                "/srv/repos/demo", "uxon-demo@claude", args, cfg, None, "u-vz"
            )
        self.assertIn("new-session", req.cmd)
        self.assertIn("-As", req.cmd)
        self.assertIn("uxon-demo@claude", req.cmd)
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
        target = self.make_session("uxon-demo", "/srv/repos/demo")
        with self._stub_socket_path():
            with mock.patch.object(uxon.subprocess, "call", return_value=0) as call:
                with mock.patch.object(uxon.os, "execvp") as execvp:
                    rc = uxon.attach_session_blocking(target, cfg, "u-vz")
        self.assertEqual(rc, 0)
        call.assert_called_once()
        execvp.assert_not_called()

    def test_launch_in_tmux_blocking_runs_prelaunch_then_cmd(self) -> None:
        cfg = self.make_config()
        args = uxon.ParsedArgs(action="run", agent_args=[])
        with self._stub_socket_path():
            with mock.patch.object(uxon.subprocess, "call", side_effect=[0, 0]) as call:
                with mock.patch.object(uxon.os, "execvp") as execvp:
                    rc = uxon.launch_in_tmux_blocking(
                        "/srv/repos/demo", "uxon-demo", args, cfg, None, "u-vz"
                    )
        self.assertEqual(rc, 0)
        self.assertEqual(call.call_count, 2)
        first_cmd = call.call_args_list[0][0][0]
        second_cmd = call.call_args_list[1][0][0]
        self.assertIn("mkdir", first_cmd)
        self.assertIn("new-session", second_cmd)
        execvp.assert_not_called()

    def test_launch_in_tmux_blocking_aborts_on_prelaunch_failure(self) -> None:
        cfg = self.make_config()
        args = uxon.ParsedArgs(action="run", agent_args=[])
        with self._stub_socket_path():
            with mock.patch.object(uxon.subprocess, "call", side_effect=[7]) as call:
                rc = uxon.launch_in_tmux_blocking(
                    "/srv/repos/demo", "uxon-demo", args, cfg, None, "u-vz"
                )
        self.assertEqual(rc, 7)
        call.assert_called_once()  # main cmd never ran

    def test_attach_session_cli_still_calls_execvp(self) -> None:
        cfg = self.make_config()
        target = self.make_session("uxon-demo", "/srv/repos/demo")
        with self._stub_socket_path():
            with mock.patch.object(uxon.os, "execvp") as execvp:
                uxon.attach_session(target, cfg, "u-vz")
        execvp.assert_called_once()
        argv = execvp.call_args[0][1]
        self.assertIn("attach-session", argv)
        self.assertIn("uxon-demo", argv)

    def test_launch_in_tmux_cli_still_calls_execvp_after_mkdir(self) -> None:
        cfg = self.make_config()
        args = uxon.ParsedArgs(action="run", agent_args=[])
        with self._stub_socket_path():
            with mock.patch.object(uxon, "run_cmd") as run_cmd:
                with mock.patch.object(uxon.os, "execvp") as execvp:
                    uxon.launch_in_tmux("/srv/repos/demo", "uxon-demo", args, cfg, None, "u-vz")
        run_cmd.assert_called_once()
        execvp.assert_called_once()

    def test_find_project_config_ignores_permission_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            allowed = [str(root)]
            target = root / "a" / "b"
            target.mkdir(parents=True)

            def fake_exists(self: Path) -> bool:
                if self == root / "a" / ".uxon.toml":
                    raise PermissionError("denied")
                return False

            with mock.patch.object(Path, "exists", fake_exists):
                self.assertIsNone(uxon.find_project_config(str(target), allowed))

    # ── is_launch_target_allowed / ensure_launch_target_allowed ──────
    # Mirrors the TUI's "new session in current folder" gate so the CLI
    # and the TUI behave identically. Predicate (in order):
    #   1. target must be an existing directory
    #   2. launch_user must be able to write to it
    #   3. when allowed_roots is non-empty, target must sit under one
    #      of them (no HOME-implicit, no other implicit allowance)

    def test_launch_target_rejects_nonexistent_directory(self) -> None:
        cfg = self.make_config(allowed_roots=[])
        self.assertFalse(
            uxon.is_launch_target_allowed(cfg, "u-ed", "/no/such/dir/here"),
        )
        with self.assertRaises(SystemExit):
            uxon.ensure_launch_target_allowed(cfg, "u-ed", "/no/such/dir/here")

    def test_launch_target_rejects_unwritable_directory(self) -> None:
        cfg = self.make_config(allowed_roots=[])
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(uxon, "probe_cwd_writable", return_value=False):
                self.assertFalse(uxon.is_launch_target_allowed(cfg, "u-ed", tmp))
                with self.assertRaises(SystemExit):
                    uxon.ensure_launch_target_allowed(cfg, "u-ed", tmp)

    def test_launch_target_writable_passes_when_allowed_roots_empty(self) -> None:
        cfg = self.make_config(allowed_roots=[])
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(uxon, "probe_cwd_writable", return_value=True):
                self.assertTrue(uxon.is_launch_target_allowed(cfg, "u-ed", tmp))
                # ensure_… is the raise-on-failure variant; passing case
                # must not raise.
                uxon.ensure_launch_target_allowed(cfg, "u-ed", tmp)

    def test_launch_target_strict_whitelist_when_allowed_roots_set(self) -> None:
        cfg = self.make_config(allowed_roots=["/srv/repos"])
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(uxon, "probe_cwd_writable", return_value=True):
                # Writable but outside the whitelist → fail.
                with mock.patch.object(uxon, "is_under", return_value=False):
                    self.assertFalse(uxon.is_launch_target_allowed(cfg, "u-ed", tmp))
                    with self.assertRaises(SystemExit):
                        uxon.ensure_launch_target_allowed(cfg, "u-ed", tmp)
                # Writable and inside the whitelist → pass.
                with mock.patch.object(uxon, "is_under", return_value=True):
                    self.assertTrue(uxon.is_launch_target_allowed(cfg, "u-ed", tmp))
                    uxon.ensure_launch_target_allowed(cfg, "u-ed", tmp)

    def test_launch_target_no_home_implicit_when_allowed_roots_set(self) -> None:
        # Regression guard for the old behaviour where the launch user's
        # $HOME was silently appended to allowed_roots: a writable dir
        # outside the whitelist must NOT pass when allowed_roots is set.
        cfg = self.make_config(allowed_roots=["/srv/repos"])
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(uxon, "probe_cwd_writable", return_value=True):
                self.assertFalse(uxon.is_launch_target_allowed(cfg, "u-ed", tmp))
                with self.assertRaises(SystemExit):
                    uxon.ensure_launch_target_allowed(cfg, "u-ed", tmp)

    # ── TUI callback error surfacing (0.10.3) ────────────────────────

    def test_sanitize_callback_stderr_strips_ccw_prefix_and_list_indent(self) -> None:
        raw = (
            "uxon: directory must be under one of:\n"
            "uxon:   - /srv/repos\n"
            "uxon:   - /home/u-ed\n"
            "uxon: got: /tmp\n"
        )
        expected = "directory must be under one of:\n  - /srv/repos\n  - /home/u-ed\ngot: /tmp"
        self.assertEqual(uxon._sanitize_callback_stderr(raw), expected)

    def test_sanitize_callback_stderr_passes_through_non_uxon_lines(self) -> None:
        raw = "random warning\nuxon: the real error\n\n"
        self.assertEqual(
            uxon._sanitize_callback_stderr(raw),
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
            entries = uxon._list_existing_projects(str(root))
        names = [n for n, _ in entries]
        self.assertEqual(names, ["alpha", "beta"])
        for _, mtime in entries:
            # Either HH:MM (today) or MM-DD. Never blank on a fresh mkdir.
            self.assertRegex(mtime, r"^\d\d[:-]\d\d$")

    def test_list_existing_projects_missing_root_returns_empty(self) -> None:
        self.assertEqual(uxon._list_existing_projects("/nonexistent/path/for/uxon/test"), [])

    def test_wrap_tui_callback_passes_return_value(self) -> None:
        class _Err(Exception):
            pass

            # pragma: no cover — marker only

        wrapped = uxon._wrap_tui_callback(lambda x, y: x + y, _Err)
        self.assertEqual(wrapped(2, 3), 5)

    def test_wrap_tui_callback_captures_fail_message(self) -> None:
        class _Err(Exception):
            pass

        def inner() -> None:
            uxon.fail("directory must be under one of:\nccw:   - /srv/repos")

        wrapped = uxon._wrap_tui_callback(inner, _Err)
        with self.assertRaises(_Err) as cm:
            wrapped()
        # Leading "uxon: " prefix must be stripped; list indent normalised.
        self.assertIn("directory must be under one of:", str(cm.exception))
        self.assertIn("/srv/repos", str(cm.exception))
        self.assertNotIn("uxon:", str(cm.exception))

    def test_wrap_tui_callback_falls_back_to_exit_code_when_stderr_empty(self) -> None:
        class _Err(Exception):
            pass

        def inner() -> None:
            raise SystemExit(7)

        wrapped = uxon._wrap_tui_callback(inner, _Err)
        with self.assertRaises(_Err) as cm:
            wrapped()
        self.assertIn("7", str(cm.exception))

    # ── tmux nesting detection ───────────────────────────────────────

    def test_tmux_host_socket_returns_none_without_env(self) -> None:
        with mock.patch.dict(uxon.os.environ, {}, clear=True):
            self.assertIsNone(uxon.tmux_host_socket())

    def test_tmux_host_socket_parses_socket_from_tmux_env(self) -> None:
        env = {"TMUX": "/tmp/uxon-u-vz.sock,12345,0"}
        with mock.patch.dict(uxon.os.environ, env, clear=True):
            self.assertEqual(uxon.tmux_host_socket(), "/tmp/uxon-u-vz.sock")

    def test_tmux_nesting_mode_execvp_when_not_in_tmux(self) -> None:
        with mock.patch.object(uxon, "tmux_host_socket", return_value=None):
            self.assertEqual(uxon.tmux_nesting_mode("/tmp/uxon-u-vz.sock"), "execvp")

    def test_tmux_nesting_mode_switch_when_same_socket(self) -> None:
        with mock.patch.object(uxon, "tmux_host_socket", return_value="/tmp/uxon-u-vz.sock"):
            self.assertEqual(uxon.tmux_nesting_mode("/tmp/uxon-u-vz.sock"), "switch")

    def test_tmux_nesting_mode_fails_when_foreign_socket(self) -> None:
        with mock.patch.object(uxon, "tmux_host_socket", return_value="/tmp/tmux-1000/default"):
            with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                with self.assertRaises(SystemExit) as ctx:
                    uxon.tmux_nesting_mode("/tmp/uxon-u-vz.sock")
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertIn("different socket", stderr.getvalue())

    def test_build_tmux_attach_request_uses_switch_client_when_nested(self) -> None:
        cfg = self.make_config()
        target = self.make_session("uxon-demo", "/srv/repos/demo")
        stubs = _StubsChain(
            mock.patch.object(uxon, "tmux_socket_path", return_value="/tmp/uxon-test.sock"),
            mock.patch.object(uxon, "tmux_host_socket", return_value="/tmp/uxon-test.sock"),
        )
        with stubs:
            req = uxon._build_tmux_attach_request(target, cfg, "u-vz")
        self.assertIn("switch-client", req.cmd)
        self.assertNotIn("attach-session", req.cmd)
        self.assertIn("uxon-demo", req.cmd)
        self.assertEqual(req.prelaunch, ())
        self.assertIn("switch-client", req.label)

    def test_build_tmux_launch_request_uses_switch_client_when_nested(self) -> None:
        cfg = self.make_config(
            agent_default_args={"claude": ("--model", "sonnet"), "codex": (), "cursor": ()}
        )
        args = uxon.ParsedArgs(action="run", permission_mode="yolo", agent_args=["--foo"])
        stubs = _StubsChain(
            mock.patch.object(uxon, "tmux_socket_path", return_value="/tmp/uxon-test.sock"),
            mock.patch.object(uxon, "tmux_host_socket", return_value="/tmp/uxon-test.sock"),
        )
        with stubs:
            req = uxon._build_tmux_launch_request(
                "/srv/repos/demo", "uxon-demo@claude", args, cfg, None, "u-vz"
            )
        # Main cmd is the switch; creation happens in prelaunch.
        self.assertIn("switch-client", req.cmd)
        self.assertIn("uxon-demo@claude", req.cmd)
        # Two prelaunches: mkdir + detached create-or-noop with claude args.
        self.assertEqual(len(req.prelaunch), 2)
        mkdir_pre, create_pre = req.prelaunch
        self.assertIn("mkdir", mkdir_pre)
        self.assertIn("new-session", create_pre)
        self.assertIn("-dA", create_pre)
        self.assertIn("uxon-demo@claude", create_pre)
        self.assertIn("claude", create_pre)
        self.assertIn("--dangerously-skip-permissions", create_pre)
        self.assertIn("--foo", create_pre)
        self.assertIn("--model", create_pre)
        self.assertIn("sonnet", create_pre)
        self.assertIn("nested", req.label)

    # ── Task 5: --agent / --auto / permission_mode ───────────────────

    def test_parse_dsp_and_auto_mutually_exclusive(self) -> None:
        with self.assertRaises(SystemExit):
            uxon.parse_run_like(["--dsp", "--auto"], "run")
        with self.assertRaises(SystemExit):
            uxon.parse_run_like(["--auto", "--dsp"], "run")

    def test_parse_agent_flag(self) -> None:
        p = uxon.parse_run_like(["--agent", "codex", "--dsp"], "run")
        self.assertEqual(p.agent, "codex")
        self.assertEqual(p.permission_mode, "yolo")

    def test_parse_unknown_flag_goes_to_agent_args(self) -> None:
        p = uxon.parse_run_like(["--some-claude-flag", "x"], "run")
        self.assertEqual(p.agent_args, ["--some-claude-flag", "x"])

    def test_launch_builder_cursor_yolo(self) -> None:
        cfg = self.make_config(enabled_agents=("cursor",), default_agent="cursor")
        args = uxon.ParsedArgs(action="run", permission_mode="yolo")
        with self._stub_socket_path():
            req = uxon._build_tmux_launch_request(
                "/tmp/x", "uxon-x@cursor", args, cfg, None, "u-vz"
            )
        self.assertIn("cursor-agent", req.cmd)
        self.assertIn("--yolo", req.cmd)

    def test_launch_builder_cursor_auto_errors(self) -> None:
        cfg = self.make_config(enabled_agents=("cursor",), default_agent="cursor")
        args = uxon.ParsedArgs(action="run", permission_mode="auto", agent="cursor")
        with self._stub_socket_path():
            with self.assertRaises(SystemExit):
                uxon._build_tmux_launch_request("/tmp/x", "uxon-x@cursor", args, cfg, None, "u-vz")

    def test_launch_builder_codex_full_auto(self) -> None:
        cfg = self.make_config(enabled_agents=("codex",), default_agent="codex")
        args = uxon.ParsedArgs(action="run", permission_mode="auto", agent="codex")
        with self._stub_socket_path():
            req = uxon._build_tmux_launch_request("/tmp/x", "uxon-x@codex", args, cfg, None, "u-vz")
        self.assertIn("--full-auto", req.cmd)

    def test_launch_builder_branch_does_not_add_native_w_flag(self) -> None:
        # uxon launches worktrees via ``-c <worktree_path>``, never the
        # agent's native ``-w`` flag (§2.1) — branch is informational only.
        cfg = self.make_config()
        args = uxon.ParsedArgs(action="run", agent="claude", permission_mode="normal")
        with self._stub_socket_path():
            req = uxon._build_tmux_launch_request(
                "/srv/repos/myapp/.uxon/worktrees/feat",
                "uxon-myapp-feat@claude",
                args,
                cfg,
                "feat",
                "u-vz",
            )
        joined = " ".join(req.cmd)
        self.assertNotIn(" -w ", f" {joined} ")
        self.assertNotIn("-w feat", joined)

    def test_launch_builder_branch_allowed_for_non_claude_agent(self) -> None:
        # The old "-w is only supported for claude" guard is gone.
        cfg = self.make_config(enabled_agents=("codex",), default_agent="codex")
        args = uxon.ParsedArgs(action="run", agent="codex", permission_mode="normal")
        with self._stub_socket_path():
            req = uxon._build_tmux_launch_request(
                "/srv/repos/myapp/.uxon/worktrees/feat",
                "uxon-myapp-feat@codex",
                args,
                cfg,
                "feat",
                "u-vz",
            )
        self.assertTrue(req.cmd)

    def test_auto_with_cursor_default_fails_at_launch(self) -> None:
        # Full-stack check: parser accepts --auto without knowing the resolved agent;
        # launch builder must reject it when the resolved agent has no auto mode.
        cfg = self.make_config(enabled_agents=("cursor",), default_agent="cursor")
        parsed = uxon.parse_run_like(["--auto"], "run")
        self.assertEqual(parsed.permission_mode, "auto")
        self.assertIsNone(parsed.agent)  # not explicitly set
        with self._stub_socket_path():
            with self.assertRaises(SystemExit):
                uxon._build_tmux_launch_request(
                    "/tmp/x", "uxon-x@cursor", parsed, cfg, None, "u-vz"
                )


def _mk_session(
    name: str, path: str = "/srv/repos/x", agent: str = "claude", legacy: bool = False
) -> uxon.SessionInfo:
    return uxon.SessionInfo(
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


class NonintGitResolverTests(unittest.TestCase):
    def test_repo_root_nonint_uses_nonint_prefix(self) -> None:
        import uxon.cli as cli

        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd

            class CP:
                returncode = 0
                stdout = "/srv/work/myapp\n"
                stderr = ""

            return CP()

        with (
            mock.patch.object(cli.subprocess, "run", fake_run),
            mock.patch.object(cli, "process_user", return_value="caller"),
        ):
            root = cli.git_repo_root_nonint_as_user("/srv/work/myapp/sub", "devagent")
        self.assertEqual(root, cli.canonical("/srv/work/myapp"))
        # The resolver uses the non-interactive (``sudo -n``) prefix — assert
        # it is the leading prefix of the issued argv. (cli.py composes the
        # non-interactive flags as ``-niu``, so the prefix is checked as a
        # whole rather than for a standalone ``-n`` token.)
        prefix = cli.nonint_command_prefix_for_user("devagent")
        self.assertEqual(seen["cmd"][: len(prefix)], prefix)

    def test_repo_root_nonint_none_on_failure(self) -> None:
        import uxon.cli as cli

        def fake_run(cmd, **kw):
            class CP:
                returncode = 128
                stdout = ""
                stderr = "not a git repo"

            return CP()

        with (
            mock.patch.object(cli.subprocess, "run", fake_run),
            mock.patch.object(cli, "process_user", return_value="caller"),
        ):
            self.assertIsNone(cli.git_repo_root_nonint_as_user("/tmp/x", "devagent"))

    def test_common_dir_normalises_to_primary_root(self) -> None:
        import uxon.cli as cli

        # git rev-parse --git-common-dir on a linked worktree returns the
        # primary repo's .git; the primary root is its parent.
        def fake_run(cmd, **kw):
            class CP:
                returncode = 0
                stdout = "/srv/work/myapp/.git\n"
                stderr = ""

            return CP()

        with (
            mock.patch.object(cli.subprocess, "run", fake_run),
            mock.patch.object(cli, "process_user", return_value="caller"),
        ):
            root = cli.git_common_dir_root_as_user(
                "/srv/work/myapp/.uxon/worktrees/feat", "devagent"
            )
        self.assertEqual(root, cli.canonical("/srv/work/myapp"))


class AllowedRootsUnifiedSemanticsTests(unittest.TestCase):
    """Regression: empty ``allowed_roots`` must mean "any writable" everywhere.

    The 3.1.0 fix introduced this semantics for ``is_launch_target_allowed``
    but missed ``do_new``, ``_resolve_tui_project_dir``,
    ``do_doctor`` and ``find_project_config``. After the unification
    refactor every consumer routes through
    :func:`uxon.cli.is_under_allowed_roots` so the four sites behave
    identically.
    """

    def _cfg(self, **overrides) -> uxon.Config:
        defaults = dict(
            runtime_user="",
            default_launch_mode="caller",
            enable_all_users_list=False,
            launch_user_by_caller={},
            session_users=[],
            allowed_roots=[],
            session_prefix="uxon-",
            legacy_session_prefixes=(),
            enabled_agents=("claude",),
            default_agent="claude",
            agent_default_args={"claude": (), "codex": (), "cursor": ()},
            new_project_root="/srv/work",
            repeat_noninteractive_mode="fail",
            tmux_socket_template="/tmp/uxon-{user}.sock",
            tui_refresh_interval_seconds=2.0,
            git_create_enabled=False,
            default_git_remote_profile="",
            git_remote_profiles=[],
        )
        defaults.update(overrides)
        return uxon.Config(**defaults)

    def test_is_under_allowed_roots_empty_list_returns_true(self) -> None:
        cfg = self._cfg(allowed_roots=[])
        self.assertTrue(uxon.is_under_allowed_roots(cfg, "/anything/at/all"))

    def test_is_under_allowed_roots_non_empty_strict(self) -> None:
        cfg = self._cfg(allowed_roots=["/srv/work"])
        self.assertTrue(uxon.is_under_allowed_roots(cfg, "/srv/work/proj"))
        self.assertFalse(uxon.is_under_allowed_roots(cfg, "/home/u/proj"))

    def test_do_new_empty_allowed_roots_passes_writable_parent(self) -> None:
        """Regression for the original bug report: ``uxon new x --dry-run``
        used to fail with "new target must be under allowed_roots" even
        when ``allowed_roots=[]``.
        """
        cfg = self._cfg(allowed_roots=[])
        args = uxon.ParsedArgs(action="new", target_id="demo", dry_run=True, agent_args=[])

        with mock.patch.object(uxon, "probe_cwd_writable", return_value=True):
            with mock.patch.object(uxon, "canonical", side_effect=lambda v: str(v)):
                with mock.patch.object(uxon.os, "getcwd", return_value="/home/u-vz"):
                    with mock.patch.object(uxon, "collect_sessions", return_value=[]):
                        with mock.patch.object(
                            uxon, "allocate_session_name", return_value="uxon-demo"
                        ):
                            with mock.patch.object(uxon, "launch_in_tmux", return_value=0):
                                rc = uxon.do_new(args, cfg, "u-vz")
        self.assertEqual(rc, 0)

    def test_do_new_empty_allowed_roots_rejects_unwritable_parent(self) -> None:
        """Empty ``allowed_roots`` doesn't mean "anything goes" — the
        parent of the new project still has to be writable for the
        launch user."""
        cfg = self._cfg(allowed_roots=[])
        args = uxon.ParsedArgs(action="new", target_id="demo", dry_run=True, agent_args=[])

        with mock.patch.object(uxon, "probe_cwd_writable", return_value=False):
            with mock.patch.object(uxon, "canonical", side_effect=lambda v: str(v)):
                with mock.patch.object(uxon, "eprint"):
                    with self.assertRaises(SystemExit) as exc:
                        uxon.do_new(args, cfg, "u-vz")
        self.assertEqual(exc.exception.code, 2)

    def test_do_new_non_empty_allowed_roots_rejects_outside(self) -> None:
        cfg = self._cfg(allowed_roots=["/srv/work"], new_project_root="/home/u-vz")
        args = uxon.ParsedArgs(action="new", target_id="demo", dry_run=True, agent_args=[])

        with mock.patch.object(uxon, "probe_cwd_writable", return_value=True):
            with mock.patch.object(uxon, "canonical", side_effect=lambda v: str(v)):
                with mock.patch.object(uxon, "eprint"):
                    with self.assertRaises(SystemExit) as exc:
                        uxon.do_new(args, cfg, "u-vz")
        self.assertEqual(exc.exception.code, 2)

    def test_doctor_no_issue_when_allowed_roots_empty(self) -> None:
        """Doctor should not flag ``new_project_root outside allowed_roots``
        when ``allowed_roots`` is empty — the whitelist is bypassed
        and the path is vacuously fine."""
        cfg = self._cfg(allowed_roots=[], new_project_root="/anywhere")
        # Direct unit test of the predicate that drives the doctor
        # warning; the full doctor flow is exercised elsewhere.
        self.assertTrue(uxon.is_under_allowed_roots(cfg, cfg.new_project_root))

    def test_find_project_config_empty_list_returns_any_uxon_toml(self) -> None:
        """Regression: with ``allowed_roots=[]``, ``find_project_config``
        used to silently return ``None`` (empty for-loop never matched),
        so project configs were ignored on default-config hosts."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_real = os.path.realpath(tmp)
            (Path(tmp_real) / ".uxon.toml").write_text("# stub\n")
            sub = Path(tmp_real) / "sub"
            sub.mkdir()
            found = uxon.find_project_config(str(sub), allowed_roots=[])
            self.assertIsNotNone(found)
            self.assertEqual(str(found), str(Path(tmp_real) / ".uxon.toml"))

    def test_find_project_config_non_empty_list_still_strict(self) -> None:
        """Non-empty ``allowed_roots`` still constrains the walk — a
        ``.uxon.toml`` outside the listed roots is rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_real = os.path.realpath(tmp)
            (Path(tmp_real) / ".uxon.toml").write_text("# stub\n")
            sub = Path(tmp_real) / "sub"
            sub.mkdir()
            found = uxon.find_project_config(str(sub), allowed_roots=["/some/other/root"])
            self.assertIsNone(found)


class SessionNamingTests(unittest.TestCase):
    """Tests for the new uxon-<stem>@<agent> session naming scheme."""

    def test_parse_session_name_new(self) -> None:
        self.assertEqual(uxon.parse_session_name("uxon-foo@codex"), ("foo", "codex", 1, False))
        self.assertEqual(uxon.parse_session_name("uxon-foo@codex-3"), ("foo", "codex", 3, False))
        self.assertEqual(
            uxon.parse_session_name("uxon-my-repo-branch@claude"),
            ("my-repo-branch", "claude", 1, False),
        )

    def test_parse_session_name_legacy_at_prefix(self) -> None:
        # ``ccw-`` sessions still parse when listed in ``legacy_prefixes`` and
        # are flagged ``legacy=True`` (default prefix is ``uxon-``).
        self.assertEqual(
            uxon.parse_session_name("ccw-foo@codex", legacy_prefixes=("ccw-",)),
            ("foo", "codex", 1, True),
        )
        # When ``uxon-`` is the configured current prefix, the same-prefixed
        # name is not legacy.
        self.assertEqual(
            uxon.parse_session_name("uxon-foo@codex", prefix="uxon-"),
            ("foo", "codex", 1, False),
        )
        # Without legacy_prefixes, a ccw- name does not match (default prefix
        # is uxon-).
        self.assertIsNone(uxon.parse_session_name("ccw-foo@codex"))
        # And with a non-matching explicit prefix, uxon- is also unrecognised.
        self.assertIsNone(uxon.parse_session_name("uxon-foo@codex", prefix="ccw-"))

    def test_parse_session_name_rejects_garbage(self) -> None:
        self.assertIsNone(uxon.parse_session_name("random-x"))
        self.assertIsNone(uxon.parse_session_name("uxon-foo"))  # missing @agent
        self.assertIsNone(uxon.parse_session_name("cc-foo"))  # ancient format no longer recognised

    def test_candidate_session_name(self) -> None:
        self.assertEqual(uxon.candidate_session_name("foo", 1, "cursor"), "uxon-foo@cursor")
        self.assertEqual(uxon.candidate_session_name("foo", 2, "cursor"), "uxon-foo@cursor-2")

    def test_compatible_indexed_sessions_agent_specific(self) -> None:
        # Two sessions same stem different agents are NOT siblings.
        compat_root = "/srv/repos/foo"
        s_claude = _mk_session("uxon-foo@claude", compat_root, agent="claude")
        s_codex = _mk_session("uxon-foo@codex", compat_root, agent="codex")
        matches = uxon.compatible_indexed_sessions(
            "foo", "claude", compat_root, [s_claude, s_codex]
        )
        self.assertEqual([m.name for m in matches], ["uxon-foo@claude"])

    def test_resolve_full_new(self) -> None:
        sessions = [_mk_session("uxon-foo@claude"), _mk_session("uxon-foo@codex", agent="codex")]
        self.assertEqual(
            uxon.resolve_session("uxon-foo@codex", sessions, "uxon-").name,
            "uxon-foo@codex",
        )

    def test_resolve_suffixed_without_prefix(self) -> None:
        sessions = [_mk_session("uxon-foo@codex", agent="codex")]
        self.assertEqual(
            uxon.resolve_session("foo@codex", sessions, "uxon-").name,
            "uxon-foo@codex",
        )

    def test_resolve_stem_unique(self) -> None:
        sessions = [_mk_session("uxon-foo@codex", agent="codex")]
        self.assertEqual(
            uxon.resolve_session("foo", sessions, "uxon-").name,
            "uxon-foo@codex",
        )

    def test_resolve_stem_ambiguous(self) -> None:
        sessions = [_mk_session("uxon-foo@claude"), _mk_session("uxon-foo@codex", agent="codex")]
        with self.assertRaises(SystemExit):
            uxon.resolve_session("foo", sessions, "uxon-")


class CliPreflightTests(unittest.TestCase):
    """Tests for CLI preflight probe in main()."""

    def test_preflight_tmux_missing_on_run_action(self) -> None:
        """When tmux is missing, run action should fail with friendly message."""
        buf_err = io.StringIO()
        with mock.patch.object(sys, "stderr", buf_err):
            with mock.patch("uxon.probes.probe_host") as probe:
                mock_tmux_missing = mock.MagicMock()
                mock_tmux_missing.tmux.path = None
                mock_tmux_missing.tmux.install_hint = "apt install tmux"
                mock_tmux_missing.agents = {"claude": mock.MagicMock(path="/usr/bin/claude")}
                probe.return_value = mock_tmux_missing

                with self.assertRaises(SystemExit) as ctx:
                    uxon.main(["run"])
                self.assertEqual(ctx.exception.code, 1)
                err = buf_err.getvalue()
                self.assertIn("tmux is not installed", err)
                self.assertIn("apt install tmux", err)

    def test_preflight_agent_missing_on_run_action(self) -> None:
        """When an explicitly requested agent is missing on ``run``,
        :func:`resolve_agent_id` surfaces an install hint with exit
        code 1 (environment error, not usage). The preflight only
        owns the tmux check now; agent install-gating is centralised
        in ``resolve_agent_id``.
        """
        buf_err = io.StringIO()
        with mock.patch.object(sys, "stderr", buf_err):
            with mock.patch("uxon.probes.probe_host") as probe:
                mock_report = mock.MagicMock()
                mock_report.tmux.path = "/usr/bin/tmux"
                mock_claude = mock.MagicMock()
                mock_claude.path = None
                mock_claude.install_hint = "npm install -g @anthropic-ai/claude-code"
                mock_report.agents = {"claude": mock_claude}
                probe.return_value = mock_report

                with self.assertRaises(SystemExit) as ctx:
                    uxon.main(["run", "--agent", "claude"])
                self.assertEqual(ctx.exception.code, 1)
                err = buf_err.getvalue()
                self.assertIn("'claude'", err)
                self.assertIn("is not installed", err)
                self.assertIn("npm install", err)

    def test_preflight_skipped_on_version_action(self) -> None:
        """version action should skip the preflight probe."""
        with mock.patch("uxon.probes.probe_host") as probe:
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                uxon.main(["version"])
            # Probe should never have been called.
            probe.assert_not_called()

    def test_preflight_skipped_on_doctor_action(self) -> None:
        """doctor action should skip the preflight probe."""
        with mock.patch("uxon.probes.probe_host") as probe:
            with mock.patch("uxon.cli.do_doctor", return_value=0):
                uxon.main(["doctor"])
            # Probe should never have been called.
            probe.assert_not_called()

    def test_preflight_skipped_on_interactive_action(self) -> None:
        """interactive (TUI) action skips the CLI preflight.

        Regression: a wider gate that included ``interactive`` made every
        no-arg ``uxon`` invocation block on a sudo round-trip before the
        TUI mounted, defeating the fast-first-frame design. The TUI runs
        its own async probe in the background.
        """
        with mock.patch("uxon.probes.probe_host") as probe:
            with mock.patch("uxon.cli.do_interactive", return_value=0):
                uxon.main([])
            probe.assert_not_called()

    def test_preflight_passes_on_run_both_ok(self) -> None:
        """When tmux and agent are both present, run action should proceed past preflight."""
        with mock.patch("uxon.probes.probe_host") as probe:
            mock_report = mock.MagicMock()
            mock_report.tmux.path = "/usr/bin/tmux"
            mock_claude = mock.MagicMock()
            mock_claude.path = "/home/user/.npm/claude"
            mock_report.agents = {"claude": mock_claude}
            probe.return_value = mock_report

            with mock.patch("uxon.cli.do_run", return_value=0):
                rc = uxon.main(["run"])
            self.assertEqual(rc, 0)

    def test_preflight_list_action_does_not_need_agents(self) -> None:
        """list action should check tmux but not any specific agent."""
        with mock.patch("uxon.probes.probe_host") as probe:
            mock_report = mock.MagicMock()
            mock_report.tmux.path = "/usr/bin/tmux"
            # Agent can be missing; list doesn't care.
            mock_report.agents = {"claude": mock.MagicMock(path=None)}
            probe.return_value = mock_report

            with mock.patch("uxon.cli.print_list", return_value=0):
                with mock.patch("uxon.cli.collect_sessions", return_value=[]):
                    rc = uxon.main(["list"])
                # Should not have failed; list doesn't require agents.
                self.assertEqual(rc, 0)

    def test_peer_inbound_list_all_users_disabled_emits_remote_in_denied(self) -> None:
        # Spec lines 207-209: state-changing events emit on success AND
        # failure paths.  Spec line 306: peer-inbound list emits
        # ``list.remote.in`` instead of ``list.peek``.  Combined: a
        # peer that refuses ``--all-users`` (because
        # ``enable_all_users_list = false``) must record exactly one
        # ``list.remote.in outcome=denied``, no parallel ``list.peek``,
        # and no stale ``outcome=ok`` from a top-of-block emit.
        # Regression for the pre-fix bug where the peer-inbound branch
        # emitted ``outcome=ok`` *before* the gate check, then ``fail``
        # raised SystemExit unaudited.
        import uxon.cli as cli
        from uxon import audit as uxon_audit

        recorded: list[tuple[str, dict]] = []

        def fake_audit(event: str, *, outcome: str = "ok", **fields: object) -> None:
            recorded.append((event, {"outcome": outcome, **fields}))

        cfg = cli.Config(
            runtime_user="",
            default_launch_mode="caller",
            enable_all_users_list=False,
            launch_user_by_caller={},
            session_users=[],
            allowed_roots=["/srv/repos"],
            session_prefix="uxon-",
            legacy_session_prefixes=(),
            enabled_agents=("claude",),
            default_agent="claude",
            agent_default_args={"claude": (), "codex": (), "cursor": ()},
            new_project_root="/srv/repos",
            repeat_noninteractive_mode="fail",
            tmux_socket_template="/tmp/uxon-{user}.sock",
            tui_refresh_interval_seconds=2.0,
            git_create_enabled=False,
            default_git_remote_profile="",
            git_remote_profiles=[],
        )

        with mock.patch("uxon.probes.probe_host") as probe:
            mock_report = mock.MagicMock()
            mock_report.tmux.path = "/usr/bin/tmux"
            mock_report.agents = {"claude": mock.MagicMock(path=None)}
            probe.return_value = mock_report
            with (
                mock.patch.dict("os.environ", {"SSH_CONNECTION": "1.2.3.4 22 5.6.7.8 22"}),
                mock.patch.object(cli, "load_config", return_value=cfg),
                mock.patch.object(uxon_audit, "audit", side_effect=fake_audit),
                mock.patch("sys.stderr", new_callable=io.StringIO),
            ):
                with self.assertRaises(SystemExit):
                    uxon.main(["list", "--all-users"])

        list_emits = [e for e in recorded if e[0] in ("list.remote.in", "list.peek")]
        peek_emits = [e for e in list_emits if e[0] == "list.peek"]
        rin_emits = [e for e in list_emits if e[0] == "list.remote.in"]
        # ``replaces`` semantics: on the peer-inbound path, no
        # ``list.peek`` may be emitted alongside.
        self.assertEqual(peek_emits, [])
        self.assertEqual(len(rin_emits), 1)
        self.assertEqual(rin_emits[0][1]["outcome"], "denied")
        self.assertEqual(rin_emits[0][1]["scope"], "all-users")


class DoInteractiveTextualMissingTests(unittest.TestCase):
    """With textual unavailable, ``uxon`` (interactive) must print a single
    install hint on stderr, no traceback, and return 1."""

    def test_prints_install_hint_when_textual_missing(self) -> None:
        # Simulate a stripped install where ``uxon.tui`` (and its
        # textual dep) is unavailable. A ``sys.modules`` sentinel alone
        # is insufficient because the package may already be cached as
        # an attribute on ``uxon``; we also clear that attribute and
        # restore it on teardown.
        import uxon as uxon_pkg

        saved_uxon_tui_module = sys.modules.get("uxon.tui")
        saved_uxon_tui_attr = getattr(uxon_pkg, "tui", None)
        sys.modules["uxon.tui"] = None  # type: ignore[assignment]
        if hasattr(uxon_pkg, "tui"):
            delattr(uxon_pkg, "tui")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfg = uxon.load_config(tmp)
                buf_err = io.StringIO()
                buf_out = io.StringIO()
                with (
                    mock.patch.object(sys, "stderr", buf_err),
                    mock.patch.object(sys, "stdout", buf_out),
                ):
                    rc = uxon.do_interactive(cfg, "nobody")
                self.assertEqual(rc, 1)
                err_text = buf_err.getvalue()
                self.assertIn("requires", err_text)
                self.assertIn("textual", err_text)
                self.assertNotIn("Traceback", err_text)
        finally:
            if saved_uxon_tui_module is None:
                sys.modules.pop("uxon.tui", None)
            else:
                sys.modules["uxon.tui"] = saved_uxon_tui_module
            if saved_uxon_tui_attr is not None:
                uxon_pkg.tui = saved_uxon_tui_attr  # type: ignore[attr-defined]


class TuiPlannerWorktreeStemTests(unittest.TestCase):
    def test_run_agent_uses_worktree_stem_when_branch_given(self) -> None:
        import uxon.cli as cli

        captured = {}

        def fake_alloc(stem, agent, root, sessions, *, prefix):
            captured["stem"] = stem
            return f"{prefix}{stem}@{agent}"

        cfg = cli.load_config("/tmp")
        with (
            mock.patch.object(cli, "ensure_launch_target_allowed", lambda *a, **k: None),
            mock.patch.object(cli, "collect_sessions", return_value=[]),
            mock.patch.object(cli, "allocate_session_name", fake_alloc),
            mock.patch.object(
                cli,
                "_build_tmux_launch_request",
                lambda *a, **k: cli._tui_launch_request_cls()(cmd=("true",), label="x"),
            ),
        ):
            cli._plan_tui_run_agent(
                cfg,
                "devagent",
                "/srv/work/myapp/.uxon/worktrees/feature-auth",
                "claude",
                "default",
                worktree=("/srv/work/myapp", "feature/auth"),
            )
        self.assertEqual(captured["stem"], "myapp-feature-auth")

    def test_run_agent_uses_path_stem_without_worktree(self) -> None:
        import uxon.cli as cli

        captured = {}

        def fake_alloc(stem, agent, root, sessions, *, prefix):
            captured["stem"] = stem
            return f"{prefix}{stem}@{agent}"

        cfg = cli.load_config("/tmp")
        with (
            mock.patch.object(cli, "ensure_launch_target_allowed", lambda *a, **k: None),
            mock.patch.object(cli, "collect_sessions", return_value=[]),
            mock.patch.object(cli, "allocate_session_name", fake_alloc),
            mock.patch.object(
                cli,
                "_build_tmux_launch_request",
                lambda *a, **k: cli._tui_launch_request_cls()(cmd=("true",), label="x"),
            ),
        ):
            cli._plan_tui_run_agent(cfg, "devagent", "/srv/work/plain", "claude", "default")
        self.assertEqual(captured["stem"], "plain")


class ProbeWorktreeStemTests(unittest.TestCase):
    def _session(self, name: str, path: str):
        import uxon.cli as cli

        return cli.SessionInfo(
            user="devagent",
            name=name,
            attached="0",
            windows="1",
            created="",
            last_attached="",
            pane_pids=(),
            active_pid=None,
            active_cmd="claude",
            active_path=path,
        )

    def test_explicit_stem_matches_worktree_session(self) -> None:
        import uxon.cli as cli

        wt = "/srv/work/myapp/.uxon/worktrees/feature-auth"
        sess = [self._session("uxon-myapp-feature-auth@claude", wt)]
        cfg = cli.load_config("/tmp")
        with mock.patch.object(cli, "collect_sessions", return_value=sess):
            out = cli.probe_tui_compatible_sessions(
                cfg,
                "devagent",
                wt,
                "claude",
                stem="myapp-feature-auth",
                compatibility_root=wt,
            )
        self.assertEqual([s.name for s in out], ["uxon-myapp-feature-auth@claude"])

    def test_default_stem_unchanged_for_plain_target(self) -> None:
        import uxon.cli as cli

        target = "/srv/work/plain"
        sess = [self._session("uxon-plain@claude", target)]
        cfg = cli.load_config("/tmp")
        with mock.patch.object(cli, "collect_sessions", return_value=sess):
            out = cli.probe_tui_compatible_sessions(cfg, "devagent", target, "claude")
        self.assertEqual([s.name for s in out], ["uxon-plain@claude"])


class WorktreeIdentityRegressionTests(unittest.TestCase):
    """Regression guard for §2.5: planner and probe derive the SAME
    repo-qualified stem; cross-repo same-named worktrees never collide.
    """

    def _session(self, name: str, path: str):
        import uxon.cli as cli

        return cli.SessionInfo(
            user="devagent",
            name=name,
            attached="0",
            windows="1",
            created="",
            last_attached="",
            pane_pids=(),
            active_pid=None,
            active_cmd="claude",
            active_path=path,
        )

    def test_planner_allocates_repo_qualified_name_probe_then_matches(self) -> None:
        import uxon.cli as cli

        repo = "/srv/work/myapp"
        wt = "/srv/work/myapp/.uxon/worktrees/feature-auth"
        branch = "feature/auth"
        cfg = cli.load_config("/tmp")

        # (a) planner names the session with the worktree stem.
        with (
            mock.patch.object(cli, "ensure_launch_target_allowed", lambda *a, **k: None),
            mock.patch.object(cli, "collect_sessions", return_value=[]),
            mock.patch.object(
                cli,
                "_build_tmux_launch_request",
                lambda td, s, *a, **k: cli._tui_launch_request_cls()(
                    cmd=("true",), label=f"launch {s}"
                ),
            ),
        ):
            req = cli._plan_tui_run_agent(
                cfg, "devagent", wt, "claude", "default", worktree=(repo, branch)
            )
        self.assertEqual(req.label, "launch uxon-myapp-feature-auth@claude")

        # (b) the worktree-aware probe finds exactly that session.
        live = [self._session("uxon-myapp-feature-auth@claude", wt)]
        with mock.patch.object(cli, "collect_sessions", return_value=live):
            found = cli.probe_tui_compatible_sessions(
                cfg,
                "devagent",
                wt,
                "claude",
                stem=cli.session_stem_for_worktree(repo, branch),
                compatibility_root=wt,
            )
        self.assertEqual([s.name for s in found], ["uxon-myapp-feature-auth@claude"])

    def test_two_repos_same_branch_do_not_collide(self) -> None:
        import uxon.cli as cli

        repo_b = "/srv/work/beta"
        wt_a = "/srv/work/alpha/.uxon/worktrees/feature"
        wt_b = "/srv/work/beta/.uxon/worktrees/feature"
        cfg = cli.load_config("/tmp")
        # alpha's worktree session is live; probing beta's worktree must
        # NOT match it and must NOT hard-fail (distinct repo-qualified stems).
        live = [self._session("uxon-alpha-feature@claude", wt_a)]
        with mock.patch.object(cli, "collect_sessions", return_value=live):
            found = cli.probe_tui_compatible_sessions(
                cfg,
                "devagent",
                wt_b,
                "claude",
                stem=cli.session_stem_for_worktree(repo_b, "feature"),
                compatibility_root=wt_b,
            )
        self.assertEqual(found, ())  # no match, no SystemExit


class PlanWorktreeLaunchTests(unittest.TestCase):
    def test_new_branch_local_base_adds_worktree_and_names_session(self) -> None:
        import uxon.cli as cli

        cfg = cli.load_config("/tmp")
        repo = "/srv/work/myapp"
        calls: list[list[str]] = []

        def fake_run_cmd(cmd, check=True, **kw):
            calls.append(cmd)

            class CP:
                returncode = 0
                stdout = ""
                stderr = ""

            return CP()

        events: list[tuple[str, dict]] = []

        def fake_audit(event, **fields):
            events.append((event, fields))

        with (
            mock.patch.object(cli, "is_worktree_target_allowed", return_value=True),
            mock.patch.object(cli, "collect_sessions", return_value=[]),
            mock.patch.object(cli, "run_cmd", fake_run_cmd),
            mock.patch.object(cli, "write_uxon_exclude_entry", lambda *a, **k: None),
            mock.patch.object(cli, "copy_worktreeinclude_matches", lambda *a, **k: None),
            mock.patch.object(cli, "_local_base_ref_as_user", return_value="origin/HEAD"),
            mock.patch.object(cli, "_branch_exists_as_user", return_value=False),
            mock.patch.object(
                cli,
                "_build_tmux_launch_request",
                lambda td, s, *a, **k: cli._tui_launch_request_cls()(
                    cmd=("true",), label=f"launch {s}"
                ),
            ),
            mock.patch("uxon.audit.audit", fake_audit),
        ):
            req = cli.plan_worktree_launch(
                cfg, "devagent", repo, "feature/auth", "claude", "default"
            )
        # session named with the worktree stem
        self.assertEqual(req.label, "launch uxon-myapp-feature-auth@claude")
        # a `git worktree add ... -b feature/auth` was issued
        add = [c for c in calls if "worktree" in c and "add" in c]
        self.assertTrue(add)
        self.assertIn("-b", add[0])
        # BOTH worktree.create AND session.new emitted (§4.6, B3).
        names = [e for e, _ in events]
        self.assertIn("worktree.create", names)
        self.assertIn("session.new", names)
        wc = dict(events[names.index("worktree.create")][1])
        self.assertEqual(wc.get("branch"), "feature/auth")
        self.assertEqual(wc.get("project"), repo)
        self.assertEqual(wc.get("base"), "local")
        self.assertEqual(wc.get("agent"), "claude")
        self.assertEqual(wc.get("session"), "uxon-myapp-feature-auth@claude")
        self.assertTrue(wc.get("path", "").endswith("/.uxon/worktrees/feature-auth"))
        sn = dict(events[names.index("session.new")][1])
        self.assertEqual(sn.get("session"), "uxon-myapp-feature-auth@claude")
        self.assertEqual(sn.get("branch"), "feature/auth")
        self.assertEqual(sn.get("project"), wc.get("path"))

    def test_worktree_root_outside_allowed_roots_rejected(self) -> None:
        # B1 / §2.3 / §9 "gating failure → clear error": a worktree_root
        # pointing outside allowed_roots must fail with an actionable error
        # BEFORE any git work runs.
        import uxon.cli as cli

        cfg = cli.load_config("/tmp")
        cfg.worktree_root = "/not/allowed"
        cfg.allowed_roots = ["/srv/work"]
        called: list[list[str]] = []

        def fake_run_cmd(cmd, check=True, **kw):
            called.append(cmd)

            class CP:
                returncode = 0
                stdout = ""
                stderr = ""

            return CP()

        with (
            mock.patch.object(cli, "probe_cwd_writable", return_value=True),
            mock.patch.object(cli, "run_cmd", fake_run_cmd),
        ):
            with self.assertRaises(SystemExit) as cm:
                cli.plan_worktree_launch(
                    cfg, "devagent", "/srv/work/myapp", "feature/auth", "claude", "default"
                )
        msg = getattr(cm.exception, "uxon_msg", "")
        self.assertIn("allowed_roots", msg)
        self.assertIn("worktree_root", msg)  # error suggests the override key
        # No git worktree add was attempted before the gate failed.
        self.assertFalse([c for c in called if "worktree" in c and "add" in c])

    def test_existing_branch_checks_out_without_b(self) -> None:
        import uxon.cli as cli

        cfg = cli.load_config("/tmp")
        calls: list[list[str]] = []

        def fake_run_cmd(cmd, check=True, **kw):
            calls.append(cmd)

            class CP:
                returncode = 0
                stdout = ""
                stderr = ""

            return CP()

        with (
            mock.patch.object(cli, "is_worktree_target_allowed", return_value=True),
            mock.patch.object(cli, "collect_sessions", return_value=[]),
            mock.patch.object(cli, "run_cmd", fake_run_cmd),
            mock.patch.object(cli, "write_uxon_exclude_entry", lambda *a, **k: None),
            mock.patch.object(cli, "copy_worktreeinclude_matches", lambda *a, **k: None),
            mock.patch.object(cli, "_branch_exists_as_user", return_value=True),
            mock.patch.object(
                cli,
                "_build_tmux_launch_request",
                lambda td, s, *a, **k: cli._tui_launch_request_cls()(
                    cmd=("true",), label=f"launch {s}"
                ),
            ),
            mock.patch("uxon.audit.audit", lambda *a, **k: None),
        ):
            cli.plan_worktree_launch(
                cfg, "devagent", "/srv/work/myapp", "existing", "claude", "default"
            )
        add = [c for c in calls if "worktree" in c and "add" in c]
        self.assertTrue(add)
        self.assertNotIn("-b", add[0])

    def test_agent_args_forwarded_to_launch_request(self) -> None:
        # CLI parity: `uxon -w branch -- --extra-flag` must not silently drop
        # the agent passthrough args on the worktree create path.
        import uxon.cli as cli

        cfg = cli.load_config("/tmp")
        captured: dict[str, object] = {}

        def fake_build(td, s, run_args, *a, **k):
            captured["agent_args"] = list(run_args.agent_args)
            return cli._tui_launch_request_cls()(cmd=("true",), label=f"launch {s}")

        def fake_run_cmd(cmd, check=True, **kw):
            class CP:
                returncode = 0
                stdout = ""
                stderr = ""

            return CP()

        with (
            mock.patch.object(cli, "is_worktree_target_allowed", return_value=True),
            mock.patch.object(cli, "collect_sessions", return_value=[]),
            mock.patch.object(cli, "run_cmd", fake_run_cmd),
            mock.patch.object(cli, "write_uxon_exclude_entry", lambda *a, **k: None),
            mock.patch.object(cli, "copy_worktreeinclude_matches", lambda *a, **k: None),
            mock.patch.object(cli, "_branch_exists_as_user", return_value=True),
            mock.patch.object(cli, "_build_tmux_launch_request", fake_build),
            mock.patch("uxon.audit.audit", lambda *a, **k: None),
        ):
            cli.plan_worktree_launch(
                cfg,
                "devagent",
                "/srv/work/myapp",
                "existing",
                "claude",
                "default",
                agent_args=["--extra-flag", "value"],
            )
        self.assertEqual(captured["agent_args"], ["--extra-flag", "value"])

    def test_worktree_add_failure_surfaces_clear_error(self) -> None:
        import uxon.cli as cli

        cfg = cli.load_config("/tmp")

        # The planner runs the add with check=False and inspects the
        # result itself (run_cmd's own failure path calls fail() with the
        # raw git stderr; the planner wants a friendlier message). Simulate
        # git refusing because the branch is already checked out.
        def fake_run_cmd(cmd, check=True, **kw):
            class CP:
                stdout = ""
                stderr = ""
                returncode = 0

            if "worktree" in cmd and "add" in cmd:
                CP.returncode = 128
                CP.stderr = "fatal: 'feature/auth' is already checked out at '...'"
            return CP()

        with (
            mock.patch.object(cli, "is_worktree_target_allowed", return_value=True),
            mock.patch.object(cli, "collect_sessions", return_value=[]),
            mock.patch.object(cli, "run_cmd", fake_run_cmd),
            mock.patch.object(cli, "write_uxon_exclude_entry", lambda *a, **k: None),
            mock.patch.object(cli, "_branch_exists_as_user", return_value=False),
            mock.patch.object(cli, "_local_base_ref_as_user", return_value="HEAD"),
            mock.patch.object(
                cli,
                "_build_tmux_launch_request",
                lambda td, s, *a, **k: cli._tui_launch_request_cls()(
                    cmd=("true",), label=f"launch {s}"
                ),
            ),
        ):
            with self.assertRaises(SystemExit) as cm:
                cli.plan_worktree_launch(
                    cfg, "devagent", "/srv/work/myapp", "feature/auth", "claude", "default"
                )
        # Friendly message, not the raw git fatal. fail() stashes the
        # human-readable text on the SystemExit as ``uxon_msg``.
        self.assertIn("already checked out", getattr(cm.exception, "uxon_msg", ""))

    def test_dry_run_is_side_effect_free(self) -> None:
        # dry_run must not mkdir / fetch / write exclude / add worktree /
        # emit audit — only resolve + print the plan.
        import uxon.cli as cli

        cfg = cli.load_config("/tmp")
        calls: list[list[str]] = []
        events: list[str] = []

        def fake_run_cmd(cmd, check=True, **kw):
            calls.append(cmd)

            class CP:
                returncode = 0
                stdout = ""
                stderr = ""

            return CP()

        with (
            mock.patch.object(cli, "is_worktree_target_allowed", return_value=True),
            mock.patch.object(cli, "collect_sessions", return_value=[]),
            mock.patch.object(cli, "run_cmd", fake_run_cmd),
            mock.patch.object(cli, "_branch_exists_as_user", return_value=False),
            mock.patch.object(cli, "_local_base_ref_as_user", return_value="HEAD"),
            mock.patch.object(
                cli,
                "write_uxon_exclude_entry",
                lambda *a, **k: calls.append(["WROTE_EXCLUDE"]),
            ),
            mock.patch.object(
                cli,
                "copy_worktreeinclude_matches",
                lambda *a, **k: calls.append(["COPIED"]),
            ),
            mock.patch.object(
                cli,
                "_build_tmux_launch_request",
                lambda td, s, *a, **k: cli._tui_launch_request_cls()(
                    cmd=("true",), label=f"launch {s}"
                ),
            ),
            mock.patch("uxon.audit.audit", lambda event, **k: events.append(event)),
        ):
            req = cli.plan_worktree_launch(
                cfg,
                "devagent",
                "/srv/work/myapp",
                "feature/auth",
                "claude",
                "default",
                dry_run=True,
            )
        self.assertEqual(req.label, "launch uxon-myapp-feature-auth@claude")
        # No mutating commands ran, no exclude write/copy, no audit events.
        self.assertEqual(calls, [])
        self.assertEqual(events, [])


class CliWorktreeRoutingTests(unittest.TestCase):
    def test_do_run_w_routes_through_plan_worktree_launch(self) -> None:
        import uxon.cli as cli

        cfg = cli.load_config("/tmp")
        args = cli.ParsedArgs(
            action="run",
            agent="claude",
            permission_mode="default",
            worktree_branch="feature/auth",
            dry_run=True,
        )
        captured = {}

        def fake_plan(cfg_, user, repo, branch, agent, mode, *, agent_args=None, dry_run=False):
            captured.update(repo=repo, branch=branch, agent=agent, dry_run=dry_run)
            return cli._tui_launch_request_cls()(cmd=("true",), label="launch x")

        with (
            mock.patch.object(cli, "ensure_launch_target_allowed", lambda *a, **k: None),
            mock.patch.object(cli.os, "getcwd", return_value="/srv/work/myapp/sub"),
            mock.patch.object(cli, "git_repo_root_nonint_as_user", return_value="/srv/work/myapp"),
            mock.patch.object(cli, "git_common_dir_root_as_user", return_value="/srv/work/myapp"),
            mock.patch.object(cli, "resolve_agent_id", return_value="claude"),
            mock.patch.object(cli, "plan_worktree_launch", fake_plan),
        ):
            # dry_run=True → no execvp; do_run returns 0 after printing.
            rc = cli.do_run(args, cfg, "devagent")
        self.assertEqual(rc, 0)
        self.assertEqual(captured["repo"], "/srv/work/myapp")
        self.assertEqual(captured["branch"], "feature/auth")
        self.assertTrue(captured["dry_run"])  # dry_run threaded through


def _init_repo(path: str) -> None:
    subprocess.run(["git", "init", "-q", path], check=True)
    subprocess.run(["git", "-C", path, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", path, "config", "user.name", "t"], check=True)


class ExcludeWriterTests(unittest.TestCase):
    def test_appends_uxon_line_once_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            _init_repo(d)
            uxon.write_uxon_exclude_entry(d, "devagent")
            uxon.write_uxon_exclude_entry(d, "devagent")  # idempotent
            with open(os.path.join(d, ".git", "info", "exclude")) as fh:
                text = fh.read()
        self.assertEqual(text.count(".uxon/"), 1)


class WorktreeIncludeCopyTests(unittest.TestCase):
    def test_copies_only_gitignored_and_matching(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            _init_repo(d)
            # tracked file (must NOT copy), gitignored+matching (.env, copy),
            # gitignored+not-matching (debug.log, skip).
            with open(os.path.join(d, "tracked.txt"), "w") as fh:
                fh.write("x")
            with open(os.path.join(d, ".gitignore"), "w") as fh:
                fh.write(".env\n*.log\n")
            with open(os.path.join(d, ".worktreeinclude"), "w") as fh:
                fh.write(".env\n")
            with open(os.path.join(d, ".env"), "w") as fh:
                fh.write("SECRET=1")
            with open(os.path.join(d, "debug.log"), "w") as fh:
                fh.write("noise")
            subprocess.run(
                ["git", "-C", d, "add", "tracked.txt", ".gitignore", ".worktreeinclude"],
                check=True,
            )
            subprocess.run(["git", "-C", d, "commit", "-qm", "init"], check=True)
            dest = os.path.join(d, ".uxon", "worktrees", "feat")
            os.makedirs(dest)
            uxon.copy_worktreeinclude_matches(d, dest, "devagent")
            self.assertTrue(os.path.exists(os.path.join(dest, ".env")))
            self.assertFalse(os.path.exists(os.path.join(dest, "debug.log")))
            self.assertFalse(os.path.exists(os.path.join(dest, "tracked.txt")))

    def test_no_worktreeinclude_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            _init_repo(d)
            dest = os.path.join(d, "dest")
            os.makedirs(dest)
            uxon.copy_worktreeinclude_matches(d, dest, "devagent")  # no raise
            self.assertEqual(os.listdir(dest), [])


class BuildTuiContextWorktreeWiringTests(unittest.TestCase):
    def test_probe_worktrees_returns_workspaces(self) -> None:
        import uxon.cli as cli

        porcelain = (
            "worktree /srv/work/myapp\nHEAD 1111111111111111111111111111111111111111\n"
            "branch refs/heads/main\n\n"
            "worktree /srv/work/myapp/.uxon/worktrees/feature-auth\n"
            "HEAD 2222222222222222222222222222222222222222\n"
            "branch refs/heads/feature/auth\n"
        )

        def fake_run(cmd, **kw):
            class CP:
                returncode = 0
                stdout = porcelain
                stderr = ""

            return CP()

        cfg = cli.load_config("/tmp")
        with (
            mock.patch.object(cli, "git_repo_root_nonint_as_user", return_value="/srv/work/myapp"),
            mock.patch.object(cli, "git_common_dir_root_as_user", return_value="/srv/work/myapp"),
            mock.patch.object(cli.subprocess, "run", fake_run),
            mock.patch.object(cli, "process_user", return_value="devagent"),
        ):
            ctx = cli._build_tui_context(cfg, "devagent", "/srv/work/myapp", skeleton=True)
            rows = ctx.on_probe_worktrees("/srv/work/myapp")
        self.assertTrue(rows[0].is_primary)
        self.assertEqual(rows[1].branch, "feature/auth")

    def test_probe_worktrees_non_git_returns_empty(self) -> None:
        import uxon.cli as cli

        cfg = cli.load_config("/tmp")
        with (
            mock.patch.object(cli, "git_repo_root_nonint_as_user", return_value=None),
            mock.patch.object(cli, "process_user", return_value="devagent"),
        ):
            ctx = cli._build_tui_context(cfg, "devagent", "/tmp/plain", skeleton=True)
            self.assertEqual(ctx.on_probe_worktrees("/tmp/plain"), [])


class ProbeExistingWorktreeSessionsCallbackTests(unittest.TestCase):
    def test_callback_uses_worktree_stem(self) -> None:
        import uxon.cli as cli

        repo = "/srv/work/myapp"
        wt = "/srv/work/myapp/.uxon/worktrees/feature-auth"
        sess = cli.SessionInfo(
            user="devagent",
            name="uxon-myapp-feature-auth@claude",
            attached="1",
            windows="1",
            created="",
            last_attached="",
            pane_pids=(),
            active_pid=None,
            active_cmd="claude",
            active_path=wt,
        )
        cfg = cli.load_config("/tmp")
        with (
            mock.patch.object(cli, "collect_sessions", return_value=[sess]),
            mock.patch.object(cli, "git_repo_root_nonint_as_user", return_value=repo),
            mock.patch.object(cli, "git_common_dir_root_as_user", return_value=repo),
            mock.patch.object(cli, "process_user", return_value="devagent"),
        ):
            ctx = cli._build_tui_context(cfg, "devagent", repo, skeleton=True)
            out = ctx.on_probe_existing_worktree_sessions(wt, repo, "feature/auth", "claude")
        self.assertEqual(out, (("uxon-myapp-feature-auth@claude", True),))


if __name__ == "__main__":
    unittest.main()

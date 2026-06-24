import dataclasses
import io
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import uxon.app.attach as attach_app
import uxon.app.doctor as doctor_app
import uxon.app.kill as kill_app
import uxon.app.launch as launch_app
import uxon.app.new as new_app
import uxon.app.repeat as repeat_app
import uxon.app.run as run_app
import uxon.app.tui_planning as tui_planning
import uxon.cli as uxon_cli
import uxon.tui.bridge as tui_bridge
import uxon.tui.callback_wrap as callback_wrap
import uxon.tui.context_builder as context_builder
from uxon.cli.main import do_interactive, format_version, main
from uxon.cli.parsing import parse_args, parse_run_like, parse_subcommand
from uxon.domain import authz as domain_authz
from uxon.domain import config as domain_config
from uxon.domain import session as domain_session
from uxon.domain.args import ParsedArgs
from uxon.domain.config import Config
from uxon.domain.launch_profiles import (
    GitRemotePolicy,
    LaunchConfig,
    ResolvedLaunchProfile,
    builtin_launch_profiles,
)
from uxon.errors import fail
from uxon.infra import config_loader, git, identity, sessions_probe, tmux, version_probe


def _managed_create_cmd(req):
    assert req.managed is not None
    return req.managed.create_cmd


UXON_PATH = Path(uxon_cli.__file__).resolve()


def _launch_for_test(
    agents,
    *,
    default_profile: str = "claude",
    enabled_profiles: tuple[str, ...] = (),
    git_default: str = "",
    git_allowed: tuple[str, ...] = (),
) -> LaunchConfig:
    profiles = builtin_launch_profiles(agents)
    if default_profile in profiles and (git_default or git_allowed):
        profiles[default_profile] = dataclasses.replace(
            profiles[default_profile],
            git_remote=GitRemotePolicy(
                allowed_profiles=git_allowed or ((git_default,) if git_default else ()),
                default_profile=git_default,
            ),
        )
    return LaunchConfig(
        enabled_profiles=enabled_profiles,
        default_profile=default_profile if default_profile in profiles else "",
        profiles=profiles,
    )


def _resolved_for_test(
    cfg: Config,
    *,
    profile: str = "claude",
    launch_user: str = "dana_agent",
    mode: str = "normal",
) -> ResolvedLaunchProfile:
    return ResolvedLaunchProfile(
        profile=cfg.launch.profiles[profile],
        agent=cfg.agents[cfg.launch.profiles[profile].agent],
        launch_user=launch_user,
        mode_id=mode,
        git_remote=cfg.launch.profiles[profile].git_remote,
    )


def _catalog_with_default_args(agent_id: str, default_args: tuple[str, ...]):
    """Default catalog with one agent's ``default_args`` overridden — used by
    launch-argv tests that assert per-agent default args flow into the command.
    """
    import dataclasses

    from uxon.domain.agents import DEFAULT_AGENT_CATALOG

    out = dict(DEFAULT_AGENT_CATALOG)
    out[agent_id] = dataclasses.replace(out[agent_id], default_args=default_args)
    return out


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
        patcher = mock.patch("uxon.infra.identity.probe_cwd_writable", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_config(self, **overrides) -> Config:
        agents = overrides.get("agents")
        if agents is None:
            from uxon.domain.agents import default_agent_catalog

            agents = default_agent_catalog()
        old_default_agent = overrides.get("default_agent", "claude")
        old_enabled_agents = tuple(overrides.get("enabled_agents", ()))
        git_default = str(overrides.get("default_git_remote_profile", ""))
        git_allowed = tuple(
            profile.name
            for profile in overrides.get("git_remote_profiles", [])  # type: ignore[union-attr]
        )
        launch = overrides.get(
            "launch",
            _launch_for_test(
                agents,
                default_profile=old_default_agent,
                enabled_profiles=old_enabled_agents,
                git_default=git_default,
                git_allowed=git_allowed,
            ),
        )
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
            new_project_root="/srv/repos",
            repeat_noninteractive_mode="fail",
            tmux_socket_template="/tmp/uxon-{user}.sock",
            tui_refresh_interval_seconds=2.0,
            git_create_enabled=False,
            default_git_remote_profile="",
            git_remote_profiles=[],
            # Minimal baseline: tmux managed options OFF/empty so launch-argv
            # tests stay focused on the core invocation. The tmux tests opt in
            # explicitly; the on-by-default production value lives in
            # DEFAULT_CONFIG / the Config field defaults.
            tmux_manage_options=False,
            tmux_options={},
            tmux_server_options={},
            tmux_append_server_options={},
            agents=agents,
            launch=launch,
        )
        defaults.update(overrides)
        return Config(**defaults)

    def make_session(
        self,
        name: str,
        path: str,
        *,
        attached: str = "0",
    ) -> domain_session.SessionInfo:
        return domain_session.SessionInfo(
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
        with mock.patch("uxon.infra.identity.process_user", return_value="u-vz"):
            with mock.patch.dict(os.environ, {"SUDO_USER": "erin"}, clear=False):
                self.assertEqual(identity.resolve_caller_user(), "u-vz")

    def test_parse_args_supports_version_flags(self) -> None:
        parsed_long = parse_args(["--version"])
        self.assertEqual(parsed_long.action, "version")

        parsed_short = parse_args(["-V"])
        self.assertEqual(parsed_short.action, "version")

        parsed_subcommand = parse_args(["version"])
        self.assertEqual(parsed_subcommand.action, "version")

    def test_parse_args_supports_doctor(self) -> None:
        parsed = parse_args(["doctor"])
        self.assertEqual(parsed.action, "doctor")

    def test_parse_args_supports_kill_all_force(self) -> None:
        parsed = parse_args(["kill-all", "--force"])
        self.assertEqual(parsed.action, "kill-all")
        self.assertTrue(parsed.force)

    def _make_config_explicit(self, **kw) -> Config:
        """Make a Config with explicit fields (no make_config helper)."""
        agents = kw.get("agents")
        if agents is None:
            from uxon.domain.agents import default_agent_catalog

            agents = default_agent_catalog()
        old_default_agent = kw.get("default_agent", "claude")
        old_enabled_agents = tuple(kw.get("enabled_agents", ()))
        git_default = str(kw.get("default_git_remote_profile", ""))
        git_allowed = tuple(profile.name for profile in kw.get("git_remote_profiles", []))
        launch = kw.get(
            "launch",
            _launch_for_test(
                agents,
                default_profile=old_default_agent,
                enabled_profiles=old_enabled_agents,
                git_default=git_default,
                git_allowed=git_allowed,
            ),
        )
        return Config(
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
            new_project_root=kw.get("new_project_root", "/srv/agentdev"),
            repeat_noninteractive_mode=kw.get("repeat_noninteractive_mode", "fail"),
            tmux_socket_template=kw.get("tmux_socket_template", "/tmp/uxon-{user}.sock"),
            tui_refresh_interval_seconds=kw.get("tui_refresh_interval_seconds", 2.0),
            git_create_enabled=kw.get("git_create_enabled", False),
            default_git_remote_profile=kw.get("default_git_remote_profile", ""),
            git_remote_profiles=kw.get("git_remote_profiles", []),
            tmux_manage_options=kw.get("tmux_manage_options", False),
            tmux_options=kw.get("tmux_options", {}),
            tmux_server_options=kw.get("tmux_server_options", {}),
            tmux_append_server_options=kw.get("tmux_append_server_options", {}),
            agents=agents,
            launch=launch,
        )

    def test_resolve_launch_user_fixed_mode_uses_runtime_user(self) -> None:
        cfg = self._make_config_explicit(
            runtime_user="dana_agent", default_launch_mode="fixed", session_users=["dana_agent"]
        )
        self.assertEqual(identity.resolve_launch_user(cfg, "erin"), "dana_agent")

    def test_resolve_launch_user_caller_mode_uses_caller(self) -> None:
        cfg = self._make_config_explicit(
            runtime_user="dana_agent",
            default_launch_mode="caller",
            session_users=["dana_agent", "erin"],
        )
        self.assertEqual(identity.resolve_launch_user(cfg, "erin"), "erin")

    def test_resolve_launch_user_mapping_overrides_default(self) -> None:
        cfg = self._make_config_explicit(
            runtime_user="dana_agent",
            default_launch_mode="caller",
            enable_all_users_list=True,
            launch_user_by_caller={"erin": "dana_agent"},
            session_users=["dana_agent", "erin"],
        )
        self.assertEqual(identity.resolve_launch_user(cfg, "erin"), "dana_agent")

    def test_resolve_all_session_users_keeps_current_user_present(self) -> None:
        cfg = self._make_config_explicit(
            runtime_user="dana_agent",
            default_launch_mode="fixed",
            enable_all_users_list=True,
            session_users=["dana_agent"],
        )
        self.assertEqual(identity.resolve_all_session_users(cfg, "erin"), ["dana_agent", "erin"])

    def test_parse_args_supports_all_users_listing(self) -> None:
        parsed = parse_args(["list", "--all-users"])
        self.assertEqual(parsed.action, "list")
        self.assertTrue(parsed.all_users)

        parsed_short = parse_args(["-l", "--all-users"])
        self.assertEqual(parsed_short.action, "list")
        self.assertTrue(parsed_short.all_users)

    def test_parse_args_supports_repeat_mode_flags_for_new(self) -> None:
        parsed_attach = parse_args(["-n", "demo", "--attach-existing"])
        self.assertEqual(parsed_attach.action, "new")
        self.assertEqual(parsed_attach.repeat_mode, "attach")

        parsed_new = parse_args(["new", "demo", "--new-session"])
        self.assertEqual(parsed_new.action, "new")
        self.assertEqual(parsed_new.repeat_mode, "new")

    def _write_and_load_cfg(self, toml_content: str, tmpdir: str) -> Config:
        """Helper: write a config.toml in tmpdir and load_config from there."""
        tmp_path = Path(tmpdir)
        cwd = tmp_path / "workspace"
        cwd.mkdir(exist_ok=True)
        repo_cfg = tmp_path / "repo-config.toml"
        repo_cfg.write_text(toml_content, encoding="utf-8")

        def fake_load_toml(path: Path) -> dict[str, object]:
            if path == tmp_path / "config" / "config.toml":
                with repo_cfg.open("rb") as fh:
                    return config_loader.tomllib.load(fh)
            return {}

        with mock.patch.object(version_probe, "repo_root", return_value=tmp_path):
            with mock.patch("uxon.infra.config_loader.canonical", side_effect=lambda v: str(v)):
                with mock.patch("uxon.infra.config_loader.load_toml", side_effect=fake_load_toml):
                    return config_loader.load_config(str(cwd))

    def test_load_config_reads_new_multi_user_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._write_and_load_cfg(
                textwrap.dedent("""
                    runtime_user = "dana_agent"
                    default_launch_mode = "caller"
                    enable_all_users_list = true
                    session_users = ["dana_agent", "erin"]
                    allowed_roots = ["/srv", "/tmp"]
                    session_prefix = "uxon-"
                    repeat_noninteractive_mode = "attach"
                    tmux_socket_template = "/tmp/uxon-{user}-{uid}.sock"

                    [agents.claude]
                    default_args = ["--model", "sonnet"]

                    [launch]
                    enabled_profiles = ["claude"]
                    default_profile = "claude"

                    [launch_user_by_caller]
                    erin = "dana_agent"
                """).strip()
                + "\n",
                tmpdir,
            )

        self.assertEqual(cfg.runtime_user, "dana_agent")
        self.assertEqual(cfg.default_launch_mode, "caller")
        self.assertTrue(cfg.enable_all_users_list)
        self.assertEqual(cfg.session_users, ["dana_agent", "erin"])
        self.assertEqual(cfg.launch_user_by_caller, {"erin": "dana_agent"})
        self.assertEqual(cfg.agents["claude"].default_args, ("--model", "sonnet"))
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
        from uxon.infra import events as _events

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

    def test_load_config_reads_git_remote_profiles_and_launch_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._write_and_load_cfg(
                textwrap.dedent("""
                    git_create_enabled = true

                    [[git_remote_profiles]]
                    name = "vzd3v-gh"
                    host = "github.com"
                    owner = "vzd3v"
                    auth = "gh"
                    creds_user = "erin"
                    visibility = "private"

                    [[git_remote_profiles]]
                    name = "acme-tok"
                    host = "github.com"
                    owner = "acme"
                    auth = "token"
                    creds_user = "erin"
                    token_file = "/home/erin/.secrets/acme.token"

                    [launch.profiles.claude_work]
                    agent = "claude"
                    allowed_git_remote_profiles = ["vzd3v-gh", "acme-tok"]
                    default_git_remote_profile = "vzd3v-gh"

                    [launch]
                    enabled_profiles = ["claude_work"]
                    default_profile = "claude_work"
                """).strip()
                + "\n",
                tmpdir,
            )

        self.assertTrue(cfg.git_create_enabled)
        self.assertEqual(cfg.launch.profiles["claude_work"].git_remote.default_profile, "vzd3v-gh")
        self.assertEqual([p.name for p in cfg.git_remote_profiles], ["vzd3v-gh", "acme-tok"])
        self.assertEqual(cfg.git_remote_profiles[1].token_file, "/home/erin/.secrets/acme.token")

    def test_load_config_rejects_removed_top_level_default_git_remote_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit):
                self._write_and_load_cfg(
                    'default_git_remote_profile = "missing"\n',
                    tmpdir,
                )

    def test_load_config_reads_launch_profile_and_container_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._write_and_load_cfg(
                textwrap.dedent("""
                    [launch]
                    enabled_profiles = ["claude_sub1"]
                    default_profile = "claude_sub1"

                    [launch.profiles.claude_sub1]
                    agent = "claude"
                    display_name = "Claude subscription 1"
                    launch_user = "dana_agent"
                    container_profile = "claude_sub1"

                    [container.profiles.claude_sub1]
                    runtime_namespace = "per_user"
                    name_template = "{user}-{launch_profile}-{project_slug}"
                    exec_template = ["docker", "exec", "-w", "{dir}", "{name}"]
                """).strip()
                + "\n",
                tmpdir,
            )

        self.assertEqual(cfg.launch.enabled_profiles, ("claude_sub1",))
        self.assertEqual(cfg.launch.default_profile, "claude_sub1")
        self.assertEqual(cfg.launch.profiles["claude_sub1"].agent, "claude")
        self.assertEqual(cfg.launch.profiles["claude_sub1"].container_profile, "claude_sub1")
        self.assertEqual(cfg.container_profiles["claude_sub1"].runtime_namespace, "per_user")

    def test_load_config_rejects_tmux_unsafe_launch_profile_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit):
                self._write_and_load_cfg(
                    textwrap.dedent("""
                        [launch.profiles."bad.profile"]
                        agent = "claude"
                    """).strip()
                    + "\n",
                    tmpdir,
                )

    def test_load_config_rejects_tmux_unsafe_container_profile_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit):
                self._write_and_load_cfg(
                    textwrap.dedent("""
                        [container.profiles."bad-profile"]
                        runtime_namespace = "global"
                        name_template = "{project_slug}"
                        exec_template = ["docker", "exec", "{name}"]
                    """).strip()
                    + "\n",
                    tmpdir,
                )

    def test_load_config_rejects_container_placeholder_not_allowed_in_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit) as cm:
                self._write_and_load_cfg(
                    textwrap.dedent("""
                        [container.profiles.box]
                        runtime_namespace = "global"
                        name_template = "{name}"
                        exec_template = ["docker", "exec", "{name}"]
                    """).strip()
                    + "\n",
                    tmpdir,
                )
        self.assertIn("unsupported placeholder", getattr(cm.exception, "uxon_msg", ""))

    def test_load_config_rejects_non_string_container_profile_scalar(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit) as cm:
                self._write_and_load_cfg(
                    textwrap.dedent("""
                        [container.profiles.box]
                        runtime_namespace = "global"
                        name_template = 123
                        exec_template = ["docker", "exec", "{name}"]
                    """).strip()
                    + "\n",
                    tmpdir,
                )
        self.assertIn("name_template must be a string", getattr(cm.exception, "uxon_msg", ""))

    def test_load_config_rejects_stop_template_without_resolve_cmd(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit) as cm:
                self._write_and_load_cfg(
                    textwrap.dedent("""
                        [container.profiles.box]
                        runtime_namespace = "global"
                        name_template = "{project_slug}"
                        exec_template = ["docker", "exec", "{name}"]
                        stop_template = ["docker", "exec", "{name}", "kill", "$(cat {pidfile})"]
                    """).strip()
                    + "\n",
                    tmpdir,
                )
        self.assertIn("resolve_cmd", getattr(cm.exception, "uxon_msg", ""))

    def test_load_config_rejects_path_rule_git_default_after_intersection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit) as cm:
                self._write_and_load_cfg(
                    textwrap.dedent("""
                        [[git_remote_profiles]]
                        name = "work"
                        host = "github.com"
                        owner = "org"
                        auth = "gh"

                        [[git_remote_profiles]]
                        name = "personal"
                        host = "github.com"
                        owner = "me"
                        auth = "gh"

                        [launch]
                        enabled_profiles = ["claude_work"]

                        [launch.profiles.claude_work]
                        agent = "claude"
                        allowed_git_remote_profiles = ["work"]
                        default_git_remote_profile = "work"

                        [[launch.path_rules]]
                        path_prefix = "/srv/repos/app"
                        allowed_profiles = ["claude_work"]
                        default_profile = "claude_work"
                        allowed_git_remote_profiles = ["personal"]
                        default_git_remote_profile = "personal"
                    """).strip()
                    + "\n",
                    tmpdir,
                )
        self.assertIn("after git-remote policy intersection", getattr(cm.exception, "uxon_msg", ""))

    def test_load_config_rejects_relative_path_rule_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit) as cm:
                self._write_and_load_cfg(
                    textwrap.dedent("""
                        [launch]
                        enabled_profiles = ["claude"]

                        [[launch.path_rules]]
                        path_prefix = "relative/app"
                        allowed_profiles = ["claude"]
                    """).strip()
                    + "\n",
                    tmpdir,
                )
        self.assertIn("path_prefix must be an absolute path", getattr(cm.exception, "uxon_msg", ""))

    def test_load_config_rejects_non_normalized_path_rule_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit) as cm:
                self._write_and_load_cfg(
                    textwrap.dedent("""
                        [launch]
                        enabled_profiles = ["claude"]

                        [[launch.path_rules]]
                        path_prefix = "/srv/app/../secret"
                        allowed_profiles = ["claude"]
                    """).strip()
                    + "\n",
                    tmpdir,
                )
        self.assertIn("absolute normalized path", getattr(cm.exception, "uxon_msg", ""))

    def test_load_config_rejects_builtin_profile_override_in_auto_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit) as cm:
                self._write_and_load_cfg(
                    textwrap.dedent("""
                        [launch.profiles.claude]
                        agent = "claude"
                        launch_user = "dana_agent"
                    """).strip()
                    + "\n",
                    tmpdir,
                )
        self.assertIn(
            "overrides a shipped auto-mode profile", getattr(cm.exception, "uxon_msg", "")
        )

    def test_load_config_allows_builtin_profile_override_when_explicitly_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._write_and_load_cfg(
                textwrap.dedent("""
                    [launch]
                    enabled_profiles = ["claude"]

                    [launch.profiles.claude]
                    agent = "claude"
                    launch_user = "dana_agent"
                """).strip()
                + "\n",
                tmpdir,
            )
        self.assertEqual(cfg.launch.profiles["claude"].launch_user, "dana_agent")

    def test_load_config_rejects_removed_global_container_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit):
                self._write_and_load_cfg(
                    textwrap.dedent("""
                        [container]
                        enabled = true
                    """).strip()
                    + "\n",
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
            ctx = context_builder.build_tui_context(
                cfg, "dana_agent", "dana_agent", tmpdir, skeleton=True
            )
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
        self.assertEqual(cfg.agents["claude"].default_args, ())

    def test_load_config_empty_enabled_is_auto_mode(self) -> None:
        """``launch.enabled_profiles = []`` is equivalent to absent — auto-mode."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._write_and_load_cfg(
                textwrap.dedent("""
                    [launch]
                    enabled_profiles = []
                """).strip()
                + "\n",
                tmpdir,
            )
        self.assertEqual(cfg.enabled_agents, ())
        self.assertEqual(cfg.default_agent, "")
        self.assertEqual(cfg.launch.effective_enabled_profiles, ("claude", "codex", "cursor"))

    def test_load_config_multi_launch_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._write_and_load_cfg(
                textwrap.dedent("""
                    [agents.claude]
                    default_args = ["--verbose"]

                    [agents.cursor]
                    default_args = []

                    [launch]
                    enabled_profiles = ["claude", "cursor"]
                    default_profile = "cursor"
                """).strip()
                + "\n",
                tmpdir,
            )
        self.assertEqual(cfg.enabled_agents, ("claude", "cursor"))
        self.assertEqual(cfg.default_agent, "cursor")
        self.assertEqual(cfg.agents["claude"].default_args, ("--verbose",))

    def test_load_config_rejects_legacy_flat_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit):
                self._write_and_load_cfg(
                    'default_claude_args = ["--verbose"]\n',
                    tmpdir,
                )

    def test_load_config_rejects_removed_agents_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit):
                self._write_and_load_cfg(
                    '[agents]\nenabled = ["claude"]\n',
                    tmpdir,
                )

    def test_load_config_rejects_removed_agents_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit):
                self._write_and_load_cfg(
                    '[agents]\ndefault = "claude"\n',
                    tmpdir,
                )

    def test_parse_new_with_git_remote(self) -> None:
        parsed = parse_subcommand(
            ["new", "demo", "--git-remote", "prof-a", "--git-visibility", "public"]
        )
        self.assertEqual(parsed.action, "new")
        self.assertEqual(parsed.target_id, "demo")
        self.assertEqual(parsed.git_remote, "prof-a")
        self.assertEqual(parsed.git_visibility, "public")
        self.assertFalse(parsed.no_git)

    def test_parse_new_no_git(self) -> None:
        parsed = parse_subcommand(["new", "demo", "--no-git"])
        self.assertTrue(parsed.no_git)
        self.assertIsNone(parsed.git_remote)

    def test_parse_new_git_remote_default(self) -> None:
        parsed = parse_subcommand(["new", "demo", "--git-remote", "default"])
        self.assertEqual(parsed.git_remote, "default")

    def test_parse_new_rejects_git_remote_with_no_git(self) -> None:
        with self.assertRaises(SystemExit):
            parse_subcommand(["new", "demo", "--git-remote", "p", "--no-git"])

    def test_parse_new_rejects_bad_visibility(self) -> None:
        with self.assertRaises(SystemExit):
            parse_subcommand(["new", "demo", "--git-visibility", "secret"])

    def test_do_new_git_remote_dry_run_invokes_orchestrator(self) -> None:
        profile = {
            "name": "prof-a",
            "host": "github.com",
            "owner": "vzd3v",
            "auth": "gh",
            "creds_user": "erin",
            "visibility": "private",
        }
        from uxon.domain import git_profiles as uxon_git_profiles

        cfg = self.make_config(
            allowed_roots=["/srv/repos"],
            git_create_enabled=True,
            default_git_remote_profile="prof-a",
            git_remote_profiles=uxon_git_profiles.load_profiles([profile]),
        )
        args = ParsedArgs(
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
            from uxon.gitremote import create as uxon_git_create

            return uxon_git_create.CreationResult(
                profile_name=profile_arg.name,
                ssh_url=f"git@github.com:vzd3v/{repo_name}.git",
                commands=["would run: git init"],
            )

        from uxon.gitremote import create as uxon_git_create

        with mock.patch.object(uxon_git_create, "create_project_remote", side_effect=fake_create):
            with mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[]):
                with mock.patch("uxon.infra.tmux.launch_in_tmux", return_value=0):
                    with mock.patch("uxon.infra.identity.is_interactive_tty", return_value=False):
                        new_app.do_new(args, cfg, "dana_agent")

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "prof-a")
        self.assertEqual(calls[0]["repo"], "demo")
        self.assertEqual(calls[0]["dir"], "/srv/repos/demo")
        self.assertTrue(calls[0]["dry_run"])
        self.assertEqual(calls[0]["launch_user"], "dana_agent")

    def test_do_new_git_remote_rejects_disabled_feature(self) -> None:
        cfg = self.make_config(git_create_enabled=False)
        args = ParsedArgs(
            action="new",
            target_id="demo",
            git_remote="default",
            dry_run=True,
            agent_args=[],
        )
        with mock.patch("uxon.infra.identity.is_interactive_tty", return_value=False):
            with self.assertRaisesRegex(SystemExit, "2"):
                new_app.do_new(args, cfg, "dana_agent")

    def test_do_new_git_remote_with_worktree_fails(self) -> None:
        from uxon.domain import git_profiles as uxon_git_profiles

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
                        "creds_user": "erin",
                        "visibility": "private",
                    }
                ]
            ),
        )
        args = ParsedArgs(
            action="new",
            target_id="demo",
            worktree_branch="feature",
            git_remote="prof-a",
            dry_run=True,
            agent_args=[],
        )
        with mock.patch.object(new_app, "os", wraps=new_app.os) as m_os:
            m_os.path.isdir.return_value = True
            with mock.patch("uxon.infra.git.git_repo_root_as_user", return_value="/srv/repos/demo"):
                with self.assertRaises(SystemExit):
                    new_app.do_new(args, cfg, "dana_agent")

    def test_parse_run_rejects_git_flags(self) -> None:
        with self.assertRaises(SystemExit):
            parse_subcommand(["run", "--git-remote", "p"])
        with self.assertRaises(SystemExit):
            parse_subcommand(["run", "--no-git"])
        with self.assertRaises(SystemExit):
            parse_subcommand(["run", "--git-visibility", "private"])

    def test_format_version_reads_version_file_and_commit(self) -> None:
        with mock.patch("uxon.infra.version_probe.read_repo_version", return_value="0.2.0"):
            with mock.patch(
                "uxon.infra.version_probe.read_git_commit_short", return_value="abc1234"
            ):
                with mock.patch("uxon.infra.version_probe.repo_is_dirty", return_value=False):
                    self.assertEqual(format_version(), "uxon 0.2.0 (abc1234)")

    def test_format_version_marks_dirty_checkout(self) -> None:
        with mock.patch("uxon.infra.version_probe.read_repo_version", return_value="0.2.0"):
            with mock.patch(
                "uxon.infra.version_probe.read_git_commit_short", return_value="abc1234"
            ):
                with mock.patch("uxon.infra.version_probe.repo_is_dirty", return_value=True):
                    self.assertEqual(format_version(), "uxon 0.2.0 (abc1234-dirty)")

    def test_do_new_allows_call_from_outside_allowed_roots(self) -> None:
        cfg = self.make_config()
        args = ParsedArgs(action="new", target_id="demo", dry_run=True, agent_args=[])

        with mock.patch.object(os, "getcwd", return_value="/home/u-vz"):
            with mock.patch.object(
                new_app, "canonical", side_effect=lambda value: str(value), create=True
            ):
                with mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[]):
                    with mock.patch.object(
                        new_app, "allocate_session_name", return_value="uxon-demo"
                    ):
                        with mock.patch("uxon.infra.tmux.launch_in_tmux", return_value=0) as launch:
                            result = new_app.do_new(args, cfg, "u-vz")

        self.assertEqual(result, 0)
        launch.assert_called_once()

    def test_do_new_existing_session_defaults_to_attach_in_tty(self) -> None:
        cfg = self.make_config()
        args = ParsedArgs(action="new", target_id="demo", agent_args=[])
        existing = [self.make_session("uxon-demo@claude", "/srv/repos/demo")]

        with mock.patch.object(
            new_app, "canonical", side_effect=lambda value: str(value), create=True
        ):
            with mock.patch("uxon.infra.process.run_cmd") as run_cmd:
                with mock.patch(
                    "uxon.infra.sessions_probe.collect_sessions", return_value=existing
                ):
                    with mock.patch("uxon.infra.identity.is_interactive_tty", return_value=True):
                        with mock.patch("builtins.input", return_value=""):
                            with mock.patch.object(
                                attach_app, "attach_session", return_value=0
                            ) as attach:
                                with mock.patch(
                                    "uxon.infra.tmux.launch_in_tmux", return_value=0
                                ) as launch:
                                    with mock.patch(
                                        "uxon.infra.launch_records.create_pending_record"
                                    ) as create_pending:
                                        result = new_app.do_new(args, cfg, "u-vz")

        self.assertEqual(result, 0)
        run_cmd.assert_called_once()
        attach.assert_called_once()
        launch.assert_not_called()
        create_pending.assert_not_called()

    def test_do_new_existing_session_force_new_bypasses_prompt(self) -> None:
        cfg = self.make_config()
        args = ParsedArgs(action="new", target_id="demo", repeat_mode="new", agent_args=[])
        existing = [self.make_session("uxon-demo@claude", "/srv/repos/demo")]

        with mock.patch.object(
            new_app, "canonical", side_effect=lambda value: str(value), create=True
        ):
            with mock.patch("uxon.infra.process.run_cmd") as run_cmd:
                with mock.patch(
                    "uxon.infra.sessions_probe.collect_sessions", return_value=existing
                ):
                    with mock.patch.object(
                        new_app, "allocate_session_name", return_value="uxon-demo-2"
                    ) as allocate:
                        with mock.patch("uxon.infra.tmux.launch_in_tmux", return_value=0) as launch:
                            result = new_app.do_new(args, cfg, "u-vz")

        self.assertEqual(result, 0)
        run_cmd.assert_called_once()
        allocate.assert_called_once()
        launch.assert_called_once()

    def test_do_new_existing_session_without_tty_fails_with_guidance(self) -> None:
        cfg = self.make_config()
        args = ParsedArgs(action="new", target_id="demo", agent_args=[])
        existing = [self.make_session("uxon-demo@claude", "/srv/repos/demo")]

        with mock.patch.object(
            new_app, "canonical", side_effect=lambda value: str(value), create=True
        ):
            with mock.patch("uxon.infra.process.run_cmd") as run_cmd:
                with mock.patch(
                    "uxon.infra.sessions_probe.collect_sessions", return_value=existing
                ):
                    with mock.patch("uxon.infra.identity.is_interactive_tty", return_value=False):
                        with mock.patch("uxon.errors.eprint") as eprint:
                            with self.assertRaises(SystemExit) as ctx:
                                new_app.do_new(args, cfg, "u-vz")

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
        args = ParsedArgs(
            action="new", target_id="demo", worktree_branch="feature-x", agent_args=[]
        )
        wt = "/srv/repos/demo/.uxon/worktrees/feature-x"
        existing = [self.make_session("uxon-demo-feature-x@claude", wt)]

        with (
            mock.patch.object(os.path, "isdir", return_value=True),
            mock.patch("uxon.infra.identity.probe_cwd_writable", return_value=True),
            mock.patch("uxon.infra.git.git_repo_root_as_user", return_value="/srv/repos/demo"),
            mock.patch(
                "uxon.infra.git.git_common_dir_root_as_user", return_value="/srv/repos/demo"
            ),
            mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=existing),
            mock.patch("uxon.infra.identity.is_interactive_tty", return_value=True),
            mock.patch("builtins.input", return_value=""),
            mock.patch.object(attach_app, "attach_session", return_value=0) as attach,
            mock.patch.object(launch_app, "plan_worktree_launch") as plan,
        ):
            result = new_app.do_new(args, cfg, "u-vz")

        self.assertEqual(result, 0)
        attach.assert_called_once()
        plan.assert_not_called()  # attach decision → no worktree creation

    def test_do_new_existing_worktree_session_uses_configured_noninteractive_new(self) -> None:
        # Same §2.5 worktree-path compatibility root; with the noninteractive
        # mode forced to "new", the planner is invoked (creation now lives
        # inside plan_worktree_launch, not the old allocate + launch_in_tmux).
        cfg = self.make_config()
        cfg.repeat_noninteractive_mode = "new"
        args = ParsedArgs(
            action="new", target_id="demo", worktree_branch="feature-x", agent_args=[]
        )
        wt = "/srv/repos/demo/.uxon/worktrees/feature-x"
        existing = [self.make_session("uxon-demo-feature-x@claude", wt)]
        fake_req = attach_app._tui_launch_request_cls()(cmd=("true",), label="launch x")

        with (
            mock.patch.object(os.path, "isdir", return_value=True),
            mock.patch("uxon.infra.identity.probe_cwd_writable", return_value=True),
            mock.patch("uxon.infra.git.git_repo_root_as_user", return_value="/srv/repos/demo"),
            mock.patch(
                "uxon.infra.git.git_common_dir_root_as_user", return_value="/srv/repos/demo"
            ),
            mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=existing),
            mock.patch("uxon.infra.identity.is_interactive_tty", return_value=False),
            mock.patch.object(launch_app, "plan_worktree_launch", return_value=fake_req) as plan,
            mock.patch("uxon.infra.process.run_cmd"),
            mock.patch.object(os, "execvp", return_value=None) as execvp,
        ):
            result = new_app.do_new(args, cfg, "u-vz")

        self.assertEqual(result, 0)
        plan.assert_called_once()
        execvp.assert_called_once()

    def test_do_new_legacy_socket_guardrail_fails(self) -> None:
        cfg = self.make_config()
        args = ParsedArgs(action="new", target_id="demo", agent_args=[])
        legacy = [self.make_session("uxon-demo@claude", "/srv/repos/demo")]

        with mock.patch.object(
            new_app, "canonical", side_effect=lambda value: str(value), create=True
        ):
            with mock.patch("uxon.infra.process.run_cmd"):
                with mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[]):
                    with mock.patch(
                        "uxon.infra.sessions_probe.collect_sessions_for_user", return_value=legacy
                    ):
                        with mock.patch(
                            "uxon.infra.tmux.tmux_socket_path", return_value="/tmp/uxon-u-vz.sock"
                        ):
                            with mock.patch("uxon.errors.eprint") as eprint:
                                with self.assertRaises(SystemExit) as ctx:
                                    new_app.do_new(args, cfg, "u-vz")

        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("legacy default tmux socket", eprint.call_args[0][0])

    def test_resolve_repeat_decision_prefers_env_override(self) -> None:
        cfg = self.make_config()
        cfg.repeat_noninteractive_mode = "fail"
        session = self.make_session("uxon-demo", "/srv/repos/demo")

        with mock.patch("uxon.infra.identity.is_interactive_tty", return_value=False):
            with mock.patch.dict(
                os.environ, {"UXON_REPEAT_NONINTERACTIVE_POLICY": "attach"}, clear=False
            ):
                decision = repeat_app.resolve_repeat_decision(
                    "none" if False else None, cfg, "/srv/repos/demo", session, [session]
                )

        self.assertEqual(decision, "attach")

    def test_tmux_socket_path_expands_template(self) -> None:
        cfg = self.make_config()
        cfg.tmux_socket_template = "/tmp/uxon-{user}-{uid}.sock"

        with mock.patch.object(tmux.pwd, "getpwnam") as getpwnam:
            getpwnam.return_value = mock.Mock(pw_uid=1001)
            path = tmux.tmux_socket_path(cfg, "u-vz")

        self.assertEqual(path, "/tmp/uxon-u-vz-1001.sock")

    def test_doctor_reports_socket_and_config(self) -> None:
        from uxon.infra import agents as uxon_agents

        cfg = self.make_config()
        output = io.StringIO()
        ok_avail = uxon_agents.AgentAvailability(status="ok", version="1.2.3")

        from uxon.domain.host_report import BinaryStatus, HostReport

        host_report = HostReport(
            tmux=BinaryStatus("tmux", "/usr/bin/tmux", "apt"),
            agents={
                "claude": BinaryStatus("claude", "/usr/local/bin/claude", "npm"),
                "codex": BinaryStatus("codex", None, ""),
                "cursor": BinaryStatus("cursor-agent", None, ""),
            },
            launch_user="u-vz",
        )

        with mock.patch(
            "uxon.infra.config_loader.resolve_config_layers",
            return_value=({}, [Path("/srv/apps/uxon/config/config.toml")]),
        ):
            with mock.patch("uxon.infra.tmux.tmux_socket_path", return_value="/tmp/uxon-u-vz.sock"):
                with mock.patch("uxon.infra.probes.probe_host", return_value=host_report):
                    with mock.patch.object(uxon_agents, "_probe_one", return_value=ok_avail):
                        with mock.patch(
                            "uxon.infra.sessions_probe.collect_sessions",
                            return_value=[self.make_session("uxon-demo@claude", "/srv/repos/demo")],
                        ):
                            with mock.patch(
                                "uxon.infra.sessions_probe.collect_sessions_for_user",
                                return_value=[],
                            ):
                                with mock.patch.object(
                                    doctor_app, "user_can_write_dir", return_value=True
                                ):
                                    with mock.patch(
                                        "uxon.infra.version_probe.format_version",
                                        return_value="uxon 0.4.0 (abc1234)",
                                    ):
                                        with mock.patch("sys.stdout", output):
                                            rc = doctor_app.do_doctor(
                                                cfg, "erin", "u-vz", "/srv/repos/demo"
                                            )

        self.assertEqual(rc, 0)
        rendered = output.getvalue()
        self.assertIn("uxon doctor", rendered)
        self.assertIn("config_paths=/srv/apps/uxon/config/config.toml", rendered)
        self.assertIn("tmux_socket=/tmp/uxon-u-vz.sock", rendered)
        self.assertIn("claude:", rendered)
        self.assertIn("ok (1.2.3)", rendered)

    def test_doctor_reports_missing_agent(self) -> None:
        from uxon.domain.host_report import BinaryStatus, HostReport

        cfg = self.make_config()
        output = io.StringIO()
        host_report = HostReport(
            tmux=BinaryStatus("tmux", "/usr/bin/tmux", "apt"),
            agents={
                "claude": BinaryStatus("claude", None, "npm install ..."),
                "codex": BinaryStatus("codex", None, ""),
                "cursor": BinaryStatus("cursor-agent", None, ""),
            },
            launch_user="u-vz",
        )

        with mock.patch("uxon.infra.config_loader.resolve_config_layers", return_value=({}, [])):
            with mock.patch("uxon.infra.tmux.tmux_socket_path", return_value="/tmp/uxon-u-vz.sock"):
                with mock.patch("uxon.infra.probes.probe_host", return_value=host_report):
                    with mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[]):
                        with mock.patch(
                            "uxon.infra.sessions_probe.collect_sessions_for_user", return_value=[]
                        ):
                            with mock.patch.object(
                                doctor_app, "user_can_write_dir", return_value=True
                            ):
                                with mock.patch(
                                    "uxon.infra.version_probe.format_version",
                                    return_value="uxon 0.4.0",
                                ):
                                    with mock.patch("sys.stdout", output):
                                        rc = doctor_app.do_doctor(
                                            cfg, "u-vz", "u-vz", "/srv/repos/demo"
                                        )

        rendered = output.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("MISSING", rendered)
        self.assertIn("claude:", rendered)

    def test_do_kill_all_requires_force_without_tty(self) -> None:
        cfg = self.make_config()
        args = ParsedArgs(action="kill-all", force=False)
        sessions = [self.make_session("uxon-demo", "/srv/repos/demo")]

        with mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=sessions):
            with mock.patch("uxon.infra.identity.is_interactive_tty", return_value=False):
                with mock.patch("uxon.errors.eprint") as eprint:
                    with self.assertRaises(SystemExit) as ctx:
                        kill_app.do_kill_all(args, cfg, "u-vz")

        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("--force", eprint.call_args[0][0])

    def _stub_socket_path(self):
        # Classic tests run as if the process is NOT inside tmux so the
        # build_request helpers stay on the execvp / attach-session /
        # new-session path.
        return _StubsChain(
            mock.patch("uxon.infra.tmux.tmux_socket_path", return_value="/tmp/uxon-test.sock"),
            mock.patch("uxon.infra.tmux.tmux_host_socket", return_value=None),
        )

    def test_build_tmux_attach_request_produces_expected_argv(self) -> None:
        cfg = self.make_config()
        target = self.make_session("uxon-demo", "/srv/repos/demo")
        with self._stub_socket_path():
            req = tmux._build_tmux_attach_request(target, cfg, "u-vz")
        self.assertIn("attach-session", req.cmd)
        self.assertIn("uxon-demo", req.cmd)
        self.assertEqual(req.prelaunch, ())
        self.assertIn("attach", req.label)

    def test_build_tmux_launch_request_includes_claude_and_mkdir(self) -> None:
        cfg = self.make_config(agents=_catalog_with_default_args("claude", ("--model", "sonnet")))
        args = ParsedArgs(action="run", permission_mode="yolo", agent_args=["--foo"])
        resolved = _resolved_for_test(cfg, mode="yolo")
        with self._stub_socket_path():
            req = tmux._build_tmux_launch_request(
                "/srv/repos/demo",
                "uxon-demo@claude",
                args,
                cfg,
                None,
                resolved_profile=resolved,
            )
        create = _managed_create_cmd(req)
        self.assertIn("new-session", create)
        self.assertIn("-d", create)
        self.assertNotIn("-As", create)
        self.assertIn("uxon-demo@claude", create)
        # per-agent default_args + yolo flag + caller's agent_args all flow through
        self.assertIn("uxon.infra.launch_bootstrap", create)
        self.assertIn("claude", create)
        self.assertIn("--model", create)
        self.assertIn("sonnet", create)
        self.assertIn("--dangerously-skip-permissions", create)
        self.assertIn("--foo", create)
        # prelaunch mkdir for the socket parent
        self.assertEqual(len(req.prelaunch), 1)
        pre = req.prelaunch[0]
        self.assertIn("mkdir", pre)
        self.assertIn("-p", pre)

    def test_managed_launch_create_argv_is_non_adopting(self) -> None:
        cfg = self.make_config()
        args = ParsedArgs(action="run", permission_mode="yolo")
        resolved = _resolved_for_test(cfg, mode="yolo")
        with self._stub_socket_path():
            req = tmux._build_tmux_launch_request(
                "/srv/repos/demo",
                "uxon-demo@claude",
                args,
                cfg,
                None,
                resolved_profile=resolved,
            )
        create = list(_managed_create_cmd(req))
        self.assertIn("new-session", create)
        self.assertNotIn("-A", create)
        self.assertNotIn("-As", create)
        self.assertNotIn("-dA", create)

    def test_managed_launch_exports_record_dir_for_bootstrap(self) -> None:
        cfg = self.make_config()
        args = ParsedArgs(action="run", permission_mode="yolo")
        resolved = _resolved_for_test(cfg, mode="yolo")
        with (
            self._stub_socket_path(),
            mock.patch("uxon.infra.launch_records.state_dir", return_value=Path("/tmp/records")),
        ):
            req = tmux._build_tmux_launch_request(
                "/srv/repos/demo",
                "uxon-demo@claude",
                args,
                cfg,
                None,
                resolved_profile=resolved,
            )
        create = list(_managed_create_cmd(req))
        self.assertIn("UXON_LAUNCH_RECORD_DIR=/tmp/records", create)

    def test_build_tmux_launch_request_requires_resolved_profile(self) -> None:
        cfg = self.make_config()
        args = ParsedArgs(action="run")
        with self.assertRaises(SystemExit) as cm:
            tmux._build_tmux_launch_request("/srv/repos/demo", "uxon-demo@claude", args, cfg, None)
        self.assertIn("launch profile must be resolved", getattr(cm.exception, "uxon_msg", ""))

    def test_attach_session_cli_still_calls_execvp(self) -> None:
        cfg = self.make_config()
        target = self.make_session("uxon-demo", "/srv/repos/demo")
        with self._stub_socket_path():
            with mock.patch.object(attach_app.os, "execvp") as execvp:
                attach_app.attach_session(target, cfg, "u-vz")
        execvp.assert_called_once()
        argv = execvp.call_args[0][1]
        self.assertIn("attach-session", argv)
        self.assertIn("uxon-demo", argv)

    def test_attach_request_is_not_managed_launch(self) -> None:
        cfg = self.make_config()
        target = self.make_session("uxon-demo", "/srv/repos/demo")
        with self._stub_socket_path():
            req = tmux._build_tmux_attach_request(target, cfg, "u-vz")
        self.assertIsNone(req.managed)

    def test_launch_in_tmux_cli_still_calls_execvp_after_mkdir(self) -> None:
        cfg = self.make_config()
        args = ParsedArgs(action="run", agent_args=[])
        resolved = _resolved_for_test(cfg)
        with self._stub_socket_path():
            with mock.patch("uxon.infra.process.run_cmd") as run_cmd:
                with mock.patch("uxon.infra.tmux.prepare_managed_launch") as prepare:
                    with mock.patch.object(os, "execvp") as execvp:
                        tmux.launch_in_tmux(
                            "/srv/repos/demo",
                            "uxon-demo",
                            args,
                            cfg,
                            None,
                            resolved_profile=resolved,
                        )
        run_cmd.assert_called_once()
        prepare.assert_called_once()
        execvp.assert_called_once()

    def test_managed_launch_finalization_failure_kills_created_session(self) -> None:
        from uxon.infra import launch_records

        pending = launch_records.PendingLaunchRecord(
            socket_path="/tmp/uxon-test.sock",
            session_name="uxon-demo@claude",
            launch_nonce="nonce",
            launch_profile="claude",
            agent="claude",
            launch_user="dana_agent",
        )
        managed = tmux.ManagedTmuxLaunch(
            create_cmd=("tmux", "new-session", "-d", "-s", pending.session_name),
            query_cmd=("tmux", "display-message", "-p"),
            kill_cmd=("tmux", "kill-session", "-t", pending.session_name),
            record_socket=pending.socket_path,
            record_session=pending.session_name,
            record_nonce=pending.launch_nonce,
            record_dir="/tmp/uxon-launch-records-test",
            launch_profile=pending.launch_profile,
            agent=pending.agent,
            launch_user=pending.launch_user,
        )
        req = tmux.LaunchRequest(
            cmd=("tmux", "attach-session", "-t", pending.session_name), managed=managed
        )

        class CP:
            def __init__(self, returncode=0, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        calls: list[tuple[str, ...]] = []

        def fake_run_cmd(cmd, check=True):
            calls.append(tuple(cmd))
            if len(calls) == 2:
                return CP(stdout="$1\t123\tuxon-demo@claude\tnonce\n")
            return CP()

        with (
            mock.patch("uxon.infra.launch_records.create_pending_record"),
            mock.patch(
                "uxon.infra.launch_records.finalize_pending_record",
                side_effect=RuntimeError("boom"),
            ),
            mock.patch("uxon.infra.launch_records.fail_pending_record") as fail_pending,
            mock.patch("uxon.infra.process.run_cmd", fake_run_cmd),
        ):
            with self.assertRaises(RuntimeError):
                tmux.prepare_managed_launch(req, pending)

        self.assertIn(managed.kill_cmd, calls)
        fail_pending.assert_called_once_with(
            pending, override_dir=Path("/tmp/uxon-launch-records-test")
        )

    def test_managed_launch_name_conflict_fails_pending_record(self) -> None:
        from uxon.infra import launch_records

        pending = launch_records.PendingLaunchRecord(
            socket_path="/tmp/uxon-test.sock",
            session_name="uxon-demo@claude",
            launch_nonce="nonce",
            launch_profile="claude",
            agent="claude",
            launch_user="dana_agent",
        )
        managed = tmux.ManagedTmuxLaunch(
            create_cmd=("tmux", "new-session", "-d", "-s", pending.session_name),
            query_cmd=("tmux", "display-message", "-p"),
            kill_cmd=("tmux", "kill-session", "-t", pending.session_name),
            record_socket=pending.socket_path,
            record_session=pending.session_name,
            record_nonce=pending.launch_nonce,
            record_dir="/tmp/uxon-launch-records-test",
            launch_profile=pending.launch_profile,
            agent=pending.agent,
            launch_user=pending.launch_user,
        )
        req = tmux.LaunchRequest(
            cmd=("tmux", "attach-session", "-t", pending.session_name), managed=managed
        )

        class CP:
            returncode = 1
            stdout = ""
            stderr = "duplicate session"

        with (
            mock.patch("uxon.infra.launch_records.create_pending_record"),
            mock.patch("uxon.infra.launch_records.fail_pending_record") as fail_pending,
            mock.patch("uxon.infra.process.run_cmd", return_value=CP()),
        ):
            with self.assertRaises(SystemExit):
                tmux.prepare_managed_launch(req, pending)
        fail_pending.assert_called_once_with(
            pending, override_dir=Path("/tmp/uxon-launch-records-test")
        )

    def test_launch_bootstrap_waits_for_finalized_record_before_exec(self) -> None:
        from uxon.infra import launch_bootstrap

        with (
            mock.patch("uxon.infra.launch_records.wait_for_finalized_record", return_value=None),
            mock.patch.object(launch_bootstrap.os, "execvp") as execvp,
        ):
            rc = launch_bootstrap.wait_then_exec(
                socket_path="/tmp/sock",
                session_name="uxon-demo@claude",
                launch_nonce="nonce",
                agent_argv=["claude"],
                timeout_seconds=0.01,
            )
        self.assertEqual(rc, 124)
        execvp.assert_not_called()

    def test_finalized_launch_record_contains_authority_fields(self) -> None:
        from uxon.infra import launch_records

        with tempfile.TemporaryDirectory() as tmp:
            pending = launch_records.PendingLaunchRecord(
                socket_path="/tmp/uxon-test.sock",
                session_name="uxon-demo@claude",
                launch_nonce="nonce",
                launch_profile="claude",
                agent="claude",
                launch_user="dana_agent",
                container_profile="box",
                container_profile_fingerprint="fp",
                container="box-demo",
            )
            meta = launch_records.TmuxSessionMetadata(
                session_id="$1",
                created="123",
                name=pending.session_name,
                launch_nonce=pending.launch_nonce,
            )
            with mock.patch(
                "uxon.infra.launch_records._uid_for_user", return_value=os.geteuid() + 1
            ):
                launch_records.create_pending_record(pending, override_dir=Path(tmp) / "records")
                launch_records.finalize_pending_record(
                    pending,
                    meta,
                    container_id="cid",
                    container_cgroup="/x.slice",
                    container_epoch="1000",
                    override_dir=Path(tmp) / "records",
                )
            record_path = launch_records.record_path(pending, override_dir=Path(tmp) / "records")
            self.assertEqual(stat.S_IMODE((Path(tmp) / "records").stat().st_mode), 0o711)
            self.assertEqual(stat.S_IMODE(record_path.stat().st_mode), 0o644)
            payload = launch_records.read_finalized_record(
                pending.socket_path,
                pending.session_name,
                pending.launch_nonce,
                override_dir=Path(tmp) / "records",
            )
        assert payload is not None
        self.assertEqual(payload["launch_profile"], "claude")
        self.assertEqual(payload["agent"], "claude")
        self.assertEqual(payload["launch_user"], "dana_agent")
        self.assertEqual(payload["container_profile"], "box")
        self.assertEqual(payload["container_profile_fingerprint"], "fp")
        self.assertEqual(payload["container"], "box-demo")
        self.assertEqual(payload["container_id"], "cid")
        self.assertEqual(payload["container_cgroup"], "/x.slice")
        self.assertEqual(payload["container_epoch"], "1000")

    def test_launch_record_lookup_does_not_trust_env_markers(self) -> None:
        from uxon.infra import launch_records

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {
                    "UXON_LAUNCH_PROFILE": "claude",
                    "UXON_LAUNCH_NONCE": "env-nonce",
                    "UXON_AGENT": "claude",
                },
                clear=False,
            ):
                payload = launch_records.read_finalized_record(
                    "/tmp/uxon-test.sock",
                    "uxon-demo@claude",
                    "env-nonce",
                    override_dir=Path(tmp) / "records",
                )
        self.assertIsNone(payload)

    def test_load_config_does_not_open_project_uxon_toml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cwd = root / "project" / "sub"
            cwd.mkdir(parents=True)
            project_cfg = root / "project" / ".uxon.toml"
            project_cfg.write_text('[agents]\ndefault = "codex"\n', encoding="utf-8")

            opened: list[Path] = []
            original_open = Path.open

            def spy_open(path_self: Path, *args, **kwargs):
                opened.append(path_self)
                return original_open(path_self, *args, **kwargs)

            with mock.patch.object(version_probe, "repo_root", return_value=root):
                with mock.patch.object(Path, "open", spy_open):
                    cfg = config_loader.load_config(str(cwd))

        self.assertEqual(cfg.default_agent, "")
        self.assertNotIn(project_cfg, opened)

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
            launch_app.is_launch_target_allowed(cfg, "u-ed", "/no/such/dir/here"),
        )
        with self.assertRaises(SystemExit):
            launch_app.ensure_launch_target_allowed(cfg, "u-ed", "/no/such/dir/here")

    def test_launch_target_rejects_unwritable_directory(self) -> None:
        cfg = self.make_config(allowed_roots=[])
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("uxon.infra.identity.probe_cwd_writable", return_value=False):
                self.assertFalse(launch_app.is_launch_target_allowed(cfg, "u-ed", tmp))
                with self.assertRaises(SystemExit):
                    launch_app.ensure_launch_target_allowed(cfg, "u-ed", tmp)

    def test_launch_target_writable_passes_when_allowed_roots_empty(self) -> None:
        cfg = self.make_config(allowed_roots=[])
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("uxon.infra.identity.probe_cwd_writable", return_value=True):
                self.assertTrue(launch_app.is_launch_target_allowed(cfg, "u-ed", tmp))
                # ensure_… is the raise-on-failure variant; passing case
                # must not raise.
                launch_app.ensure_launch_target_allowed(cfg, "u-ed", tmp)

    def test_launch_target_strict_whitelist_when_allowed_roots_set(self) -> None:
        cfg = self.make_config(allowed_roots=["/srv/repos"])
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("uxon.infra.identity.probe_cwd_writable", return_value=True):
                # Writable but outside the whitelist → fail.
                # ``is_under_allowed_roots`` (and its ``is_under`` call) now live
                # in ``uxon.domain.authz`` — patch the consuming module's binding.
                with mock.patch("uxon.domain.authz.is_under", return_value=False):
                    self.assertFalse(launch_app.is_launch_target_allowed(cfg, "u-ed", tmp))
                    with self.assertRaises(SystemExit):
                        launch_app.ensure_launch_target_allowed(cfg, "u-ed", tmp)
                # Writable and inside the whitelist → pass.
                with mock.patch("uxon.domain.authz.is_under", return_value=True):
                    self.assertTrue(launch_app.is_launch_target_allowed(cfg, "u-ed", tmp))
                    launch_app.ensure_launch_target_allowed(cfg, "u-ed", tmp)

    def test_launch_target_no_home_implicit_when_allowed_roots_set(self) -> None:
        # Regression guard for the old behaviour where the launch user's
        # $HOME was silently appended to allowed_roots: a writable dir
        # outside the whitelist must NOT pass when allowed_roots is set.
        cfg = self.make_config(allowed_roots=["/srv/repos"])
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("uxon.infra.identity.probe_cwd_writable", return_value=True):
                self.assertFalse(launch_app.is_launch_target_allowed(cfg, "u-ed", tmp))
                with self.assertRaises(SystemExit):
                    launch_app.ensure_launch_target_allowed(cfg, "u-ed", tmp)

    # ── TUI callback error surfacing (0.10.3) ────────────────────────

    def test_sanitize_callback_stderr_strips_ccw_prefix_and_list_indent(self) -> None:
        raw = (
            "uxon: directory must be under one of:\n"
            "uxon:   - /srv/repos\n"
            "uxon:   - /home/u-ed\n"
            "uxon: got: /tmp\n"
        )
        expected = "directory must be under one of:\n  - /srv/repos\n  - /home/u-ed\ngot: /tmp"
        self.assertEqual(callback_wrap._sanitize_callback_stderr(raw), expected)

    def test_sanitize_callback_stderr_passes_through_non_uxon_lines(self) -> None:
        raw = "random warning\nuxon: the real error\n\n"
        self.assertEqual(
            callback_wrap._sanitize_callback_stderr(raw),
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
            entries = context_builder._list_existing_projects(str(root))
        names = [n for n, _ in entries]
        self.assertEqual(names, ["alpha", "beta"])
        for _, mtime in entries:
            # Either HH:MM (today) or MM-DD. Never blank on a fresh mkdir.
            self.assertRegex(mtime, r"^\d\d[:-]\d\d$")

    def test_list_existing_projects_missing_root_returns_empty(self) -> None:
        self.assertEqual(
            context_builder._list_existing_projects("/nonexistent/path/for/uxon/test"), []
        )

    def test_wrap_tui_callback_passes_return_value(self) -> None:
        class _Err(Exception):
            pass

            # pragma: no cover — marker only

        wrapped = callback_wrap._wrap_tui_callback(lambda x, y: x + y, _Err)
        self.assertEqual(wrapped(2, 3), 5)

    def test_wrap_tui_callback_captures_fail_message(self) -> None:
        class _Err(Exception):
            pass

        def inner() -> None:
            fail("directory must be under one of:\nccw:   - /srv/repos")

        wrapped = callback_wrap._wrap_tui_callback(inner, _Err)
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

        wrapped = callback_wrap._wrap_tui_callback(inner, _Err)
        with self.assertRaises(_Err) as cm:
            wrapped()
        self.assertIn("7", str(cm.exception))

    # ── tmux nesting detection ───────────────────────────────────────

    def test_tmux_host_socket_returns_none_without_env(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(tmux.tmux_host_socket())

    def test_tmux_host_socket_parses_socket_from_tmux_env(self) -> None:
        env = {"TMUX": "/tmp/uxon-u-vz.sock,12345,0"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(tmux.tmux_host_socket(), "/tmp/uxon-u-vz.sock")

    def test_tmux_nesting_mode_execvp_when_not_in_tmux(self) -> None:
        with mock.patch("uxon.infra.tmux.tmux_host_socket", return_value=None):
            self.assertEqual(tmux.tmux_nesting_mode("/tmp/uxon-u-vz.sock"), "execvp")

    def test_tmux_nesting_mode_switch_when_same_socket(self) -> None:
        with mock.patch("uxon.infra.tmux.tmux_host_socket", return_value="/tmp/uxon-u-vz.sock"):
            self.assertEqual(tmux.tmux_nesting_mode("/tmp/uxon-u-vz.sock"), "switch")

    def test_tmux_nesting_mode_fails_when_foreign_socket(self) -> None:
        with mock.patch("uxon.infra.tmux.tmux_host_socket", return_value="/tmp/tmux-1000/default"):
            with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                with self.assertRaises(SystemExit) as ctx:
                    tmux.tmux_nesting_mode("/tmp/uxon-u-vz.sock")
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertIn("different socket", stderr.getvalue())

    def test_build_tmux_attach_request_uses_switch_client_when_nested(self) -> None:
        cfg = self.make_config()
        target = self.make_session("uxon-demo", "/srv/repos/demo")
        stubs = _StubsChain(
            mock.patch("uxon.infra.tmux.tmux_socket_path", return_value="/tmp/uxon-test.sock"),
            mock.patch("uxon.infra.tmux.tmux_host_socket", return_value="/tmp/uxon-test.sock"),
        )
        with stubs:
            req = tmux._build_tmux_attach_request(target, cfg, "u-vz")
        self.assertIn("switch-client", req.cmd)
        self.assertNotIn("attach-session", req.cmd)
        self.assertIn("uxon-demo", req.cmd)
        self.assertEqual(req.prelaunch, ())
        self.assertIn("switch-client", req.label)

    def test_build_tmux_launch_request_uses_switch_client_when_nested(self) -> None:
        cfg = self.make_config(agents=_catalog_with_default_args("claude", ("--model", "sonnet")))
        args = ParsedArgs(action="run", permission_mode="yolo", agent_args=["--foo"])
        resolved = _resolved_for_test(cfg, mode="yolo")
        stubs = _StubsChain(
            mock.patch("uxon.infra.tmux.tmux_socket_path", return_value="/tmp/uxon-test.sock"),
            mock.patch("uxon.infra.tmux.tmux_host_socket", return_value="/tmp/uxon-test.sock"),
        )
        with stubs:
            req = tmux._build_tmux_launch_request(
                "/srv/repos/demo",
                "uxon-demo@claude",
                args,
                cfg,
                None,
                resolved_profile=resolved,
            )
        # Main cmd is the switch; creation happens in prelaunch.
        self.assertIn("switch-client", req.cmd)
        self.assertIn("uxon-demo@claude", req.cmd)
        # Prelaunch only creates the socket parent; detached creation is
        # finalized by the managed launch preparation.
        self.assertEqual(len(req.prelaunch), 1)
        (mkdir_pre,) = req.prelaunch
        create_pre = _managed_create_cmd(req)
        self.assertIn("mkdir", mkdir_pre)
        self.assertIn("new-session", create_pre)
        self.assertIn("-d", create_pre)
        self.assertNotIn("-dA", create_pre)
        self.assertIn("uxon-demo@claude", create_pre)
        self.assertIn("claude", create_pre)
        self.assertIn("--dangerously-skip-permissions", create_pre)
        self.assertIn("--foo", create_pre)
        self.assertIn("--model", create_pre)
        self.assertIn("sonnet", create_pre)
        self.assertIn("nested", req.label)

    # ── tmux managed options (3.5.0) ─────────────────────────────────

    _RECO_EXPECTED_CHAIN = [
        "set",
        "-g",
        "mouse",
        "on",
        ";",
        "set",
        "-g",
        "allow-passthrough",
        "on",
        ";",
        "set",
        "-s",
        "extended-keys",
        "on",
        ";",
        "set",
        "-as",
        "terminal-features",
        "xterm*:extkeys",
        ";",
    ]

    # The chain when the server is ALREADY live (D9): -g/-s are re-asserted
    # (idempotent), the -as append scope is dropped to avoid list growth.
    _RECO_LIVE_CHAIN = [
        "set",
        "-g",
        "mouse",
        "on",
        ";",
        "set",
        "-g",
        "allow-passthrough",
        "on",
        ";",
        "set",
        "-s",
        "extended-keys",
        "on",
        ";",
    ]

    def _reco_cfg(self):
        reco = domain_config.RECOMMENDED_TMUX_OPTIONS
        return self.make_config(
            tmux_manage_options=True,
            tmux_options=dict(reco["options"]),
            tmux_server_options=dict(reco["server_options"]),
            tmux_append_server_options=dict(reco["append_server_options"]),
        )

    def test_tmux_opt_value_renders_scalars(self) -> None:
        self.assertEqual(tmux._tmux_opt_value(True), "on")
        self.assertEqual(tmux._tmux_opt_value(False), "off")
        self.assertEqual(tmux._tmux_opt_value(5), "5")
        self.assertEqual(tmux._tmux_opt_value("xterm*:extkeys"), "xterm*:extkeys")

    def test_tmux_set_chain_empty_when_disabled(self) -> None:
        cfg = self.make_config(
            tmux_options={"mouse": "on"},  # populated but toggle off
        )
        self.assertEqual(tmux._tmux_set_chain(cfg), [])

    def test_tmux_set_chain_empty_when_enabled_but_no_options(self) -> None:
        cfg = self.make_config(tmux_manage_options=True)
        self.assertEqual(tmux._tmux_set_chain(cfg), [])

    def test_tmux_set_chain_order_and_scopes(self) -> None:
        # global -> server -> append-server; declaration order within a table;
        # bool -> on/off; one bare ';' per set command (AC2/AC4/D2).
        cfg = self.make_config(
            tmux_manage_options=True,
            tmux_options={"mouse": True, "allow-passthrough": "on"},
            tmux_server_options={"extended-keys": "on"},
            tmux_append_server_options={"terminal-features": "xterm*:extkeys"},
        )
        self.assertEqual(tmux._tmux_set_chain(cfg), self._RECO_EXPECTED_CHAIN)

    def test_recommended_constant_renders_d3_set(self) -> None:
        # AC5: the shipped recommended set maps to the four documented
        # options with the correct scopes/order.
        self.assertEqual(tmux._tmux_set_chain(self._reco_cfg()), self._RECO_EXPECTED_CHAIN)

    def test_tmux_set_chain_live_server_drops_append_scope(self) -> None:
        # D9: on a live server, -g/-s are re-asserted but -as is dropped
        # (append is non-idempotent → would grow the list).
        cfg = self._reco_cfg()
        self.assertEqual(tmux._tmux_set_chain(cfg, server_running=True), self._RECO_LIVE_CHAIN)
        # birth still emits the full chain including -as
        self.assertEqual(tmux._tmux_set_chain(cfg, server_running=False), self._RECO_EXPECTED_CHAIN)

    def test_build_launch_request_live_server_omits_append(self) -> None:
        # A launch into an already-running server re-asserts -g/-s, no -as.
        cfg = self._reco_cfg()
        args = ParsedArgs(action="run", permission_mode="yolo")
        resolved = _resolved_for_test(cfg, mode="yolo")
        with self._stub_socket_path():
            req = tmux._build_tmux_launch_request(
                "/srv/repos/demo",
                "uxon-demo@claude",
                args,
                cfg,
                None,
                resolved_profile=resolved,
                server_running=True,
            )
        cmd = list(_managed_create_cmd(req))
        self.assertIn("set", cmd)
        self.assertNotIn("-as", cmd)  # append scope dropped on a live server
        idx = cmd.index("set")
        self.assertEqual(cmd[idx : idx + len(self._RECO_LIVE_CHAIN)], self._RECO_LIVE_CHAIN)
        self.assertLess(max(i for i, t in enumerate(cmd) if t == "set"), cmd.index("new-session"))

    def test_build_attach_request_reasserts_g_s_scopes(self) -> None:
        # Attaching to an existing session re-asserts -g/-s (server is live),
        # never -as; works for both the classic and nested branches.
        cfg = self._reco_cfg()
        target = self.make_session("uxon-demo", "/srv/repos/demo")
        with self._stub_socket_path():
            req = tmux._build_tmux_attach_request(target, cfg, "u-vz")
        cmd = list(req.cmd)
        self.assertIn("attach-session", cmd)
        self.assertNotIn("-as", cmd)
        idx = cmd.index("set")
        self.assertEqual(cmd[idx : idx + len(self._RECO_LIVE_CHAIN)], self._RECO_LIVE_CHAIN)
        self.assertLess(
            max(i for i, t in enumerate(cmd) if t == "set"), cmd.index("attach-session")
        )

    def test_build_attach_request_no_chain_when_disabled(self) -> None:
        # managed options off → attach argv byte-identical to pre-3.5.0.
        target = self.make_session("uxon-demo", "/srv/repos/demo")
        with self._stub_socket_path():
            req = tmux._build_tmux_attach_request(target, self.make_config(), "u-vz")
        self.assertNotIn("set", req.cmd)

    def test_build_launch_request_injects_set_chain_non_nested(self) -> None:
        # AC1/AC2: set chain rides the managed create command before new-session.
        cfg = self._reco_cfg()
        args = ParsedArgs(action="run", permission_mode="yolo")
        resolved = _resolved_for_test(cfg, mode="yolo")
        with self._stub_socket_path():
            req = tmux._build_tmux_launch_request(
                "/srv/repos/demo",
                "uxon-demo@claude",
                args,
                cfg,
                None,
                resolved_profile=resolved,
            )
        cmd = list(_managed_create_cmd(req))
        self.assertIn("set", cmd)
        # every set token precedes new-session (AC2-fail/D5 ordering)
        self.assertLess(
            max(i for i, t in enumerate(cmd) if t == "set"),
            cmd.index("new-session"),
        )
        # the full chain is present as a contiguous block
        idx = cmd.index("set")
        self.assertEqual(cmd[idx : idx + len(self._RECO_EXPECTED_CHAIN)], self._RECO_EXPECTED_CHAIN)
        self.assertEqual(len(req.prelaunch), 1)

    def test_build_launch_request_injects_set_chain_nested(self) -> None:
        # AC3: set chain rides the managed create command before new-session;
        # switch-client cmd carries NO set tokens.
        cfg = self._reco_cfg()
        args = ParsedArgs(action="run", permission_mode="yolo")
        resolved = _resolved_for_test(cfg, mode="yolo")
        stubs = _StubsChain(
            mock.patch("uxon.infra.tmux.tmux_socket_path", return_value="/tmp/uxon-test.sock"),
            mock.patch("uxon.infra.tmux.tmux_host_socket", return_value="/tmp/uxon-test.sock"),
        )
        with stubs:
            req = tmux._build_tmux_launch_request(
                "/srv/repos/demo",
                "uxon-demo@claude",
                args,
                cfg,
                None,
                resolved_profile=resolved,
            )
        self.assertIn("switch-client", req.cmd)
        self.assertNotIn("set", req.cmd)  # set never rides the switch
        self.assertEqual(len(req.prelaunch), 1)
        create = list(_managed_create_cmd(req))
        self.assertIn("set", create)
        self.assertLess(
            max(i for i, t in enumerate(create) if t == "set"),
            create.index("new-session"),
        )
        idx = create.index("set")
        self.assertEqual(
            create[idx : idx + len(self._RECO_EXPECTED_CHAIN)], self._RECO_EXPECTED_CHAIN
        )

    def test_build_launch_request_byte_identical_when_enabled_but_empty(self) -> None:
        # AC-empty/D1: manage_options=true with no options is byte-identical to off.
        args = ParsedArgs(action="run", permission_mode="yolo")
        off_cfg = self.make_config()
        on_cfg = self.make_config(tmux_manage_options=True)
        with self._stub_socket_path():
            off = tmux._build_tmux_launch_request(
                "/srv/repos/demo",
                "uxon-demo@claude",
                args,
                off_cfg,
                None,
                resolved_profile=_resolved_for_test(off_cfg, mode="yolo"),
            )
            on_empty = tmux._build_tmux_launch_request(
                "/srv/repos/demo",
                "uxon-demo@claude",
                args,
                on_cfg,
                None,
                resolved_profile=_resolved_for_test(on_cfg, mode="yolo"),
            )
        self.assertEqual(off.cmd, on_empty.cmd)
        self.assertEqual(off.prelaunch, on_empty.prelaunch)

    def test_dry_run_prints_set_chain(self) -> None:
        # AC7: --dry-run surfaces the set chain (rides the printed exec line).
        cfg = self._reco_cfg()
        args = ParsedArgs(action="run", permission_mode="yolo", dry_run=True)
        resolved = _resolved_for_test(cfg, mode="yolo")
        buf = io.StringIO()
        with self._stub_socket_path():
            with mock.patch.object(os, "execvp") as execvp:
                with __import__("contextlib").redirect_stdout(buf):
                    rc = tmux.launch_in_tmux(
                        "/srv/repos/demo",
                        "uxon-demo@claude",
                        args,
                        cfg,
                        None,
                        resolved_profile=resolved,
                    )
        self.assertEqual(rc, 0)
        execvp.assert_not_called()
        out = buf.getvalue()
        self.assertIn("set -g mouse on", out)
        self.assertIn("set -as terminal-features", out)

    def test_load_config_tmux_defaults_off_but_scaffolded(self) -> None:
        # Off by default: a config with no [tmux] section emits nothing, but
        # the recommended scope tables ship scaffolded in DEFAULT_CONFIG so
        # flipping manage_options on (see the toggle test) yields the set.
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._write_and_load_cfg("", tmpdir)
        self.assertFalse(cfg.tmux_manage_options)
        self.assertEqual(cfg.tmux_options, {"mouse": "on", "allow-passthrough": "on"})
        self.assertEqual(cfg.tmux_server_options, {"extended-keys": "on"})
        self.assertEqual(cfg.tmux_append_server_options, {"terminal-features": "xterm*:extkeys"})
        # dormant: the recommended tables are present but emit nothing while off
        self.assertEqual(tmux._tmux_set_chain(cfg), [])

    def test_load_config_tmux_manage_options_false_disables(self) -> None:
        # Operator opt-out: manage_options=false yields an empty chain even
        # though the (replaced) tables would otherwise be the default.
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._write_and_load_cfg("[tmux]\nmanage_options = false\n", tmpdir)
        self.assertFalse(cfg.tmux_manage_options)
        self.assertEqual(tmux._tmux_set_chain(cfg), [])

    def test_load_config_tmux_tables_parse(self) -> None:
        toml = textwrap.dedent(
            """
            [tmux]
            manage_options = true

            [tmux.options]
            mouse = "on"

            [tmux.server_options]
            extended-keys = "on"

            [tmux.append_server_options]
            terminal-features = "xterm*:extkeys"
            """
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._write_and_load_cfg(toml, tmpdir)
        self.assertTrue(cfg.tmux_manage_options)
        self.assertEqual(cfg.tmux_options, {"mouse": "on"})
        self.assertEqual(cfg.tmux_server_options, {"extended-keys": "on"})
        self.assertEqual(cfg.tmux_append_server_options, {"terminal-features": "xterm*:extkeys"})

    def test_load_config_tmux_toggle_only_keeps_recommended(self) -> None:
        # Footgun guard: a [tmux] table that sets ONLY manage_options (e.g. the
        # TUI toggle) must NOT wipe the recommended scope tables — each scope
        # falls back to its DEFAULT_CONFIG default despite the shallow merge.
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._write_and_load_cfg("[tmux]\nmanage_options = true\n", tmpdir)
        self.assertTrue(cfg.tmux_manage_options)
        self.assertEqual(tmux._tmux_set_chain(cfg), self._RECO_EXPECTED_CHAIN)

    def test_load_config_tmux_per_scope_override(self) -> None:
        # Overriding one scope replaces only that scope; omitted scopes keep
        # their recommended defaults.
        toml = '[tmux.options]\nmouse = "off"\n'
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._write_and_load_cfg(toml, tmpdir)
        self.assertEqual(cfg.tmux_options, {"mouse": "off"})  # allow-passthrough dropped
        self.assertEqual(cfg.tmux_server_options, {"extended-keys": "on"})  # kept
        self.assertEqual(cfg.tmux_append_server_options, {"terminal-features": "xterm*:extkeys"})

    def test_load_config_tmux_options_not_a_table_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit):
                self._write_and_load_cfg('[tmux]\noptions = "nope"\n', tmpdir)

    def test_load_config_tmux_non_scalar_leaf_fails(self) -> None:
        toml = "[tmux.options]\nmouse = [1, 2]\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit):
                self._write_and_load_cfg(toml, tmpdir)

    # ── --profile / --mode / permission_mode ─────────────────

    def test_parse_mode_flag(self) -> None:
        p = parse_run_like(["--mode", "yolo"], "run")
        self.assertEqual(p.permission_mode, "yolo")

    def test_parse_legacy_aliases_are_not_uxon_flags(self) -> None:
        # The old shorthand (`--dsp`/`--auto`/…) was removed: uxon no
        # longer interprets it as a mode. It now falls through to the
        # forwarded agent args, leaving ``permission_mode`` unset.
        for alias in ("--dsp", "--dap", "-dap", "-dsp", "--auto"):
            p = parse_run_like([alias], "run")
            self.assertIsNone(p.permission_mode)
            self.assertEqual(p.agent_args, [alias])

    def test_parse_agent_flag_fails_with_profile_hint(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            parse_run_like(["--agent", "codex", "--mode", "yolo"], "run")
        self.assertIn("--profile", getattr(cm.exception, "uxon_msg", ""))

    def test_parse_profile_flag(self) -> None:
        p = parse_run_like(["--profile", "codex", "--mode", "yolo"], "run")
        self.assertEqual(p.profile, "codex")
        self.assertEqual(p.permission_mode, "yolo")

    def test_parse_unknown_flag_goes_to_agent_args(self) -> None:
        p = parse_run_like(["--some-claude-flag", "x"], "run")
        self.assertEqual(p.agent_args, ["--some-claude-flag", "x"])

    def test_launch_builder_cursor_yolo(self) -> None:
        cfg = self.make_config(enabled_agents=("cursor",), default_agent="cursor")
        args = ParsedArgs(action="run", permission_mode="yolo")
        resolved = _resolved_for_test(cfg, profile="cursor", mode="yolo")
        with self._stub_socket_path():
            req = tmux._build_tmux_launch_request(
                "/tmp/x",
                "uxon-x@cursor",
                args,
                cfg,
                None,
                resolved_profile=resolved,
            )
        create = _managed_create_cmd(req)
        self.assertIn("cursor-agent", create)
        self.assertIn("--yolo", create)

    def test_launch_builder_cursor_auto_errors(self) -> None:
        cfg = self.make_config(enabled_agents=("cursor",), default_agent="cursor")
        args = ParsedArgs(action="run", profile="cursor", permission_mode="auto")
        resolved = _resolved_for_test(cfg, profile="cursor", mode="auto")
        with self._stub_socket_path():
            with self.assertRaises(SystemExit):
                tmux._build_tmux_launch_request(
                    "/tmp/x",
                    "uxon-x@cursor",
                    args,
                    cfg,
                    None,
                    resolved_profile=resolved,
                )

    def test_launch_builder_codex_full_auto(self) -> None:
        cfg = self.make_config(enabled_agents=("codex",), default_agent="codex")
        args = ParsedArgs(action="run", profile="codex", permission_mode="auto")
        resolved = _resolved_for_test(cfg, profile="codex", mode="auto")
        with self._stub_socket_path():
            req = tmux._build_tmux_launch_request(
                "/tmp/x",
                "uxon-x@codex",
                args,
                cfg,
                None,
                resolved_profile=resolved,
            )
        self.assertIn("--full-auto", _managed_create_cmd(req))

    def test_launch_builder_branch_does_not_add_native_w_flag(self) -> None:
        # uxon launches worktrees via ``-c <worktree_path>``, never the
        # agent's native ``-w`` flag (§2.1) — branch is informational only.
        cfg = self.make_config()
        args = ParsedArgs(action="run", profile="claude", permission_mode="normal")
        resolved = _resolved_for_test(cfg, profile="claude", mode="normal")
        with self._stub_socket_path():
            req = tmux._build_tmux_launch_request(
                "/srv/repos/myapp/.uxon/worktrees/feat",
                "uxon-myapp-feat@claude",
                args,
                cfg,
                "feat",
                resolved_profile=resolved,
            )
        joined = " ".join(_managed_create_cmd(req))
        self.assertNotIn(" -w ", f" {joined} ")
        self.assertNotIn("-w feat", joined)

    def test_launch_builder_branch_allowed_for_non_claude_agent(self) -> None:
        # The old "-w is only supported for claude" guard is gone.
        cfg = self.make_config(enabled_agents=("codex",), default_agent="codex")
        args = ParsedArgs(action="run", profile="codex", permission_mode="normal")
        resolved = _resolved_for_test(cfg, profile="codex", mode="normal")
        with self._stub_socket_path():
            req = tmux._build_tmux_launch_request(
                "/srv/repos/myapp/.uxon/worktrees/feat",
                "uxon-myapp-feat@codex",
                args,
                cfg,
                "feat",
                resolved_profile=resolved,
            )
        self.assertTrue(_managed_create_cmd(req))

    def test_mode_auto_with_cursor_default_fails_at_launch(self) -> None:
        # Full-stack check: parser accepts --mode without knowing the resolved
        # agent; the launch builder rejects an id the resolved agent lacks,
        # listing the agent's valid modes.
        cfg = self.make_config(enabled_agents=("cursor",), default_agent="cursor")
        parsed = parse_run_like(["--mode", "auto"], "run")
        self.assertEqual(parsed.permission_mode, "auto")
        self.assertIsNone(parsed.profile)  # not explicitly set
        resolved = _resolved_for_test(cfg, profile="cursor", mode="auto")
        with self._stub_socket_path():
            with self.assertRaises(SystemExit):
                tmux._build_tmux_launch_request(
                    "/tmp/x",
                    "uxon-x@cursor",
                    parsed,
                    cfg,
                    None,
                    resolved_profile=resolved,
                )


def _mk_session(
    name: str, path: str = "/srv/repos/x", agent: str = "claude", legacy: bool = False
) -> domain_session.SessionInfo:
    return domain_session.SessionInfo(
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
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd

            class CP:
                returncode = 0
                stdout = "/srv/work/myapp\n"
                stderr = ""

            return CP()

        with (
            mock.patch("uxon.infra.git.run_query", fake_run),
            mock.patch("uxon.infra.identity.process_user", return_value="caller"),
        ):
            root = git.git_repo_root_nonint_as_user("/srv/work/myapp/sub", "dana_agent")
        self.assertEqual(root, domain_authz.canonical("/srv/work/myapp"))
        # The resolver uses the non-interactive (``sudo -n``) prefix — assert
        # it is the leading prefix of the issued argv. (cli.py composes the
        # non-interactive flags as ``-niu``, so the prefix is checked as a
        # whole rather than for a standalone ``-n`` token.)
        prefix = identity.nonint_command_prefix_for_user("dana_agent")
        self.assertEqual(seen["cmd"][: len(prefix)], prefix)

    def test_repo_root_nonint_none_on_failure(self) -> None:
        def fake_run(cmd, **kw):
            class CP:
                returncode = 128
                stdout = ""
                stderr = "not a git repo"

            return CP()

        with (
            mock.patch("uxon.infra.git.run_query", fake_run),
            mock.patch("uxon.infra.identity.process_user", return_value="caller"),
        ):
            self.assertIsNone(git.git_repo_root_nonint_as_user("/tmp/x", "dana_agent"))

    def test_common_dir_normalises_to_primary_root(self) -> None:
        # git rev-parse --git-common-dir on a linked worktree returns the
        # primary repo's .git; the primary root is its parent.
        def fake_run(cmd, **kw):
            class CP:
                returncode = 0
                stdout = "/srv/work/myapp/.git\n"
                stderr = ""

            return CP()

        with (
            mock.patch("uxon.infra.git.run_query", fake_run),
            mock.patch("uxon.infra.identity.process_user", return_value="caller"),
        ):
            root = git.git_common_dir_root_as_user(
                "/srv/work/myapp/.uxon/worktrees/feat", "dana_agent"
            )
        self.assertEqual(root, domain_authz.canonical("/srv/work/myapp"))


class AllowedRootsUnifiedSemanticsTests(unittest.TestCase):
    """Regression: empty ``allowed_roots`` must mean "any writable" everywhere.

    The 3.1.0 fix introduced this semantics for ``is_launch_target_allowed``
    but missed ``do_new``, ``_resolve_tui_project_dir`` and
    ``do_doctor``. After the unification
    refactor every consumer routes through
    :func:`uxon.domain.authz.is_under_allowed_roots` so the four sites behave
    identically.
    """

    def _cfg(self, **overrides) -> Config:
        from uxon.domain.agents import default_agent_catalog

        agents = default_agent_catalog()
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
            new_project_root="/srv/work",
            repeat_noninteractive_mode="fail",
            tmux_socket_template="/tmp/uxon-{user}.sock",
            tui_refresh_interval_seconds=2.0,
            git_create_enabled=False,
            default_git_remote_profile="",
            git_remote_profiles=[],
            # Minimal baseline: tmux managed options OFF/empty so launch-argv
            # tests stay focused on the core invocation. The tmux tests opt in
            # explicitly; the on-by-default production value lives in
            # DEFAULT_CONFIG / the Config field defaults.
            tmux_manage_options=False,
            tmux_options={},
            tmux_server_options={},
            tmux_append_server_options={},
            agents=agents,
            launch=_launch_for_test(agents, default_profile="claude"),
        )
        defaults.update(overrides)
        return Config(**defaults)

    def test_is_under_allowed_roots_empty_list_returns_true(self) -> None:
        cfg = self._cfg(allowed_roots=[])
        self.assertTrue(domain_authz.is_under_allowed_roots(cfg, "/anything/at/all"))

    def test_is_under_allowed_roots_non_empty_strict(self) -> None:
        cfg = self._cfg(allowed_roots=["/srv/work"])
        self.assertTrue(domain_authz.is_under_allowed_roots(cfg, "/srv/work/proj"))
        self.assertFalse(domain_authz.is_under_allowed_roots(cfg, "/home/u/proj"))

    def test_do_new_empty_allowed_roots_passes_writable_parent(self) -> None:
        """Regression for the original bug report: ``uxon new x --dry-run``
        used to fail with "new target must be under allowed_roots" even
        when ``allowed_roots=[]``.
        """
        cfg = self._cfg(allowed_roots=[])
        args = ParsedArgs(action="new", target_id="demo", dry_run=True, agent_args=[])
        resolved = _resolved_for_test(cfg, launch_user="u-vz")

        with mock.patch("uxon.infra.identity.probe_cwd_writable", return_value=True):
            with mock.patch.object(new_app, "canonical", side_effect=lambda v: str(v), create=True):
                with mock.patch.object(os, "getcwd", return_value="/home/u-vz"):
                    with mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[]):
                        with mock.patch.object(
                            new_app, "allocate_session_name", return_value="uxon-demo"
                        ):
                            with mock.patch("uxon.infra.tmux.launch_in_tmux", return_value=0):
                                with mock.patch.object(
                                    new_app.launch_profile_app,
                                    "resolve_launch_profile",
                                    return_value=resolved,
                                ):
                                    rc = new_app.do_new(args, cfg, "u-vz")
        self.assertEqual(rc, 0)

    def test_do_new_empty_allowed_roots_rejects_unwritable_parent(self) -> None:
        """Empty ``allowed_roots`` doesn't mean "anything goes" — the
        parent of the new project still has to be writable for the
        launch user."""
        cfg = self._cfg(allowed_roots=[])
        args = ParsedArgs(action="new", target_id="demo", dry_run=True, agent_args=[])
        resolved = _resolved_for_test(cfg, launch_user="u-vz")

        with mock.patch("uxon.infra.identity.probe_cwd_writable", return_value=False):
            with mock.patch.object(new_app, "canonical", side_effect=lambda v: str(v), create=True):
                with mock.patch.object(new_app, "eprint"):
                    with mock.patch.object(
                        new_app.launch_profile_app,
                        "resolve_launch_profile",
                        return_value=resolved,
                    ):
                        with self.assertRaises(SystemExit) as exc:
                            new_app.do_new(args, cfg, "u-vz")
        self.assertEqual(exc.exception.code, 2)

    def test_do_new_non_empty_allowed_roots_rejects_outside(self) -> None:
        cfg = self._cfg(allowed_roots=["/srv/work"], new_project_root="/home/u-vz")
        args = ParsedArgs(action="new", target_id="demo", dry_run=True, agent_args=[])
        resolved = _resolved_for_test(cfg, launch_user="u-vz")

        with mock.patch("uxon.infra.identity.probe_cwd_writable", return_value=True):
            with mock.patch.object(new_app, "canonical", side_effect=lambda v: str(v), create=True):
                with mock.patch.object(new_app, "eprint"):
                    with mock.patch.object(
                        new_app.launch_profile_app,
                        "resolve_launch_profile",
                        return_value=resolved,
                    ):
                        with self.assertRaises(SystemExit) as exc:
                            new_app.do_new(args, cfg, "u-vz")
        self.assertEqual(exc.exception.code, 2)

    def test_doctor_no_issue_when_allowed_roots_empty(self) -> None:
        """Doctor should not flag ``new_project_root outside allowed_roots``
        when ``allowed_roots`` is empty — the whitelist is bypassed
        and the path is vacuously fine."""
        cfg = self._cfg(allowed_roots=[], new_project_root="/anywhere")
        # Direct unit test of the predicate that drives the doctor
        # warning; the full doctor flow is exercised elsewhere.
        self.assertTrue(domain_authz.is_under_allowed_roots(cfg, cfg.new_project_root))


class SessionNamingTests(unittest.TestCase):
    """Tests for the uxon-<stem>@<profile> session naming scheme."""

    def test_parse_session_name_new(self) -> None:
        self.assertEqual(
            domain_session.parse_session_name("uxon-foo@codex"), ("foo", "codex", 1, False)
        )
        self.assertEqual(
            domain_session.parse_session_name("uxon-foo@codex-3"), ("foo", "codex", 3, False)
        )
        self.assertEqual(
            domain_session.parse_session_name("uxon-my-repo-branch@claude"),
            ("my-repo-branch", "claude", 1, False),
        )

    def test_parse_session_name_legacy_at_prefix(self) -> None:
        # ``ccw-`` sessions still parse when listed in ``legacy_prefixes`` and
        # are flagged ``legacy=True`` (default prefix is ``uxon-``).
        self.assertEqual(
            domain_session.parse_session_name("ccw-foo@codex", legacy_prefixes=("ccw-",)),
            ("foo", "codex", 1, True),
        )
        # When ``uxon-`` is the configured current prefix, the same-prefixed
        # name is not legacy.
        self.assertEqual(
            domain_session.parse_session_name("uxon-foo@codex", prefix="uxon-"),
            ("foo", "codex", 1, False),
        )
        # Without legacy_prefixes, a ccw- name does not match (default prefix
        # is uxon-).
        self.assertIsNone(domain_session.parse_session_name("ccw-foo@codex"))
        # And with a non-matching explicit prefix, uxon- is also unrecognised.
        self.assertIsNone(domain_session.parse_session_name("uxon-foo@codex", prefix="ccw-"))

    def test_parse_session_name_rejects_garbage(self) -> None:
        self.assertIsNone(domain_session.parse_session_name("random-x"))
        self.assertIsNone(domain_session.parse_session_name("uxon-foo"))  # missing @agent
        self.assertIsNone(
            domain_session.parse_session_name("cc-foo")
        )  # ancient format no longer recognised

    def test_candidate_session_name(self) -> None:
        self.assertEqual(
            domain_session.candidate_session_name("foo", 1, "cursor"), "uxon-foo@cursor"
        )
        self.assertEqual(
            domain_session.candidate_session_name("foo", 2, "cursor"), "uxon-foo@cursor-2"
        )

    def test_compatible_indexed_sessions_agent_specific(self) -> None:
        # Two sessions same stem different agents are NOT siblings.
        compat_root = "/srv/repos/foo"
        s_claude = _mk_session("uxon-foo@claude", compat_root, agent="claude")
        s_codex = _mk_session("uxon-foo@codex", compat_root, agent="codex")
        matches = domain_session.compatible_indexed_sessions(
            "foo", "claude", compat_root, [s_claude, s_codex]
        )
        self.assertEqual([m.name for m in matches], ["uxon-foo@claude"])

    def test_resolve_full_new(self) -> None:
        sessions = [_mk_session("uxon-foo@claude"), _mk_session("uxon-foo@codex", agent="codex")]
        self.assertEqual(
            sessions_probe.resolve_session("uxon-foo@codex", sessions, "uxon-").name,
            "uxon-foo@codex",
        )

    def test_resolve_suffixed_without_prefix(self) -> None:
        sessions = [_mk_session("uxon-foo@codex", agent="codex")]
        self.assertEqual(
            sessions_probe.resolve_session("foo@codex", sessions, "uxon-").name,
            "uxon-foo@codex",
        )

    def test_resolve_stem_unique(self) -> None:
        sessions = [_mk_session("uxon-foo@codex", agent="codex")]
        self.assertEqual(
            sessions_probe.resolve_session("foo", sessions, "uxon-").name,
            "uxon-foo@codex",
        )

    def test_resolve_stem_ambiguous(self) -> None:
        sessions = [_mk_session("uxon-foo@claude"), _mk_session("uxon-foo@codex", agent="codex")]
        with self.assertRaises(SystemExit):
            sessions_probe.resolve_session("foo", sessions, "uxon-")


class CliPreflightTests(unittest.TestCase):
    """Tests for CLI preflight probe in main()."""

    def test_preflight_tmux_missing_on_run_action(self) -> None:
        """When tmux is missing, run action should fail with friendly message."""
        buf_err = io.StringIO()
        with mock.patch.object(sys, "stderr", buf_err):
            with mock.patch("uxon.infra.probes.probe_host") as probe:
                mock_tmux_missing = mock.MagicMock()
                mock_tmux_missing.tmux.path = None
                mock_tmux_missing.tmux.install_hint = "apt install tmux"
                mock_tmux_missing.agents = {"claude": mock.MagicMock(path="/usr/bin/claude")}
                probe.return_value = mock_tmux_missing

                with self.assertRaises(SystemExit) as ctx:
                    main(["run"])
                self.assertEqual(ctx.exception.code, 1)
                err = buf_err.getvalue()
                self.assertIn("tmux is not installed", err)
                self.assertIn("apt install tmux", err)

    def test_launch_profile_resolution_agent_missing_on_run_action(self) -> None:
        """A selected launch profile with a missing host agent fails in the resolver."""
        buf_err = io.StringIO()
        with mock.patch.object(sys, "stderr", buf_err):
            with mock.patch("uxon.infra.probes.probe_host") as probe:
                mock_report = mock.MagicMock()
                mock_report.tmux.path = "/usr/bin/tmux"
                mock_claude = mock.MagicMock()
                mock_claude.path = None
                mock_claude.install_hint = "npm install -g @anthropic-ai/claude-code"
                mock_report.agents = {"claude": mock_claude}
                probe.return_value = mock_report

                with self.assertRaises(SystemExit) as ctx:
                    main(["run", "--profile", "claude"])
                self.assertEqual(ctx.exception.code, 1)
                err = buf_err.getvalue()
                self.assertIn("'claude'", err)
                self.assertIn("is not installed", err)
                self.assertIn("npm install", err)

    def test_preflight_skipped_on_version_action(self) -> None:
        """version action should skip the preflight probe."""
        with mock.patch("uxon.infra.probes.probe_host") as probe:
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                main(["version"])
            # Probe should never have been called.
            probe.assert_not_called()

    def test_preflight_skipped_on_doctor_action(self) -> None:
        """doctor action should skip the preflight probe."""
        with mock.patch("uxon.infra.probes.probe_host") as probe:
            with mock.patch("uxon.app.doctor.do_doctor", return_value=0):
                main(["doctor"])
            # Probe should never have been called.
            probe.assert_not_called()

    def test_preflight_skipped_on_interactive_action(self) -> None:
        """interactive (TUI) action skips the CLI preflight.

        Regression: a wider gate that included ``interactive`` made every
        no-arg ``uxon`` invocation block on a sudo round-trip before the
        TUI mounted, defeating the fast-first-frame design. The TUI runs
        its own async probe in the background.
        """
        with mock.patch("uxon.infra.probes.probe_host") as probe:
            with mock.patch("uxon.cli.main.do_interactive", return_value=0):
                main([])
            probe.assert_not_called()

    def test_run_action_skips_main_preflight(self) -> None:
        """run resolves launch profiles inside the use-case, not in main preflight."""
        with mock.patch("uxon.infra.probes.probe_host") as probe:
            with mock.patch("uxon.app.run.do_run", return_value=0):
                rc = main(["run"])
            self.assertEqual(rc, 0)
            probe.assert_not_called()

    def test_preflight_list_action_does_not_need_agents(self) -> None:
        """list action should check tmux but not any specific agent."""
        with mock.patch("uxon.infra.probes.probe_host") as probe:
            mock_report = mock.MagicMock()
            mock_report.tmux.path = "/usr/bin/tmux"
            # Agent can be missing; list doesn't care.
            mock_report.agents = {"claude": mock.MagicMock(path=None)}
            probe.return_value = mock_report

            with mock.patch("uxon.app.listing.print_list", return_value=0):
                with mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[]):
                    rc = main(["list"])
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
        from uxon.infra import audit as uxon_audit

        recorded: list[tuple[str, dict]] = []

        def fake_audit(event: str, *, outcome: str = "ok", **fields: object) -> None:
            recorded.append((event, {"outcome": outcome, **fields}))

        cfg = Config(
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
            new_project_root="/srv/repos",
            repeat_noninteractive_mode="fail",
            tmux_socket_template="/tmp/uxon-{user}.sock",
            tui_refresh_interval_seconds=2.0,
            git_create_enabled=False,
            default_git_remote_profile="",
            git_remote_profiles=[],
        )

        with mock.patch("uxon.infra.probes.probe_host") as probe:
            mock_report = mock.MagicMock()
            mock_report.tmux.path = "/usr/bin/tmux"
            mock_report.agents = {"claude": mock.MagicMock(path=None)}
            probe.return_value = mock_report
            with (
                mock.patch.dict("os.environ", {"SSH_CONNECTION": "1.2.3.4 22 5.6.7.8 22"}),
                mock.patch("uxon.infra.config_loader.load_config", return_value=cfg),
                mock.patch.object(uxon_audit, "audit", side_effect=fake_audit),
                mock.patch("sys.stderr", new_callable=io.StringIO),
            ):
                with self.assertRaises(SystemExit):
                    main(["list", "--all-users"])

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
                cfg = config_loader.load_config(tmp)
                buf_err = io.StringIO()
                buf_out = io.StringIO()
                with (
                    mock.patch.object(sys, "stderr", buf_err),
                    mock.patch.object(sys, "stdout", buf_out),
                ):
                    rc = do_interactive(cfg, "nobody", "nobody")
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
        captured = {}

        def fake_alloc(stem, agent, root, sessions, *, prefix):
            captured["stem"] = stem
            return f"{prefix}{stem}@{agent}"

        cfg = config_loader.load_config("/tmp")
        with (
            mock.patch.object(launch_app, "ensure_launch_target_allowed", lambda *a, **k: None),
            mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[]),
            mock.patch.object(tui_planning, "allocate_session_name", fake_alloc),
            mock.patch(
                "uxon.infra.tmux._build_tmux_launch_request",
                lambda *a, **k: attach_app._tui_launch_request_cls()(cmd=("true",), label="x"),
            ),
        ):
            tui_planning._plan_tui_run_agent(
                cfg,
                "dana_agent",
                "dana_agent",
                "/srv/work/myapp/.uxon/worktrees/feature-auth",
                "claude",
                "default",
                worktree=("/srv/work/myapp", "feature/auth"),
            )
        self.assertEqual(captured["stem"], "myapp-feature-auth")

    def test_run_agent_uses_path_stem_without_worktree(self) -> None:
        captured = {}

        def fake_alloc(stem, agent, root, sessions, *, prefix):
            captured["stem"] = stem
            return f"{prefix}{stem}@{agent}"

        cfg = config_loader.load_config("/tmp")
        with (
            mock.patch.object(launch_app, "ensure_launch_target_allowed", lambda *a, **k: None),
            mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[]),
            mock.patch.object(tui_planning, "allocate_session_name", fake_alloc),
            mock.patch(
                "uxon.infra.tmux._build_tmux_launch_request",
                lambda *a, **k: attach_app._tui_launch_request_cls()(cmd=("true",), label="x"),
            ),
        ):
            tui_planning._plan_tui_run_agent(
                cfg, "dana_agent", "dana_agent", "/srv/work/plain", "claude", "default"
            )
        self.assertEqual(captured["stem"], "plain")


class ProbeWorktreeStemTests(unittest.TestCase):
    def _session(self, name: str, path: str):
        return domain_session.SessionInfo(
            user="dana_agent",
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

        wt = "/srv/work/myapp/.uxon/worktrees/feature-auth"
        sess = [self._session("uxon-myapp-feature-auth@claude", wt)]
        cfg = config_loader.load_config("/tmp")
        with mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=sess):
            out = sessions_probe.probe_tui_compatible_sessions(
                cfg,
                "dana_agent",
                wt,
                "claude",
                stem="myapp-feature-auth",
                compatibility_root=wt,
            )
        self.assertEqual([s.name for s in out], ["uxon-myapp-feature-auth@claude"])

    def test_default_stem_unchanged_for_plain_target(self) -> None:

        target = "/srv/work/plain"
        sess = [self._session("uxon-plain@claude", target)]
        cfg = config_loader.load_config("/tmp")
        with mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=sess):
            out = sessions_probe.probe_tui_compatible_sessions(cfg, "dana_agent", target, "claude")
        self.assertEqual([s.name for s in out], ["uxon-plain@claude"])


class WorktreeIdentityRegressionTests(unittest.TestCase):
    """Regression guard for §2.5: planner and probe derive the SAME
    repo-qualified stem; cross-repo same-named worktrees never collide.
    """

    def _session(self, name: str, path: str):
        return domain_session.SessionInfo(
            user="dana_agent",
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
        repo = "/srv/work/myapp"
        wt = "/srv/work/myapp/.uxon/worktrees/feature-auth"
        branch = "feature/auth"
        cfg = config_loader.load_config("/tmp")

        # (a) planner names the session with the worktree stem.
        with (
            mock.patch.object(launch_app, "ensure_launch_target_allowed", lambda *a, **k: None),
            mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[]),
            mock.patch(
                "uxon.infra.tmux._build_tmux_launch_request",
                lambda td, s, *a, **k: attach_app._tui_launch_request_cls()(
                    cmd=("true",), label=f"launch {s}"
                ),
            ),
        ):
            req = tui_planning._plan_tui_run_agent(
                cfg,
                "dana_agent",
                "dana_agent",
                wt,
                "claude",
                "default",
                worktree=(repo, branch),
            )
        self.assertEqual(req.label, "launch uxon-myapp-feature-auth@claude")

        # (b) the worktree-aware probe finds exactly that session.
        live = [self._session("uxon-myapp-feature-auth@claude", wt)]
        with mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=live):
            found = sessions_probe.probe_tui_compatible_sessions(
                cfg,
                "dana_agent",
                wt,
                "claude",
                stem=domain_session.session_stem_for_worktree(repo, branch),
                compatibility_root=wt,
            )
        self.assertEqual([s.name for s in found], ["uxon-myapp-feature-auth@claude"])

    def test_two_repos_same_branch_do_not_collide(self) -> None:
        repo_b = "/srv/work/beta"
        wt_a = "/srv/work/alpha/.uxon/worktrees/feature"
        wt_b = "/srv/work/beta/.uxon/worktrees/feature"
        cfg = config_loader.load_config("/tmp")
        # alpha's worktree session is live; probing beta's worktree must
        # NOT match it and must NOT hard-fail (distinct repo-qualified stems).
        live = [self._session("uxon-alpha-feature@claude", wt_a)]
        with mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=live):
            found = sessions_probe.probe_tui_compatible_sessions(
                cfg,
                "dana_agent",
                wt_b,
                "claude",
                stem=domain_session.session_stem_for_worktree(repo_b, "feature"),
                compatibility_root=wt_b,
            )
        self.assertEqual(found, ())  # no match, no SystemExit

    def test_branch_named_like_repo_stays_repo_qualified(self) -> None:
        # §2.5: the worktree stem must never collapse to the bare repo slug,
        # or a worktree on a branch named like its repo would collide with
        # the primary tree's stem (session_stem_for_path) and hard-fail.
        repo = "/srv/work/myapp"
        self.assertEqual(domain_session.session_stem_for_worktree(repo, "myapp"), "myapp-myapp")
        self.assertNotEqual(
            domain_session.session_stem_for_worktree(repo, "myapp"),
            domain_session.session_stem_for_path(repo),
        )


class PlanWorktreeLaunchTests(unittest.TestCase):
    def test_new_branch_local_base_adds_worktree_and_names_session(self) -> None:

        cfg = config_loader.load_config("/tmp")
        repo = "/srv/work/myapp"
        resolved = _resolved_for_test(cfg)
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
            mock.patch.object(launch_app, "is_worktree_target_allowed", return_value=True),
            mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[]),
            mock.patch("uxon.infra.process.run_cmd", fake_run_cmd),
            mock.patch("uxon.infra.git.write_uxon_exclude_entry", lambda *a, **k: None),
            mock.patch("uxon.infra.git.copy_worktreeinclude_matches", lambda *a, **k: None),
            mock.patch("uxon.infra.git._local_base_ref_as_user", return_value="origin/HEAD"),
            mock.patch("uxon.infra.git._branch_exists_as_user", return_value=False),
            mock.patch(
                "uxon.infra.tmux._build_tmux_launch_request",
                lambda td, s, *a, **k: attach_app._tui_launch_request_cls()(
                    cmd=("true",), label=f"launch {s}"
                ),
            ),
            mock.patch.object(
                launch_app.launch_profile_app,
                "revalidate_launch_profile",
                return_value=resolved,
            ),
            mock.patch("uxon.infra.audit.audit", fake_audit),
        ):
            req = launch_app.plan_worktree_launch(
                cfg,
                "dana_agent",
                resolved,
                repo,
                "feature/auth",
                requested_profile="claude",
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
        cfg = config_loader.load_config("/tmp")
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
            mock.patch("uxon.infra.identity.probe_cwd_writable", return_value=True),
            mock.patch("uxon.infra.process.run_cmd", fake_run_cmd),
        ):
            with self.assertRaises(SystemExit) as cm:
                launch_app.plan_worktree_launch(
                    cfg,
                    "dana_agent",
                    _resolved_for_test(cfg),
                    "/srv/work/myapp",
                    "feature/auth",
                    requested_profile="claude",
                )
        msg = getattr(cm.exception, "uxon_msg", "")
        self.assertIn("allowed_roots", msg)
        self.assertIn("worktree_root", msg)  # error suggests the override key
        # No git worktree add was attempted before the gate failed.
        self.assertFalse([c for c in called if "worktree" in c and "add" in c])

    def test_existing_branch_checks_out_without_b(self) -> None:

        cfg = config_loader.load_config("/tmp")
        resolved = _resolved_for_test(cfg)
        calls: list[list[str]] = []

        def fake_run_cmd(cmd, check=True, **kw):
            calls.append(cmd)

            class CP:
                returncode = 0
                stdout = ""
                stderr = ""

            return CP()

        with (
            mock.patch.object(launch_app, "is_worktree_target_allowed", return_value=True),
            mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[]),
            mock.patch("uxon.infra.process.run_cmd", fake_run_cmd),
            mock.patch("uxon.infra.git.write_uxon_exclude_entry", lambda *a, **k: None),
            mock.patch("uxon.infra.git.copy_worktreeinclude_matches", lambda *a, **k: None),
            mock.patch("uxon.infra.git._branch_exists_as_user", return_value=True),
            mock.patch(
                "uxon.infra.tmux._build_tmux_launch_request",
                lambda td, s, *a, **k: attach_app._tui_launch_request_cls()(
                    cmd=("true",), label=f"launch {s}"
                ),
            ),
            mock.patch.object(
                launch_app.launch_profile_app,
                "revalidate_launch_profile",
                return_value=resolved,
            ),
            mock.patch("uxon.infra.audit.audit", lambda *a, **k: None),
        ):
            launch_app.plan_worktree_launch(
                cfg,
                "dana_agent",
                resolved,
                "/srv/work/myapp",
                "existing",
                requested_profile="claude",
            )
        add = [c for c in calls if "worktree" in c and "add" in c]
        self.assertTrue(add)
        self.assertNotIn("-b", add[0])

    def test_agent_args_forwarded_to_launch_request(self) -> None:
        # CLI parity: `uxon -w branch -- --extra-flag` must not silently drop
        # the agent passthrough args on the worktree create path.

        cfg = config_loader.load_config("/tmp")
        resolved = _resolved_for_test(cfg)
        captured: dict[str, object] = {}

        def fake_build(td, s, run_args, *a, **k):
            captured["agent_args"] = list(run_args.agent_args)
            return attach_app._tui_launch_request_cls()(cmd=("true",), label=f"launch {s}")

        def fake_run_cmd(cmd, check=True, **kw):
            class CP:
                returncode = 0
                stdout = ""
                stderr = ""

            return CP()

        with (
            mock.patch.object(launch_app, "is_worktree_target_allowed", return_value=True),
            mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[]),
            mock.patch("uxon.infra.process.run_cmd", fake_run_cmd),
            mock.patch("uxon.infra.git.write_uxon_exclude_entry", lambda *a, **k: None),
            mock.patch("uxon.infra.git.copy_worktreeinclude_matches", lambda *a, **k: None),
            mock.patch("uxon.infra.git._branch_exists_as_user", return_value=True),
            mock.patch("uxon.infra.tmux._build_tmux_launch_request", fake_build),
            mock.patch.object(
                launch_app.launch_profile_app,
                "revalidate_launch_profile",
                return_value=resolved,
            ),
            mock.patch("uxon.infra.audit.audit", lambda *a, **k: None),
        ):
            launch_app.plan_worktree_launch(
                cfg,
                "dana_agent",
                resolved,
                "/srv/work/myapp",
                "existing",
                requested_profile="claude",
                agent_args=["--extra-flag", "value"],
            )
        self.assertEqual(captured["agent_args"], ["--extra-flag", "value"])

    def test_worktree_add_failure_surfaces_clear_error(self) -> None:

        cfg = config_loader.load_config("/tmp")

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
            mock.patch.object(launch_app, "is_worktree_target_allowed", return_value=True),
            mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[]),
            mock.patch("uxon.infra.process.run_cmd", fake_run_cmd),
            mock.patch("uxon.infra.git.write_uxon_exclude_entry", lambda *a, **k: None),
            mock.patch("uxon.infra.git._branch_exists_as_user", return_value=False),
            mock.patch("uxon.infra.git._local_base_ref_as_user", return_value="HEAD"),
            mock.patch(
                "uxon.infra.tmux._build_tmux_launch_request",
                lambda td, s, *a, **k: attach_app._tui_launch_request_cls()(
                    cmd=("true",), label=f"launch {s}"
                ),
            ),
        ):
            with self.assertRaises(SystemExit) as cm:
                launch_app.plan_worktree_launch(
                    cfg,
                    "dana_agent",
                    _resolved_for_test(cfg),
                    "/srv/work/myapp",
                    "feature/auth",
                    requested_profile="claude",
                )
        # Friendly message, not the raw git fatal. fail() stashes the
        # human-readable text on the SystemExit as ``uxon_msg``.
        self.assertIn("already checked out", getattr(cm.exception, "uxon_msg", ""))

    def test_dry_run_is_side_effect_free(self) -> None:
        # dry_run must not mkdir / fetch / write exclude / add worktree /
        # emit audit — only resolve + print the plan.

        cfg = config_loader.load_config("/tmp")
        resolved = _resolved_for_test(cfg)
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
            mock.patch.object(launch_app, "is_worktree_target_allowed", return_value=True),
            mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[]),
            mock.patch("uxon.infra.process.run_cmd", fake_run_cmd),
            mock.patch("uxon.infra.git._branch_exists_as_user", return_value=False),
            mock.patch("uxon.infra.git._local_base_ref_as_user", return_value="HEAD"),
            mock.patch(
                "uxon.infra.git.write_uxon_exclude_entry",
                lambda *a, **k: calls.append(["WROTE_EXCLUDE"]),
            ),
            mock.patch(
                "uxon.infra.git.copy_worktreeinclude_matches",
                lambda *a, **k: calls.append(["COPIED"]),
            ),
            mock.patch(
                "uxon.infra.tmux._build_tmux_launch_request",
                lambda td, s, *a, **k: attach_app._tui_launch_request_cls()(
                    cmd=("true",), label=f"launch {s}"
                ),
            ),
            mock.patch("uxon.infra.audit.audit", lambda event, **k: events.append(event)),
        ):
            req = launch_app.plan_worktree_launch(
                cfg,
                "dana_agent",
                resolved,
                "/srv/work/myapp",
                "feature/auth",
                requested_profile="claude",
                dry_run=True,
            )
        self.assertEqual(req.label, "launch uxon-myapp-feature-auth@claude")
        # No mutating commands ran, no exclude write/copy, no audit events.
        self.assertEqual(calls, [])
        self.assertEqual(events, [])


class CliWorktreeRoutingTests(unittest.TestCase):
    def test_do_run_w_routes_through_plan_worktree_launch(self) -> None:
        cfg = config_loader.load_config("/tmp")
        args = ParsedArgs(
            action="run",
            profile="claude",
            permission_mode="normal",
            worktree_branch="feature/auth",
            dry_run=True,
        )
        captured = {}
        resolved = _resolved_for_test(cfg)

        def fake_plan(
            cfg_,
            user,
            resolved_profile,
            repo,
            branch,
            *,
            requested_profile=None,
            agent_args=None,
            dry_run=False,
        ):
            captured.update(
                repo=repo,
                branch=branch,
                profile=resolved_profile.profile.id,
                requested_profile=requested_profile,
                dry_run=dry_run,
            )
            return attach_app._tui_launch_request_cls()(cmd=("true",), label="launch x")

        with (
            mock.patch.object(launch_app, "ensure_launch_target_allowed", lambda *a, **k: None),
            mock.patch.object(os, "getcwd", return_value="/srv/work/myapp/sub"),
            mock.patch(
                "uxon.infra.git.git_repo_root_nonint_as_user", return_value="/srv/work/myapp"
            ),
            mock.patch(
                "uxon.infra.git.git_common_dir_root_as_user", return_value="/srv/work/myapp"
            ),
            mock.patch.object(
                run_app.launch_profile_app, "resolve_launch_profile", return_value=resolved
            ),
            mock.patch.object(launch_app, "plan_worktree_launch", fake_plan),
        ):
            # dry_run=True → no execvp; do_run returns 0 after printing.
            rc = run_app.do_run(args, cfg, "dana_agent")
        self.assertEqual(rc, 0)
        self.assertEqual(captured["repo"], "/srv/work/myapp")
        self.assertEqual(captured["branch"], "feature/auth")
        self.assertEqual(captured["profile"], "claude")
        self.assertEqual(captured["requested_profile"], "claude")
        self.assertTrue(captured["dry_run"])  # dry_run threaded through


def _init_repo(path: str) -> None:
    subprocess.run(["git", "init", "-q", path], check=True)
    subprocess.run(["git", "-C", path, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", path, "config", "user.name", "t"], check=True)


class ExcludeWriterTests(unittest.TestCase):
    def test_appends_uxon_line_once_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            _init_repo(d)
            # Launch user == process user so the sudo prefix collapses —
            # the CI runner has no fixed username to hard-code.
            with mock.patch("uxon.infra.identity.process_user", return_value="dana_agent"):
                git.write_uxon_exclude_entry(d, "dana_agent")
                git.write_uxon_exclude_entry(d, "dana_agent")  # idempotent
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
            # Same process_user collapse as ExcludeWriterTests above.
            with mock.patch("uxon.infra.identity.process_user", return_value="dana_agent"):
                git.copy_worktreeinclude_matches(d, dest, "dana_agent")
            self.assertTrue(os.path.exists(os.path.join(dest, ".env")))
            self.assertFalse(os.path.exists(os.path.join(dest, "debug.log")))
            self.assertFalse(os.path.exists(os.path.join(dest, "tracked.txt")))

    def test_no_worktreeinclude_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            _init_repo(d)
            dest = os.path.join(d, "dest")
            os.makedirs(dest)
            git.copy_worktreeinclude_matches(d, dest, "dana_agent")  # no raise
            self.assertEqual(os.listdir(dest), [])


class BuildTuiContextWorktreeWiringTests(unittest.TestCase):
    def test_probe_worktrees_returns_workspaces(self) -> None:
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

        cfg = config_loader.load_config("/tmp")
        with (
            mock.patch(
                "uxon.infra.git.git_repo_root_nonint_as_user", return_value="/srv/work/myapp"
            ),
            mock.patch(
                "uxon.infra.git.git_common_dir_root_as_user", return_value="/srv/work/myapp"
            ),
            mock.patch.object(tui_bridge.subprocess, "run", fake_run),
            mock.patch("uxon.infra.identity.process_user", return_value="dana_agent"),
        ):
            ctx = context_builder.build_tui_context(
                cfg, "dana_agent", "dana_agent", "/srv/work/myapp", skeleton=True
            )
            rows = ctx.on_probe_worktrees("/srv/work/myapp")
        self.assertTrue(rows[0].is_primary)
        self.assertEqual(rows[1].branch, "feature/auth")

    def test_probe_worktrees_non_git_returns_empty(self) -> None:
        cfg = config_loader.load_config("/tmp")
        with (
            mock.patch("uxon.infra.git.git_repo_root_nonint_as_user", return_value=None),
            mock.patch("uxon.infra.identity.process_user", return_value="dana_agent"),
        ):
            ctx = context_builder.build_tui_context(
                cfg, "dana_agent", "dana_agent", "/tmp/plain", skeleton=True
            )
            self.assertEqual(ctx.on_probe_worktrees("/tmp/plain"), [])

    def test_probe_worktrees_git_failure_raises(self) -> None:
        """A real repo whose ``git worktree list`` errors makes the probe RAISE
        (carrying stderr) — distinct from the non-git empty-list path, so the
        TUI can show an error row, not "no repo". The bridge raises
        ``WorktreeProbeError``; the context-level callback wrapper re-raises it
        as ``CallbackError`` with the message preserved (this is what the probe
        worker catches and stringifies into the WORKSPACE error row)."""
        from uxon.tui.context import CallbackError

        def fake_run(cmd, **kw):
            class CP:
                returncode = 128
                stdout = ""
                stderr = "fatal: not a git repository: '.git'\n"

            return CP()

        cfg = config_loader.load_config("/tmp")
        with (
            mock.patch(
                "uxon.infra.git.git_repo_root_nonint_as_user", return_value="/srv/work/myapp"
            ),
            mock.patch(
                "uxon.infra.git.git_common_dir_root_as_user", return_value="/srv/work/myapp"
            ),
            mock.patch.object(tui_bridge.subprocess, "run", fake_run),
            mock.patch("uxon.infra.identity.process_user", return_value="dana_agent"),
        ):
            ctx = context_builder.build_tui_context(
                cfg, "dana_agent", "dana_agent", "/srv/work/myapp", skeleton=True
            )
            with self.assertRaises(CallbackError) as caught:
                ctx.on_probe_worktrees("/srv/work/myapp")
        self.assertIn("fatal: not a git repository", str(caught.exception))


class ProbeExistingWorktreeSessionsCallbackTests(unittest.TestCase):
    def test_callback_uses_worktree_stem(self) -> None:
        repo = "/srv/work/myapp"
        wt = "/srv/work/myapp/.uxon/worktrees/feature-auth"
        sess = domain_session.SessionInfo(
            user="dana_agent",
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
        cfg = config_loader.load_config("/tmp")
        resolved = _resolved_for_test(cfg, launch_user="dana_agent")
        with (
            mock.patch.object(
                tui_bridge.launch_profile_app, "resolve_launch_profile", return_value=resolved
            ),
            mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[sess]),
            mock.patch("uxon.infra.git.git_repo_root_nonint_as_user", return_value=repo),
            mock.patch("uxon.infra.git.git_common_dir_root_as_user", return_value=repo),
            mock.patch("uxon.infra.identity.process_user", return_value="dana_agent"),
        ):
            ctx = context_builder.build_tui_context(
                cfg, "dana_agent", "dana_agent", repo, skeleton=True
            )
            out = ctx.on_probe_existing_worktree_sessions(
                wt, repo, "feature/auth", "claude", "normal"
            )
        self.assertEqual(out, (("dana_agent", "uxon-myapp-feature-auth@claude", True),))

    def test_session_probe_uses_resolved_launch_user(self) -> None:
        cfg = config_loader.load_config("/tmp")
        resolved = _resolved_for_test(cfg, launch_user="profile_user")
        captured: dict[str, str] = {}

        def fake_probe(cfg_arg, launch_user, target_dir, profile_id, **kwargs):
            captured.update(user=launch_user, target=target_dir, profile=profile_id)
            return []

        with (
            mock.patch.object(
                tui_bridge.launch_profile_app, "resolve_launch_profile", return_value=resolved
            ),
            mock.patch(
                "uxon.infra.sessions_probe.probe_tui_compatible_sessions",
                side_effect=fake_probe,
            ),
        ):
            ctx = context_builder.build_tui_context(
                cfg, "caller", "startup_user", "/srv/work", skeleton=True
            )
            self.assertEqual(ctx.on_probe_existing_sessions("/srv/work", "claude", "normal"), ())

        self.assertEqual(captured["user"], "profile_user")
        self.assertEqual(captured["profile"], "claude")

    def test_container_gate_uses_resolved_launch_user(self) -> None:
        cfg = config_loader.load_config("/tmp")
        resolved = _resolved_for_test(cfg, launch_user="profile_user")

        with (
            mock.patch.object(
                tui_bridge.launch_profile_app, "resolve_launch_profile", return_value=resolved
            ),
            mock.patch.object(
                tui_bridge.launch_app, "decide_container_gate", return_value=None
            ) as gate,
        ):
            ctx = context_builder.build_tui_context(
                cfg, "caller", "startup_user", "/srv/work", skeleton=True
            )
            self.assertIsNone(ctx.on_container_gate("/srv/work", "claude", "normal"))

        gate.assert_called_once_with(cfg, "/srv/work", resolved)


if __name__ == "__main__":
    unittest.main()

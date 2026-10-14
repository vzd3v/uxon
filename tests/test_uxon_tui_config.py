"""Configuration snapshot preserves nondefault policy and callback boundaries."""

from __future__ import annotations

import unittest

from uxon.tui.config import TuiConfig
from uxon.tui.context import LaunchRequest, TuiContext
from uxon.tui.refresh import SourceSpec


def _mk_ctx(**overrides) -> TuiContext:
    base = dict(
        sessions=[],
        total_cpu="0",
        total_ram="0",
        version="0.0.0-test",
        cwd="/srv/work",
        cwd_short="work",
        new_project_root="/srv/work",
        existing_projects=[],
        cwd_writable=True,
        current_user="dana_agent",
        launch_user="dana_agent",
        enabled_profiles=("claude", "codex"),
        default_profile="claude",
        tui_refresh_interval_seconds=2.5,
        tui_ssh_refresh_interval_seconds=11.0,
        ssh_multiplex="off",
        fetch_concurrency=8,
        git_create_enabled=True,
        on_attach=lambda u, n: LaunchRequest(cmd=("/bin/true",), label="attach"),
        on_kill=lambda u, n: None,
        on_kill_all=lambda: None,
        on_kill_all_reachable=lambda: None,
        on_remote_kill=lambda h, u, n: None,
        on_remote_attach=lambda h, u, n: LaunchRequest(cmd=("/bin/true",), label="remote-attach"),
        on_refresh=lambda: _mk_ctx(),
        on_probe_link_health=lambda: None,
        on_probe_cwd_writable=lambda: True,
        on_launch_cwd=lambda a, m, target_dir=None: LaunchRequest(cmd=("/bin/true",), label="cwd"),
        on_launch_new=lambda n, a, m, g: LaunchRequest(cmd=("/bin/true",), label="new"),
        on_launch_existing=lambda n, a, m: LaunchRequest(cmd=("/bin/true",), label="exist"),
        get_settings_entries=lambda: [],
        on_setting_save=lambda k, v: None,
        on_setting_remove=lambda k: None,
        on_setting_save_mapping=lambda k, m: None,
        get_git_remote_profile_rows=lambda: [],
    )
    base.update(overrides)
    return TuiContext(**base)


class FromContextRoundTripTests(unittest.TestCase):
    def test_every_field_round_trips(self) -> None:
        spec = SourceSpec(
            name="main_ctx_rebuild",
            fetch=lambda: None,  # type: ignore[arg-type]
            cadence_seconds_attr="tui_refresh_interval_seconds",
        )
        ctx = _mk_ctx()
        from uxon.domain.agents import DEFAULT_AGENT_CATALOG

        ctx.agents = dict(DEFAULT_AGENT_CATALOG)
        ctx.refresh_sources = [spec]
        from uxon.infra.remote_hosts import RemoteHost
        from uxon.tui.context import LaunchProfileOption

        peer = RemoteHost("peer", "peer-alias", "build host", "uxon")
        profile = LaunchProfileOption("ops", "Operations", "claude", "alice")
        ctx.remote_hosts = [peer]
        ctx.launch_profiles = {"ops": profile}
        cfg = TuiConfig.from_context(ctx)

        self.assertEqual(cfg.current_user, "dana_agent")
        self.assertEqual(cfg.launch_user, "dana_agent")
        self.assertEqual(cfg.enabled_profiles, ("claude", "codex"))
        self.assertEqual(cfg.default_profile, "claude")
        self.assertEqual(cfg.tui_refresh_interval_seconds, 2.5)
        self.assertEqual(cfg.tui_ssh_refresh_interval_seconds, 11.0)
        self.assertEqual(cfg.ssh_multiplex, "off")
        self.assertEqual(cfg.fetch_concurrency, 8)
        self.assertEqual(cfg.remote_hosts, (peer,))
        self.assertEqual(cfg.launch_profiles, {"ops": profile})
        self.assertEqual(cfg.refresh_sources, (spec,))
        self.assertTrue(cfg.git_create_enabled)

        # Callable identity is preserved (callbacks pass through by reference).
        self.assertIs(cfg.on_attach, ctx.on_attach)
        self.assertIs(cfg.on_kill, ctx.on_kill)
        self.assertIs(cfg.on_kill_all, ctx.on_kill_all)
        self.assertIs(cfg.on_kill_all_reachable, ctx.on_kill_all_reachable)
        self.assertIs(cfg.on_remote_kill, ctx.on_remote_kill)
        self.assertIs(cfg.on_remote_attach, ctx.on_remote_attach)
        self.assertIs(cfg.on_refresh, ctx.on_refresh)
        self.assertIs(cfg.on_probe_link_health, ctx.on_probe_link_health)
        self.assertIs(cfg.on_probe_cwd_writable, ctx.on_probe_cwd_writable)
        self.assertIs(cfg.on_launch_cwd, ctx.on_launch_cwd)
        self.assertIs(cfg.on_launch_new, ctx.on_launch_new)
        self.assertIs(cfg.on_launch_existing, ctx.on_launch_existing)
        self.assertIs(cfg.get_settings_entries, ctx.get_settings_entries)
        self.assertIs(cfg.on_setting_save, ctx.on_setting_save)
        self.assertIs(cfg.on_setting_remove, ctx.on_setting_remove)
        self.assertIs(cfg.on_setting_save_mapping, ctx.on_setting_save_mapping)
        self.assertIs(cfg.get_git_remote_profile_rows, ctx.get_git_remote_profile_rows)
        ctx.remote_hosts.clear()
        ctx.refresh_sources.clear()
        ctx.launch_profiles.clear()
        ctx.agents.clear()
        self.assertEqual(cfg.remote_hosts, (peer,))
        self.assertEqual(cfg.refresh_sources, (spec,))
        self.assertEqual(cfg.launch_profiles, {"ops": profile})
        self.assertIn("claude", cfg.agents)


class WorktreeCallbacksRoundTripTests(unittest.TestCase):
    def test_from_context_snapshots_worktree_callbacks(self) -> None:
        sentinel_probe = lambda cwd: []  # noqa: E731
        sentinel_create = lambda repo, branch, agent, mode: LaunchRequest(  # noqa: E731
            cmd=("/bin/true",), label="create"
        )
        sentinel_launch = lambda repo, branch, path, agent, mode: LaunchRequest(  # noqa: E731
            cmd=("/bin/true",), label="launch-existing"
        )
        sentinel_probe_sess = lambda path, repo, branch, agent: ()  # noqa: E731
        ctx = _mk_ctx(
            on_probe_worktrees=sentinel_probe,
            on_create_worktree=sentinel_create,
            on_launch_existing_worktree=sentinel_launch,
            on_probe_existing_worktree_sessions=sentinel_probe_sess,
        )
        cfg = TuiConfig.from_context(ctx)
        self.assertIs(cfg.on_probe_worktrees, sentinel_probe)
        self.assertIs(cfg.on_create_worktree, sentinel_create)
        self.assertIs(cfg.on_launch_existing_worktree, sentinel_launch)
        self.assertIs(cfg.on_probe_existing_worktree_sessions, sentinel_probe_sess)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import dataclasses
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import make_config

import uxon.app.launch as launch_app
import uxon.app.launch_profile as launch_profile_app
import uxon.app.new as new_app
import uxon.app.run as run_app
import uxon.app.tui_planning as tui_planning
from uxon.domain.agents import default_agent_catalog
from uxon.domain.args import ParsedArgs
from uxon.domain.host_report import BinaryStatus, HostReport
from uxon.domain.launch_profiles import (
    GitRemotePolicy,
    LaunchConfig,
    LaunchPathRule,
    LaunchProfile,
    ResolvedLaunchProfile,
    builtin_launch_profiles,
)
from uxon.domain.launch_request import LaunchRequest
from uxon.gitremote.create import CreationResult


def _report(launch_user: str, installed: tuple[str, ...]) -> HostReport:
    agents = default_agent_catalog()
    return HostReport(
        tmux=BinaryStatus("tmux", "/usr/bin/tmux", ""),
        agents={
            aid: BinaryStatus(
                aid, f"/usr/bin/{agents[aid].binary}" if aid in installed else None, ""
            )
            for aid in agents
        },
        launch_user=launch_user,
    )


def _cfg_with_launch(launch: LaunchConfig, **overrides):
    agents = default_agent_catalog()
    return make_config(agents=agents, launch=launch, **overrides)


def _resolved(
    cfg,
    profile_id: str,
    *,
    launch_user: str = "dana_agent",
    mode: str = "normal",
) -> ResolvedLaunchProfile:
    profile = cfg.launch.profiles[profile_id]
    return ResolvedLaunchProfile(
        profile=profile,
        agent=cfg.agents[profile.agent],
        launch_user=launch_user,
        mode_id=mode,
        git_remote=profile.git_remote,
    )


class LaunchProfileResolutionTests(unittest.TestCase):
    def _builtin_launch(self, *, enabled=(), default="claude", rules=()) -> LaunchConfig:
        agents = default_agent_catalog()
        return LaunchConfig(
            enabled_profiles=tuple(enabled),
            default_profile=default,
            profiles=builtin_launch_profiles(agents),
            path_rules=tuple(rules),
        )

    def test_path_disallowed_profile_fails_before_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = str(Path(tmp) / "app")
            Path(app).mkdir()
            launch = self._builtin_launch(
                enabled=("claude", "codex"),
                default="claude",
                rules=(
                    LaunchPathRule(
                        path_prefix=app,
                        allowed_profiles=("codex",),
                        default_profile="codex",
                    ),
                ),
            )
            cfg = _cfg_with_launch(launch, allowed_roots=[tmp])

            with mock.patch("uxon.infra.probes.probe_host") as probe:
                with self.assertRaises(SystemExit) as cm:
                    launch_profile_app.resolve_launch_profile(
                        cfg, "alice", "claude", str(Path(app) / "sub"), "normal"
                    )

            probe.assert_not_called()
            msg = getattr(cm.exception, "uxon_msg", "")
            self.assertIn("valid profiles: codex", msg)

    def test_path_rule_matching_is_component_aware(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "app"
            app_old = Path(tmp) / "app-old"
            app.mkdir()
            app_old.mkdir()
            launch = self._builtin_launch(
                enabled=("claude", "codex"),
                default="claude",
                rules=(
                    LaunchPathRule(
                        path_prefix=str(app),
                        allowed_profiles=("codex",),
                        default_profile="codex",
                    ),
                ),
            )
            cfg = _cfg_with_launch(launch, allowed_roots=[tmp])

            with mock.patch(
                "uxon.infra.probes.probe_host",
                return_value=_report("alice", ("claude", "codex")),
            ):
                resolved = launch_profile_app.resolve_launch_profile(
                    cfg, "alice", None, str(app_old), "normal"
                )

            self.assertEqual(resolved.profile.id, "claude")

    def test_existing_target_canonicalizes_symlinks_before_path_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real"
            real.mkdir()
            project = real / "project"
            project.mkdir()
            link = Path(tmp) / "link"
            os.symlink(real, link)
            launch = self._builtin_launch(
                enabled=("claude", "codex"),
                default="claude",
                rules=(
                    LaunchPathRule(
                        path_prefix=str(real),
                        allowed_profiles=("codex",),
                        default_profile="codex",
                    ),
                ),
            )
            cfg = _cfg_with_launch(launch, allowed_roots=[tmp])

            with mock.patch(
                "uxon.infra.probes.probe_host",
                return_value=_report("alice", ("codex",)),
            ):
                resolved = launch_profile_app.resolve_launch_profile(
                    cfg, "alice", None, str(link / "project"), "normal"
                )

            self.assertEqual(resolved.profile.id, "codex")

    def test_auto_mode_probes_only_builtin_os_user_profiles_and_freezes_one(self) -> None:
        agents = default_agent_catalog()
        profiles = builtin_launch_profiles(agents)
        profiles["claude_sub1"] = LaunchProfile(
            id="claude_sub1",
            agent="claude",
            launch_user="dana_agent",
        )
        launch = LaunchConfig(profiles=profiles)
        cfg = _cfg_with_launch(launch)
        seen_catalogs: list[tuple[str, ...]] = []

        def fake_probe(_cfg, launch_user, catalog):
            seen_catalogs.append(tuple(catalog))
            return _report(launch_user, ("codex",))

        with mock.patch("uxon.infra.probes.probe_host", side_effect=fake_probe):
            resolved = launch_profile_app.resolve_launch_profile(
                cfg, "alice", None, "/srv/repos/demo", None
            )

        self.assertEqual(resolved.profile.id, "codex")
        self.assertEqual(seen_catalogs, [("claude", "codex", "cursor")])

    def test_pinned_launch_user_controls_tmux_and_agent_probe(self) -> None:
        agents = default_agent_catalog()
        profiles = builtin_launch_profiles(agents)
        profiles["claude_sub1"] = LaunchProfile(
            id="claude_sub1",
            agent="claude",
            launch_user="dana_agent",
        )
        launch = LaunchConfig(
            enabled_profiles=("claude_sub1",),
            default_profile="claude_sub1",
            profiles=profiles,
        )
        cfg = _cfg_with_launch(launch)

        with mock.patch(
            "uxon.infra.probes.probe_host",
            return_value=_report("dana_agent", ("claude",)),
        ) as probe:
            resolved = launch_profile_app.resolve_launch_profile(
                cfg, "alice", "claude_sub1", "/srv/repos/demo", "normal"
            )

        self.assertEqual(resolved.launch_user, "dana_agent")
        probe.assert_called_once()
        self.assertIs(probe.call_args.args[0], cfg)
        self.assertEqual(probe.call_args.args[1], "dana_agent")
        self.assertEqual(tuple(probe.call_args.args[2]), ("claude",))

    def test_unknown_disabled_and_path_disallowed_messages_list_choices(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "app"
            app.mkdir()
            launch = self._builtin_launch(
                enabled=("codex",),
                default="codex",
                rules=(
                    LaunchPathRule(
                        path_prefix=str(app),
                        allowed_profiles=("codex",),
                        default_profile="codex",
                    ),
                ),
            )
            cfg = _cfg_with_launch(launch, allowed_roots=[tmp])

            with self.assertRaises(SystemExit) as unknown:
                launch_profile_app.resolve_launch_profile(cfg, "alice", "missing", str(app), None)
            self.assertIn("valid profiles: codex", getattr(unknown.exception, "uxon_msg", ""))

            with self.assertRaises(SystemExit) as disabled:
                launch_profile_app.resolve_launch_profile(cfg, "alice", "claude", str(app), None)
            self.assertIn("valid profiles: codex", getattr(disabled.exception, "uxon_msg", ""))


class LaunchProfileRuntimeGateTests(unittest.TestCase):
    def test_run_allocates_session_with_profile_suffix(self) -> None:
        agents = default_agent_catalog()
        profiles = builtin_launch_profiles(agents)
        profiles["claude_sub1"] = LaunchProfile(id="claude_sub1", agent="claude")
        launch = LaunchConfig(
            enabled_profiles=("claude_sub1",),
            default_profile="claude_sub1",
            profiles=profiles,
        )
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg_with_launch(launch, allowed_roots=[tmp])
            resolved = dataclasses.replace(
                _resolved(cfg, "claude_sub1", launch_user="alice"), canonical_target=tmp
            )
            captured: dict[str, str] = {}

            def fake_launch(target_dir, session, *args, **kwargs):
                captured["session"] = session
                captured["target_dir"] = target_dir
                return 0

            with (
                mock.patch.object(os, "getcwd", return_value=tmp),
                mock.patch.object(
                    run_app.launch_profile_app, "resolve_launch_profile", return_value=resolved
                ),
                mock.patch("uxon.infra.identity.probe_cwd_writable", return_value=True),
                mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[]),
                mock.patch("uxon.infra.sessions_probe.legacy_compatible_sessions", return_value=[]),
                mock.patch("uxon.infra.tmux.launch_in_tmux", side_effect=fake_launch),
            ):
                rc = run_app.do_run(ParsedArgs(action="run", profile="claude_sub1"), cfg, "alice")

        self.assertEqual(rc, 0)
        self.assertTrue(captured["session"].endswith("@claude_sub1"))

    def test_run_worktree_rejects_profile_for_worktree_path_before_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "demo"
            repo.mkdir()
            worktree = repo / ".uxon" / "worktrees" / "feature-x"
            launch = LaunchConfig(
                enabled_profiles=("claude", "codex"),
                default_profile="claude",
                profiles=builtin_launch_profiles(default_agent_catalog()),
                path_rules=(
                    LaunchPathRule(
                        path_prefix=str(worktree),
                        allowed_profiles=("codex",),
                        default_profile="codex",
                    ),
                ),
            )
            cfg = _cfg_with_launch(launch, allowed_roots=[tmp])
            args = ParsedArgs(
                action="run",
                profile="claude",
                worktree_branch="feature/x",
                dry_run=True,
            )

            with (
                mock.patch.object(os, "getcwd", return_value=str(repo)),
                mock.patch("uxon.infra.git.git_repo_root_nonint_as_user", return_value=str(repo)),
                mock.patch("uxon.infra.git.git_common_dir_root_as_user", return_value=str(repo)),
                mock.patch("uxon.infra.probes.probe_host") as probe,
                mock.patch.object(launch_app, "plan_worktree_launch") as plan,
            ):
                with self.assertRaises(SystemExit) as cm:
                    run_app.do_run(args, cfg, "alice")

        self.assertIn("valid profiles: codex", getattr(cm.exception, "uxon_msg", ""))
        probe.assert_not_called()
        plan.assert_not_called()

    def test_new_worktree_rejects_profile_for_worktree_path_before_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            project.mkdir()
            worktree = project / ".uxon" / "worktrees" / "feature-x"
            launch = LaunchConfig(
                enabled_profiles=("claude", "codex"),
                default_profile="claude",
                profiles=builtin_launch_profiles(default_agent_catalog()),
                path_rules=(
                    LaunchPathRule(
                        path_prefix=str(worktree),
                        allowed_profiles=("codex",),
                        default_profile="codex",
                    ),
                ),
            )
            cfg = _cfg_with_launch(launch, allowed_roots=[tmp], new_project_root=tmp)
            args = ParsedArgs(
                action="new",
                target_id="demo",
                profile="claude",
                worktree_branch="feature/x",
                dry_run=True,
            )

            with (
                mock.patch("uxon.infra.git.git_repo_root_as_user", return_value=str(project)),
                mock.patch("uxon.infra.git.git_common_dir_root_as_user", return_value=str(project)),
                mock.patch("uxon.infra.probes.probe_host") as probe,
                mock.patch.object(launch_app, "plan_worktree_launch") as plan,
            ):
                with self.assertRaises(SystemExit) as cm:
                    new_app.do_new(args, cfg, "alice")

        self.assertIn("valid profiles: codex", getattr(cm.exception, "uxon_msg", ""))
        probe.assert_not_called()
        plan.assert_not_called()

    def test_new_revalidates_after_mkdir_before_git_remote_create(self) -> None:
        profile = {"name": "work", "host": "github.com", "owner": "acme", "auth": "gh"}
        from uxon.domain import git_profiles

        agents = default_agent_catalog()
        profiles = builtin_launch_profiles(agents)
        profiles["claude"] = dataclasses.replace(
            profiles["claude"],
            git_remote=GitRemotePolicy(allowed_profiles=("work",), default_profile="work"),
        )
        launch = LaunchConfig(default_profile="claude", profiles=profiles)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg_with_launch(
                launch,
                allowed_roots=[tmp],
                new_project_root=tmp,
                git_create_enabled=True,
                git_remote_profiles=git_profiles.load_profiles([profile]),
            )
            resolved = dataclasses.replace(
                _resolved(cfg, "claude", launch_user="alice"),
                canonical_target=str(Path(tmp) / "demo"),
            )
            calls: list[list[str]] = []

            def fake_run_cmd(cmd, **kwargs):
                calls.append(cmd)
                Path(tmp, "demo").mkdir(exist_ok=True)

                class CP:
                    returncode = 0
                    stdout = ""
                    stderr = ""

                return CP()

            with (
                mock.patch.object(
                    new_app.launch_profile_app, "resolve_launch_profile", return_value=resolved
                ),
                mock.patch.object(
                    new_app.launch_profile_app,
                    "revalidate_launch_profile",
                    side_effect=SystemExit(2),
                ),
                mock.patch("uxon.infra.identity.probe_cwd_writable", return_value=True),
                mock.patch("uxon.infra.process.run_cmd", side_effect=fake_run_cmd),
                mock.patch(
                    "uxon.gitremote.create.create_project_remote",
                    return_value=CreationResult("work", "git@github.com:acme/demo.git"),
                ) as create_remote,
            ):
                with self.assertRaises(SystemExit):
                    new_app.do_new(
                        ParsedArgs(action="new", target_id="demo", git_remote="default"),
                        cfg,
                        "alice",
                    )

        self.assertTrue(calls)
        create_remote.assert_not_called()

    def test_tui_new_revalidates_after_mkdir_before_git_remote_create(self) -> None:
        profile = {"name": "work", "host": "github.com", "owner": "acme", "auth": "gh"}
        from uxon.domain import git_profiles

        agents = default_agent_catalog()
        profiles = builtin_launch_profiles(agents)
        profiles["claude"] = dataclasses.replace(
            profiles["claude"],
            git_remote=GitRemotePolicy(allowed_profiles=("work",), default_profile="work"),
        )
        launch = LaunchConfig(default_profile="claude", profiles=profiles)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg_with_launch(
                launch,
                allowed_roots=[tmp],
                new_project_root=tmp,
                git_create_enabled=True,
                git_remote_profiles=git_profiles.load_profiles([profile]),
            )
            resolved = dataclasses.replace(
                _resolved(cfg, "claude", launch_user="alice"),
                canonical_target=str(Path(tmp) / "demo"),
            )
            calls: list[list[str]] = []

            def fake_run_cmd(cmd, **kwargs):
                calls.append(cmd)
                Path(tmp, "demo").mkdir(exist_ok=True)

                class CP:
                    returncode = 0
                    stdout = ""
                    stderr = ""

                return CP()

            with (
                mock.patch.object(
                    tui_planning.launch_profile_app,
                    "resolve_launch_profile",
                    return_value=resolved,
                ),
                mock.patch.object(
                    tui_planning.launch_profile_app,
                    "revalidate_launch_profile",
                    side_effect=SystemExit(2),
                ),
                mock.patch("uxon.infra.identity.probe_cwd_writable", return_value=True),
                mock.patch("uxon.infra.process.run_cmd", side_effect=fake_run_cmd),
                mock.patch.object(tui_planning.new_app, "_do_create_git_remote") as create_remote,
                mock.patch.object(tui_planning.launch_app, "ensure_runtime_ready"),
            ):
                with self.assertRaises(SystemExit):
                    tui_planning._plan_tui_create_new_agent(
                        cfg, "alice", "alice", "demo", "claude", "normal", "default"
                    )

        self.assertTrue(calls)
        create_remote.assert_not_called()

    def test_tui_new_uses_caller_for_resolution_and_resolved_user_for_container(self) -> None:
        launch = LaunchConfig(
            default_profile="claude",
            profiles=builtin_launch_profiles(default_agent_catalog()),
        )
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg_with_launch(launch, allowed_roots=[tmp], new_project_root=tmp)
            resolved = dataclasses.replace(
                _resolved(cfg, "claude", launch_user="profile_user"),
                canonical_target=str(Path(tmp) / "demo"),
            )

            def fake_run_cmd(cmd, **kwargs):
                Path(tmp, "demo").mkdir(exist_ok=True)

                class CP:
                    returncode = 0
                    stdout = ""
                    stderr = ""

                return CP()

            def fake_resolve(cfg_arg, caller_user, *args, **kwargs):
                self.assertIs(cfg_arg, cfg)
                self.assertEqual(caller_user, "erin")
                return resolved

            def fake_revalidate(cfg_arg, caller_user, *args, **kwargs):
                self.assertIs(cfg_arg, cfg)
                self.assertEqual(caller_user, "erin")
                return resolved

            with (
                mock.patch.object(
                    tui_planning.launch_profile_app,
                    "resolve_launch_profile",
                    side_effect=fake_resolve,
                ),
                mock.patch.object(
                    tui_planning.launch_profile_app,
                    "revalidate_launch_profile",
                    side_effect=fake_revalidate,
                ),
                mock.patch("uxon.infra.identity.probe_cwd_writable", return_value=True),
                mock.patch("uxon.infra.process.run_cmd", side_effect=fake_run_cmd),
                mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[]),
                mock.patch("uxon.infra.sessions_probe.legacy_compatible_sessions", return_value=[]),
                mock.patch.object(tui_planning.launch_app, "ensure_runtime_ready") as ready,
                mock.patch(
                    "uxon.infra.tmux._build_tmux_launch_request",
                    return_value=LaunchRequest(cmd=("true",), label="launch x"),
                ),
            ):
                tui_planning._plan_tui_create_new_agent(
                    cfg, "erin", "dana_agent", "demo", "claude", "normal", ""
                )

        ready.assert_called_once_with(cfg, str(Path(tmp) / "demo"), resolved)

    def test_tui_open_existing_resolves_before_gate_and_does_not_mkdir(self) -> None:
        launch = LaunchConfig(
            default_profile="claude",
            profiles=builtin_launch_profiles(default_agent_catalog()),
        )
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            project.mkdir()
            cfg = _cfg_with_launch(launch, allowed_roots=[tmp], new_project_root=tmp)
            resolved = dataclasses.replace(
                _resolved(cfg, "claude", launch_user="profile_user"),
                canonical_target=str(project),
            )

            with (
                mock.patch.object(
                    tui_planning.launch_profile_app,
                    "resolve_launch_profile",
                    return_value=resolved,
                ) as resolve,
                mock.patch.object(tui_planning.launch_app, "ensure_launch_target_allowed") as gate,
                mock.patch("uxon.infra.process.run_cmd") as run_cmd,
                mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[]),
                mock.patch("uxon.infra.sessions_probe.legacy_compatible_sessions", return_value=[]),
                mock.patch(
                    "uxon.infra.tmux._build_tmux_launch_request",
                    return_value=LaunchRequest(cmd=("true",), label="launch x"),
                ),
            ):
                tui_planning._plan_tui_open_existing_agent(
                    cfg, "erin", "startup_user", "demo", "claude", "normal"
                )

        resolve.assert_called_once()
        self.assertEqual(resolve.call_args.args[1], "erin")
        gate.assert_called_once_with(cfg, "profile_user", str(project))
        run_cmd.assert_not_called()

    def test_worktree_revalidates_after_add_before_runtime_prepare(self) -> None:
        cfg = make_config(allowed_roots=["/srv/work"])
        resolved = _resolved(cfg, "claude")
        calls: list[list[str]] = []

        def fake_run_cmd(cmd, check=True, **kwargs):
            calls.append(cmd)

            class CP:
                returncode = 0
                stdout = ""
                stderr = ""

            return CP()

        with (
            mock.patch.object(launch_app, "is_worktree_target_allowed", return_value=True),
            mock.patch("uxon.infra.sessions_probe.collect_sessions", return_value=[]),
            mock.patch("uxon.infra.git._branch_exists_as_user", return_value=True),
            mock.patch("uxon.infra.process.run_cmd", side_effect=fake_run_cmd),
            mock.patch("uxon.infra.git.write_uxon_exclude_entry", lambda *a, **k: None),
            mock.patch(
                "uxon.infra.tmux._build_tmux_launch_request",
                return_value=type("Req", (), {"cmd": ("true",), "prelaunch": (), "label": "x"})(),
            ),
            mock.patch.object(
                launch_app.launch_profile_app,
                "revalidate_launch_profile",
                side_effect=SystemExit(2),
            ),
            mock.patch.object(launch_app, "ensure_runtime_ready") as runtime_ready,
        ):
            with self.assertRaises(SystemExit):
                launch_app.plan_worktree_launch(
                    cfg,
                    "alice",
                    resolved,
                    "/srv/work/myapp",
                    "feature/auth",
                    requested_profile="claude",
                )

        self.assertTrue([cmd for cmd in calls if "worktree" in cmd and "add" in cmd])
        runtime_ready.assert_not_called()


if __name__ == "__main__":
    unittest.main()

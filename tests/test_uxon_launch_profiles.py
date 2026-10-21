"""Real path policy and launch planners under a strict target command boundary."""

from __future__ import annotations

import dataclasses
import io
import json
import shlex
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from harness.target_commands import TargetCommands, target_config

from uxon.app import launch, new, run
from uxon.app.launch_profile import resolve_launch_profile
from uxon.domain.args import ParsedArgs
from uxon.domain.git_profiles import GitRemoteProfile
from uxon.domain.launch_profiles import GitRemotePolicy, LaunchConfig, LaunchPathRule, LaunchProfile
from uxon.domain.runtime import WorkloadRuntimeSpec
from uxon.infra.worktrees import compute_worktree_path
from uxon.tui.bridge import TuiBridge

pytestmark = pytest.mark.usefixtures("real_execution", "real_host_probes")


def _cp(stdout="", rc=0):
    return subprocess.CompletedProcess([], rc, stdout, "")


def _cfg(root, **changes):
    return target_config(
        allowed_roots=[str(root)],
        new_project_root=str(root),
        tmux_manage_options=False,
        tmux_options={},
        tmux_server_options={},
        tmux_append_server_options={},
        **changes,
    )


def _server(user, command, kwargs):
    assert user in {"alice", "bob"}
    if "uxon.infra.tmux_server_probe" in command:
        return _cp(json.dumps({"state": "absent", "sessions": [], "error": ""}))
    raise AssertionError(command)


def _pinned(cfg, profile="claude_fast", runtime="direct"):
    cfg.launch = LaunchConfig(
        default_profile=profile,
        enabled_profiles=(profile,),
        profiles={
            profile: LaunchProfile(id=profile, agent="claude", launch_user="alice", runtime=runtime)
        },
    )


def test_profile_rejections_precede_host_discovery_and_mutations(tmp_path, subtests):
    target = tmp_path / "app"
    target.mkdir()
    for label in ("unknown", "disabled", "path-denied", "mode"):
        with subtests.test(label=label):
            cfg = _cfg(tmp_path)
            cfg.launch = dataclasses.replace(
                cfg.launch,
                enabled_profiles=("codex",) if label == "disabled" else ("claude", "codex"),
                path_rules=(
                    LaunchPathRule(
                        path_prefix=str(target),
                        allowed_profiles=("codex",),
                        default_profile="codex",
                    ),
                )
                if label == "path-denied"
                else (),
            )
            commands = TargetCommands()
            with commands.installed_boundary(), pytest.raises(SystemExit) as error:
                resolve_launch_profile(
                    cfg,
                    "alice",
                    "unknown" if label == "unknown" else "claude",
                    str(target),
                    "invalid" if label == "mode" else "normal",
                )
            assert {
                "unknown": "unknown --profile",
                "disabled": "disabled",
                "path-denied": "not allowed",
                "mode": "unknown --mode",
            }[label] in error.value.uxon_msg
            assert ("valid modes" if label == "mode" else "valid profiles") in error.value.uxon_msg
            assert not any(command[:2] == ["sh", "-c"] for _, command, _ in commands.calls)


def test_path_rules_apply_to_canonical_component_boundaries(tmp_path, subtests):
    app = tmp_path / "app"
    app.mkdir()
    (app / "nested").mkdir()
    neighbor = tmp_path / "app-old"
    neighbor.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(app, target_is_directory=True)
    cfg = _cfg(tmp_path)
    cfg.launch = dataclasses.replace(
        cfg.launch,
        path_rules=(
            LaunchPathRule(
                path_prefix=str(app), allowed_profiles=("codex",), default_profile="codex"
            ),
        ),
    )
    with TargetCommands().installed_boundary():
        for path, profile, canonical in (
            (app, "codex", app),
            (app / "nested", "codex", app / "nested"),
            (alias / "nested", "codex", app / "nested"),
            (neighbor, "claude", neighbor),
        ):
            with subtests.test(path=str(path)):
                resolved = resolve_launch_profile(cfg, "alice", None, str(path), "normal")
                assert (resolved.profile.id, resolved.canonical_target) == (profile, str(canonical))


def test_profile_selection_freezes_auto_and_effective_user_before_filesystem(tmp_path, subtests):
    target = tmp_path / "repo"
    target.mkdir()
    cfg = _cfg(tmp_path)
    cfg.runtimes["box"] = WorkloadRuntimeSpec(
        id="box",
        resource_name_template="box-{project_slug}",
        exec_prefix=("engine", "exec", "{resource}"),
    )
    profiles = dict(cfg.launch.profiles)
    profiles["operator"] = LaunchProfile(id="operator", agent="claude", launch_user="bob")
    profiles["runtime"] = LaunchProfile(
        id="runtime", agent="claude", runtime="box", launch_user="bob"
    )
    for label, installed, requested, user, profile, error in (
        ("auto", ("tmux", "codex"), None, "alice", "codex", None),
        ("pinned", ("tmux", "claude"), "operator", "bob", "operator", None),
        ("runtime", ("tmux",), "runtime", "bob", "runtime", None),
        ("direct-missing", ("tmux",), "claude", "alice", None, "not installed"),
    ):
        with subtests.test(label=label):
            cfg.launch = (
                LaunchConfig(profiles=dict(profiles), default_profile="", enabled_profiles=())
                if label == "auto"
                else LaunchConfig(
                    profiles=dict(profiles),
                    default_profile=requested,
                    enabled_profiles=(requested,),
                )
            )
            commands = TargetCommands(installed=installed)
            with commands.installed_boundary():
                if error:
                    with pytest.raises(SystemExit) as exc:
                        resolve_launch_profile(cfg, "alice", requested, str(target), "normal")
                    assert error in exc.value.uxon_msg
                else:
                    resolved = resolve_launch_profile(
                        cfg, "alice", requested, str(target), "normal"
                    )
                    assert (resolved.profile.id, resolved.launch_user, resolved.mode_id) == (
                        profile,
                        user,
                        "normal",
                    )
                    request = __import__(
                        "uxon.infra.tmux", fromlist=["tmux"]
                    )._build_tmux_launch_request(
                        str(target),
                        f"uxon-repo@{profile}",
                        ParsedArgs(action="run"),
                        cfg,
                        None,
                        resolved_profile=resolved,
                    )
                    assert (
                        request.managed.launch_user == user
                        and request.managed.launch_profile == profile
                    )
            assert (
                commands.calls[0][0] == user
                and commands.calls[0][1][-1] == "uxon.infra.execution_probe"
            )
            assert all(actual_user == user for actual_user, _, _ in commands.calls)
            if label == "auto":
                discovery = [
                    command[2] for _, command, _ in commands.calls if command[:2] == ["sh", "-c"]
                ]
                assert len(discovery) == 1 and "claude codex cursor-agent" in discovery[0]
                assert "engine" not in discovery[0]


def test_cli_run_emits_profile_suffix_and_preserves_running_empty_server(tmp_path, monkeypatch):
    project = tmp_path / "repo"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("TMUX", raising=False)
    cfg = _cfg(tmp_path)
    cfg.tmux_manage_options = True
    cfg.tmux_server_options = {"exit-empty": False}
    cfg.tmux_append_server_options = {"terminal-overrides": ",*:Tc"}
    _pinned(cfg)

    def respond(user, command, kwargs):
        if "uxon.infra.tmux_server_probe" in command:
            return _cp(json.dumps({"state": "running", "sessions": [], "error": ""}))
        assert command[:1] == ["tmux"] and command[3] == "list-sessions"
        return _cp()

    with TargetCommands(respond).installed_boundary(), redirect_stdout(io.StringIO()) as output:
        assert (
            run.do_run(
                ParsedArgs(action="run", profile="claude_fast", dry_run=True), cfg, "operator"
            )
            == 0
        )
    lines = output.getvalue().splitlines()
    assert "session=uxon-repo@claude_fast" in lines
    create = shlex.split(
        next(line.removeprefix("tmux_create=") for line in lines if line.startswith("tmux_create="))
    )
    assert create[:3] == ["boundary", "alice", "--"] and "new-session" in create
    assert "exit-empty" in create and "-as" not in create and ",*:Tc" not in create


def test_cli_worktree_routes_reject_final_profile_path_before_discovery_or_git_mutation(
    tmp_path, monkeypatch, subtests
):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    worktree = Path(
        compute_worktree_path(repo_root=str(repo), branch="feature/x", worktree_root="")
    )
    cfg = _cfg(tmp_path)
    cfg.launch = dataclasses.replace(
        cfg.launch,
        path_rules=(
            LaunchPathRule(
                path_prefix=str(worktree), allowed_profiles=("codex",), default_profile="codex"
            ),
        ),
    )
    for action in ("run", "new"):
        with subtests.test(action=action):

            def respond(user, command, kwargs):
                assert user == "operator" and command[:3] == ["git", "-C", str(repo)]
                assert "rev-parse" in command
                return _cp(str(repo / ".git" if "--git-common-dir" in command else repo) + "\n")

            commands = TargetCommands(respond)
            with commands.installed_boundary(), pytest.raises(SystemExit) as error:
                args = ParsedArgs(
                    action=action,
                    target_id="repo",
                    profile="claude",
                    worktree_branch="feature/x",
                    dry_run=True,
                )
                (run.do_run if action == "run" else new.do_new)(args, cfg, "operator")
            assert "valid profiles: codex" in error.value.uxon_msg
            assert not any(
                command[:2] in (["mkdir", "-p"], ["sh", "-c"]) or "add" in command
                for _, command, _ in commands.calls
            )


def test_new_planners_revalidate_actual_postcreate_path_and_rules_before_remote_create(
    tmp_path, monkeypatch, subtests
):
    for caller in ("cli", "tui"):
        for mutation in ("symlink", "rule"):
            with subtests.test(caller=caller, mutation=mutation):
                root = tmp_path / f"{caller}-{mutation}"
                root.mkdir()
                escape = root / "elsewhere"
                escape.mkdir()
                target = root / "project"
                cfg = _cfg(
                    root,
                    git_create_enabled=True,
                    git_remote_profiles=[
                        GitRemoteProfile(
                            "work", "github.com", "example", "gh", "alice", "", "private"
                        )
                    ],
                )
                _pinned(cfg, "claude")
                cfg.launch = dataclasses.replace(
                    cfg.launch,
                    profiles={
                        "claude": dataclasses.replace(
                            cfg.launch.profiles["claude"],
                            git_remote=GitRemotePolicy(("work",), "work"),
                        ),
                        "codex": LaunchProfile(id="codex", agent="codex"),
                    },
                    enabled_profiles=("claude", "codex"),
                )
                mutations = []

                def respond(
                    user,
                    command,
                    kwargs,
                    *,
                    cfg=cfg,
                    escape=escape,
                    mutation=mutation,
                    mutations=mutations,
                    target=target,
                ):
                    assert user == "alice" and command == ["mkdir", "-p", str(target)]
                    mutations.append(command)
                    if mutation == "symlink":
                        target.symlink_to(escape, target_is_directory=True)
                    else:
                        target.mkdir()
                        cfg.launch = dataclasses.replace(
                            cfg.launch,
                            path_rules=(
                                LaunchPathRule(
                                    path_prefix=str(target),
                                    allowed_profiles=("codex",),
                                    default_profile="codex",
                                ),
                            ),
                        )
                    return _cp()

                with (
                    TargetCommands(respond).installed_boundary(),
                    pytest.raises(SystemExit) as error,
                ):
                    if caller == "cli":
                        new.do_new(
                            ParsedArgs(
                                action="new",
                                target_id="project",
                                profile="claude",
                                git_remote="work",
                            ),
                            cfg,
                            "operator",
                        )
                    else:
                        TuiBridge(cfg, "operator", "startup", str(root)).on_launch_new(
                            "project", "claude", "normal", "work"
                        )
                assert (
                    len(mutations) == 1
                    and ("policy changed" if mutation == "symlink" else "not allowed")
                    in error.value.uxon_msg
                )


def test_tui_new_existing_and_cwd_callbacks_execute_real_planner_with_effective_user(
    tmp_path, monkeypatch, subtests
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("TMUX", raising=False)
    cfg = _cfg(tmp_path)
    _pinned(cfg)
    primary = tmp_path / "primary"
    primary.mkdir()
    cwd = tmp_path / "linked"
    cwd.mkdir()
    existing = tmp_path / "group" / "existing"
    existing.mkdir(parents=True)
    bridge = TuiBridge(cfg, "operator", "startup", str(cwd))
    mutations = []

    def respond(user, command, kwargs):
        if command[:2] == ["mkdir", "-p"]:
            assert user == "alice" and command == ["mkdir", "-p", str(tmp_path / "created")]
            Path(command[-1]).mkdir()
            mutations.append(command)
            return _cp()
        return _server(user, command, kwargs)

    external = TargetCommands(respond)
    with external.installed_boundary():
        for kind, target in (
            ("new", tmp_path / "created"),
            ("existing", existing),
            ("override", primary),
            ("fallback", cwd),
        ):
            with subtests.test(kind=kind):
                before = len(mutations)
                if kind == "new":
                    request = bridge.on_launch_new("created", "claude_fast", "normal", "")
                elif kind == "existing":
                    request = bridge.on_launch_existing("group/existing", "claude_fast", "normal")
                else:
                    request = bridge.on_launch_cwd(
                        "claude_fast", "normal", str(target) if kind == "override" else None
                    )
                assert request.managed.project == str(target)
                assert request.managed.record_session == f"uxon-{target.name}@claude_fast"
                assert (
                    request.managed.launch_user == "alice"
                    and request.managed.launch_profile == "claude_fast"
                )
                assert request.managed.create_cmd[:3] == ("boundary", "alice", "--")
                assert request.managed.create_cmd[
                    request.managed.create_cmd.index("-c") + 1
                ] == str(target)
                assert len(mutations) - before == int(kind == "new")
    assert all(user == "alice" for user, _, _ in external.calls)


def test_worktree_planner_revalidates_after_real_add_mutation_and_maps_final_workload(
    tmp_path, monkeypatch, subtests
):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    for label in ("symlink", "rule", "mapped", "empty-map", "unmapped", "component-neighbor"):
        with subtests.test(label=label):
            root = tmp_path / label
            root.mkdir()
            repo = root / "repo"
            repo.mkdir()
            workroot = root / "worktrees"
            path = Path(
                compute_worktree_path(
                    repo_root=str(repo), branch="feature/x", worktree_root=str(workroot)
                )
            )
            escape = root / "elsewhere"
            escape.mkdir()
            mapping = (
                ()
                if label == "empty-map"
                else ((str(repo), "/work"),)
                if label == "unmapped"
                else ((str(workroot) + "-other", "/work"),)
                if label == "component-neighbor"
                else ((str(workroot), "/work"),)
            )
            cfg = _cfg(root, worktree_root=str(workroot))
            cfg.runtimes["box"] = WorkloadRuntimeSpec(
                id="box",
                resource_name_template="box-{project_slug}",
                exec_prefix=("engine", "exec", "-w", "{runtime_dir}", "{resource}"),
                path_map=mapping,
                ready_command=("engine", "ready", "{resource}"),
            )
            _pinned(cfg, "claude", "box")
            mutations = []

            def respond(
                user,
                command,
                kwargs,
                *,
                cfg=cfg,
                escape=escape,
                label=label,
                mutations=mutations,
                path=path,
                repo=repo,
            ):
                assert user == "alice"
                if command[:2] == ["mkdir", "-p"]:
                    assert command[-1] == str(path.parent)
                    path.parent.mkdir(parents=True)
                    return _cp()
                if command[:3] == ["git", "-C", str(repo)]:
                    if command[3:5] == ["rev-parse", "--verify"]:
                        return _cp()
                    assert command[3:] == ["worktree", "add", str(path), "feature/x"]
                    mutations.append(command)
                    if label == "symlink":
                        path.symlink_to(escape, target_is_directory=True)
                    else:
                        path.mkdir()
                    if label == "rule":
                        cfg.launch = dataclasses.replace(
                            cfg.launch,
                            path_rules=(
                                LaunchPathRule(
                                    path_prefix=str(path), allowed_profiles=(), default_profile=""
                                ),
                            ),
                        )
                    return _cp()
                if command[:2] == ["engine", "ready"]:
                    return _cp()
                if command[:2] == ["engine", "exec"]:
                    assert command[-1] == "claude" and "uxon-agent-probe" in command
                    return _cp()
                return _server(user, command, kwargs)

            with TargetCommands(respond).installed_boundary():
                resolved = resolve_launch_profile(
                    cfg, "operator", "claude", str(path), "normal", target_may_not_exist=True
                )
                if label in {"symlink", "rule", "unmapped", "component-neighbor"}:
                    with pytest.raises(SystemExit) as error:
                        launch.plan_worktree_launch(
                            cfg,
                            "operator",
                            resolved,
                            str(repo),
                            "feature/x",
                            requested_profile="claude",
                        )
                    assert (
                        "policy changed"
                        if label == "symlink"
                        else "not allowed"
                        if label == "rule"
                        else "path_map"
                    ) in error.value.uxon_msg
                else:
                    request = launch.plan_worktree_launch(
                        cfg,
                        "operator",
                        resolved,
                        str(repo),
                        "feature/x",
                        requested_profile="claude",
                    )
                    create = request.managed.create_cmd
                    index = create.index("engine")
                    expected = str(path) if label == "empty-map" else "/work/repo/feature-x"
                    assert create[index : index + 5] == (
                        "engine",
                        "exec",
                        "-w",
                        expected,
                        "box-feature-x",
                    )
                    assert request.managed.project == str(path)
            assert len(mutations) == int(label not in {"unmapped", "component-neighbor"})

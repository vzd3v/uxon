# SPDX-License-Identifier: MIT
"""Launch-profile resolution boundary.

This module is the single launch-policy resolver for CLI/TUI launch paths:
it canonicalizes the target, applies path rules, chooses exactly one launch
profile, resolves the effective OS user, validates agent/mode/git policy, and
probes tmux/agent availability before any launch side effect.
"""

from __future__ import annotations

from pathlib import Path

from uxon.domain.agents import permission_mode_for
from uxon.domain.config import Config
from uxon.domain.host_report import HostReport
from uxon.domain.launch_profiles import (
    ContainerContext,
    GitRemotePolicy,
    LaunchPathRule,
    ResolvedLaunchProfile,
    match_path_rule,
)
from uxon.errors import fail
from uxon.infra import identity

_BUILTIN_PROFILE_ORDER = ("claude", "codex", "cursor")


def canonical_existing_target(path: str) -> str:
    """Canonicalize an existing launch target for policy matching."""
    return str(Path(path).expanduser().resolve(strict=False))


def canonical_intended_target(path: str) -> str:
    """Safely canonicalize a target that may not exist yet.

    Resolve the nearest existing parent, reject symlink missing components
    including a broken symlink final component, validate the remaining path
    components, and append them without following a future path.
    """
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target

    raw_parts = target.parts
    if any(part in {"", ".", ".."} or "\0" in part for part in raw_parts):
        fail(f"invalid launch target path component in {path!r}")

    if target.is_symlink():
        fail(f"refusing to create launch target through symlink: {target}")
    if target.exists():
        return str(target.resolve(strict=False))

    missing: list[str] = []
    cursor = target
    while not cursor.exists():
        if cursor.is_symlink():
            fail(f"refusing to create launch target through symlink: {cursor}")
        missing.append(cursor.name)
        parent = cursor.parent
        if parent == cursor:
            fail(f"no existing parent for launch target: {target}")
        cursor = parent

    base = cursor.resolve(strict=True)
    for part in reversed(missing):
        if part in {"", ".", ".."} or "\0" in part:
            fail(f"invalid launch target path component {part!r} in {path!r}")
        base = base / part
    return str(base)


def _choices_text(choices: tuple[str, ...]) -> str:
    return ", ".join(choices) if choices else "(none)"


def _path_allowed_profiles(cfg: Config, rule: LaunchPathRule | None) -> tuple[str, ...]:
    return rule.allowed_profiles if rule is not None else cfg.launch.effective_enabled_profiles


def _select_configured_profile(
    cfg: Config,
    requested: str | None,
    canonical_target: str,
    rule: LaunchPathRule | None,
) -> str | None:
    enabled = cfg.launch.effective_enabled_profiles
    path_allowed = _path_allowed_profiles(cfg, rule)
    if requested:
        if requested not in cfg.launch.profiles:
            fail(f"unknown --profile {requested!r}; valid profiles: {_choices_text(path_allowed)}")
        if requested not in enabled:
            fail(f"profile {requested!r} is disabled; valid profiles: {_choices_text(enabled)}")
        if requested not in path_allowed:
            fail(
                f"profile {requested!r} is not allowed for {canonical_target}; "
                f"valid profiles: {_choices_text(path_allowed)}"
            )
        return requested

    candidate = ""
    if rule is not None and rule.default_profile:
        candidate = rule.default_profile
    elif cfg.launch.default_profile:
        candidate = cfg.launch.default_profile
    elif not cfg.launch.auto_mode and path_allowed:
        candidate = path_allowed[0]

    if not candidate:
        return None
    if candidate not in enabled:
        fail(f"profile {candidate!r} is disabled; valid profiles: {_choices_text(enabled)}")
    if candidate not in path_allowed:
        fail(
            f"profile {candidate!r} is not allowed for {canonical_target}; "
            f"valid profiles: {_choices_text(path_allowed)}"
        )
    return candidate


def _effective_git_policy(
    profile_policy: GitRemotePolicy, rule: LaunchPathRule | None
) -> GitRemotePolicy:
    allowed = profile_policy.allowed_profiles
    if rule is not None and rule.allowed_git_remote_profiles is not None:
        narrowed = set(rule.allowed_git_remote_profiles)
        allowed = tuple(name for name in allowed if name in narrowed)
    default = (
        rule.default_git_remote_profile
        if rule is not None and rule.default_git_remote_profile
        else profile_policy.default_profile
    )
    if default and default not in allowed:
        default = ""
    return GitRemotePolicy(allowed_profiles=allowed, default_profile=default)


def _validate_git_selector(
    selector: str | None,
    policy: GitRemotePolicy,
    *,
    canonical_target: str,
) -> None:
    if selector is None:
        return
    if not policy.allowed_profiles:
        fail(f"git remote creation is not allowed for {canonical_target}")
    if selector == "default":
        if not policy.default_profile:
            fail(
                "no default git-remote profile is allowed for this launch profile; "
                f"valid profiles: {_choices_text(policy.allowed_profiles)}"
            )
        return
    if selector not in policy.allowed_profiles:
        fail(
            f"git remote profile {selector!r} is not allowed for this launch profile; "
            f"valid profiles: {_choices_text(policy.allowed_profiles)}"
        )


def _profile_launch_user(cfg: Config, caller_user: str, profile_id: str) -> str:
    profile = cfg.launch.profiles[profile_id]
    return profile.launch_user or identity.resolve_launch_user(cfg, caller_user)


def preflight_launch_user(cfg: Config, caller_user: str, requested_profile: str | None) -> str:
    """Resolve a seed launch user without tmux/agent probes.

    Worktree paths depend on the git repo root, and git repo discovery needs
    an OS user before the final worktree path exists. This helper validates an
    explicitly requested profile, or uses the configured default profile when
    available; auto-mode falls back to the caller-derived launch user because
    built-in auto candidates are OS-user-only.
    """
    enabled = cfg.launch.effective_enabled_profiles
    if requested_profile:
        if requested_profile not in cfg.launch.profiles:
            fail(
                f"unknown --profile {requested_profile!r}; valid profiles: {_choices_text(enabled)}"
            )
        if requested_profile not in enabled:
            fail(
                f"profile {requested_profile!r} is disabled; valid profiles: {_choices_text(enabled)}"
            )
        return _profile_launch_user(cfg, caller_user, requested_profile)
    if cfg.launch.default_profile and cfg.launch.default_profile in enabled:
        return _profile_launch_user(cfg, caller_user, cfg.launch.default_profile)
    return identity.resolve_launch_user(cfg, caller_user)


def _probe_host_for(
    launch_user: str,
    cfg: Config,
    agent_ids: tuple[str, ...],
    *,
    report: HostReport | None,
) -> HostReport:
    if (
        report is not None
        and report.launch_user == launch_user
        and set(agent_ids).issubset(report.agents)
    ):
        return report
    from uxon.infra import probes

    catalog = {aid: cfg.agents[aid] for aid in agent_ids}
    return probes.probe_host(launch_user, catalog)


def _fail_if_tmux_missing(report: HostReport) -> None:
    if report.tmux.path is None:
        fail(f"tmux is not installed.\n{report.tmux.install_hint}", 1)


def _auto_discover_profile(
    cfg: Config,
    caller_user: str,
    canonical_target: str,
    rule: LaunchPathRule | None,
    *,
    report: HostReport | None,
) -> tuple[str, str, HostReport]:
    path_allowed = _path_allowed_profiles(cfg, rule)
    candidates = tuple(
        pid
        for pid in _BUILTIN_PROFILE_ORDER
        if pid in path_allowed
        and pid in cfg.launch.profiles
        and not cfg.launch.profiles[pid].launch_user
        and not cfg.launch.profiles[pid].container_profile
    )
    if not candidates:
        fail(f"no auto-mode launch profiles are allowed for {canonical_target}")
    launch_user = identity.resolve_launch_user(cfg, caller_user)
    agent_ids = tuple(dict.fromkeys(cfg.launch.profiles[pid].agent for pid in candidates))
    probe_report = _probe_host_for(launch_user, cfg, agent_ids, report=report)
    _fail_if_tmux_missing(probe_report)
    missing: list[str] = []
    for pid in candidates:
        agent_id = cfg.launch.profiles[pid].agent
        status = probe_report.agents.get(agent_id)
        if status is not None and status.path is not None:
            return pid, launch_user, probe_report
        hint = status.install_hint if status is not None else ""
        missing.append(f"{pid} ({agent_id}: not installed{'; ' + hint if hint else ''})")
    fail(
        f"no auto-mode launch profile has an installed agent for {launch_user!r}; "
        f"candidates: {', '.join(missing)}",
        1,
    )
    raise AssertionError("unreachable")


def resolve_launch_profile(
    cfg: Config,
    caller_user: str,
    requested_profile: str | None,
    target_path: str,
    mode_id: str | None,
    *,
    git_remote_selector: str | None = None,
    target_may_not_exist: bool = False,
    report: HostReport | None = None,
) -> ResolvedLaunchProfile:
    canonical_target = (
        canonical_intended_target(target_path)
        if target_may_not_exist
        else canonical_existing_target(target_path)
    )
    rule = match_path_rule(cfg.launch.path_rules, canonical_target)
    selected = _select_configured_profile(cfg, requested_profile, canonical_target, rule)
    selected_report: HostReport | None = None
    if selected is None:
        selected, launch_user, selected_report = _auto_discover_profile(
            cfg, caller_user, canonical_target, rule, report=report
        )
    else:
        launch_user = _profile_launch_user(cfg, caller_user, selected)

    profile = cfg.launch.profiles[selected]
    if profile.agent not in cfg.agents:
        fail(f"launch profile {selected!r} references unknown agent {profile.agent!r}")
    agent = cfg.agents[profile.agent]
    requested_mode = None if mode_id in (None, "", "default") else mode_id
    resolved_mode = requested_mode or agent.permission_modes[0].id
    if permission_mode_for(agent, resolved_mode) is None:
        valid = ", ".join(mode.id for mode in agent.permission_modes)
        fail(f"unknown --mode {resolved_mode!r} for agent {agent.id!r}; valid modes: {valid}")

    policy = _effective_git_policy(profile.git_remote, rule)
    _validate_git_selector(git_remote_selector, policy, canonical_target=canonical_target)

    final_report = selected_report or _probe_host_for(
        launch_user, cfg, (profile.agent,), report=report
    )
    _fail_if_tmux_missing(final_report)
    status = final_report.agents.get(profile.agent)
    if not profile.container_profile and (status is None or status.path is None):
        hint = status.install_hint if status is not None else ""
        fail(
            f"agent {profile.agent!r} for launch profile {selected!r} is not installed for "
            f"{launch_user!r}." + (f"\n{hint}" if hint else ""),
            1,
        )

    container = (
        ContainerContext(profile_id=profile.container_profile, name="", dir_token="")
        if profile.container_profile
        else None
    )
    return ResolvedLaunchProfile(
        profile=profile,
        agent=agent,
        launch_user=launch_user,
        mode_id=resolved_mode,
        container=container,
        git_remote=policy,
    )


def revalidate_launch_profile(
    cfg: Config,
    caller_user: str,
    original: ResolvedLaunchProfile,
    target_path: str,
    *,
    requested_profile: str | None,
    mode_id: str | None,
    git_remote_selector: str | None = None,
    target_may_not_exist: bool = False,
) -> ResolvedLaunchProfile:
    resolved = resolve_launch_profile(
        cfg,
        caller_user,
        requested_profile,
        target_path,
        mode_id,
        git_remote_selector=git_remote_selector,
        target_may_not_exist=target_may_not_exist,
    )
    if (
        resolved.profile.id != original.profile.id
        or resolved.launch_user != original.launch_user
        or resolved.agent.id != original.agent.id
        or resolved.mode_id != original.mode_id
        or resolved.git_remote != original.git_remote
    ):
        fail("launch profile policy changed after target creation; refusing to continue")
    return resolved

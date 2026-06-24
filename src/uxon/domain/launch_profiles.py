# SPDX-License-Identifier: MIT
"""Pure launch-profile policy data.

Launch profiles are the operator-facing runnable choices. Agents remain the
binary/mode catalog; a launch profile points at one agent and may pin a launch
user, a container profile, and a git-remote credential policy.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from uxon.domain.agents import AgentSpec
from uxon.errors import fail

_TMUX_SAFE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class GitRemotePolicy:
    allowed_profiles: tuple[str, ...] = ()
    default_profile: str = ""


@dataclass(frozen=True)
class LaunchProfile:
    id: str
    agent: str
    display_name: str = ""
    launch_user: str = ""
    container_profile: str = ""
    git_remote: GitRemotePolicy = field(default_factory=GitRemotePolicy)


@dataclass(frozen=True)
class LaunchPathRule:
    path_prefix: str
    allowed_profiles: tuple[str, ...]
    default_profile: str
    allowed_git_remote_profiles: tuple[str, ...] | None = None
    default_git_remote_profile: str = ""


@dataclass(frozen=True)
class LaunchConfig:
    enabled_profiles: tuple[str, ...] = ()
    default_profile: str = ""
    profiles: dict[str, LaunchProfile] = field(default_factory=dict)
    path_rules: tuple[LaunchPathRule, ...] = ()

    @property
    def auto_mode(self) -> bool:
        return not self.enabled_profiles

    @property
    def effective_enabled_profiles(self) -> tuple[str, ...]:
        if self.enabled_profiles:
            return self.enabled_profiles
        return tuple(pid for pid in ("claude", "codex", "cursor") if pid in self.profiles)


@dataclass(frozen=True)
class ContainerIdentity:
    id: str
    cgroup: str = ""
    epoch: str = ""


@dataclass(frozen=True)
class ContainerContext:
    profile_id: str
    name: str
    dir_token: str
    profile_fingerprint: str = ""
    identity: ContainerIdentity | None = None


@dataclass(frozen=True)
class ResolvedLaunchProfile:
    profile: LaunchProfile
    agent: AgentSpec
    launch_user: str
    mode_id: str = ""
    container: ContainerContext | None = None
    git_remote: GitRemotePolicy = field(default_factory=GitRemotePolicy)

    @property
    def container_context(self) -> ContainerContext | None:
        return self.container


def validate_tmux_safe_id(value: str, *, what: str) -> str:
    if not isinstance(value, str) or not _TMUX_SAFE_ID_RE.match(value):
        fail(
            f"invalid {what} {value!r}: must match [a-z][a-z0-9_]* "
            "and contain no ':' or '.' (tmux-safe)"
        )
    if ":" in value or "." in value:
        fail(
            f"invalid {what} {value!r}: must match [a-z][a-z0-9_]* "
            "and contain no ':' or '.' (tmux-safe)"
        )
    return value


def builtin_launch_profiles(agent_catalog: dict[str, AgentSpec]) -> dict[str, LaunchProfile]:
    """Return shipped OS-user-only profiles derived from the agent catalog."""
    return {
        aid: LaunchProfile(id=aid, agent=aid)
        for aid in ("claude", "codex", "cursor")
        if aid in agent_catalog
    }


def match_path_rule(
    rules: tuple[LaunchPathRule, ...], canonical_target: str
) -> LaunchPathRule | None:
    """Return the longest component-aware path rule match.

    The caller supplies already-canonicalized paths. This helper is pure so the
    later launch-resolution phase can share the matching rule with tests.
    """
    target = os.path.normpath(canonical_target)
    matches = [
        rule
        for rule in rules
        if target == rule.path_prefix or target.startswith(rule.path_prefix.rstrip("/") + "/")
    ]
    if not matches:
        return None
    return max(matches, key=lambda rule: len(rule.path_prefix))

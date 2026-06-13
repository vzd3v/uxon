# SPDX-License-Identifier: MIT
"""Pure agent-catalog data: permission modes, agent specs, shipped defaults.

These are frozen dataclasses with no I/O — they live in ``domain`` (the
lowest layer above :mod:`uxon.errors`) so :class:`uxon.domain.config.Config`
can hold a ``dict[str, AgentSpec]`` without pulling an ``infra`` type into
``domain``, and so serving the catalog never touches the impure probe in
:mod:`uxon.infra.agents`. Mirrors the ``RECOMMENDED_TMUX_OPTIONS`` pattern
(pure data in ``domain``, materialised onto ``Config``).

``DEFAULT_AGENT_CATALOG`` is the shipped preset (claude / codex / cursor).
The config loader merges operator ``[agents.<id>]`` tables over it and
stores the result on ``Config.agents``; every reader (CLI, doctor, probe,
TUI) consumes that merged catalog as data.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PermissionMode:
    id: str  # free-form mode id, e.g. "normal" / "auto" / "yolo" / "plan"
    label: str = ""  # user-facing in the TUI; defaults to ``id`` when omitted
    flags: tuple[str, ...] = ()
    dangerous: bool = False  # semantic signal for audit / TUI emphasis

    def __post_init__(self) -> None:
        if not self.label:
            # ``frozen=True`` blocks plain assignment; go through __setattr__.
            object.__setattr__(self, "label", self.id)


@dataclass(frozen=True)
class AgentSpec:
    id: str  # agent id; the session suffix ``@<id>`` is derived where needed
    binary: str  # executable name on PATH
    permission_modes: tuple[PermissionMode, ...]
    install_hint: str = ""  # shown by doctor / agent-select / TUI
    version_args: tuple[str, ...] = ("--version",)  # doctor probe; () → no version line
    default_args: tuple[str, ...] = ()  # flags prepended to every invocation of this agent


def permission_mode_for(agent: AgentSpec, mode_id: str) -> PermissionMode | None:
    for mode in agent.permission_modes:
        if mode.id == mode_id:
            return mode
    return None


DEFAULT_AGENT_CATALOG: dict[str, AgentSpec] = {
    "claude": AgentSpec(
        id="claude",
        binary="claude",
        permission_modes=(
            PermissionMode("normal"),
            PermissionMode("auto", "auto", ("--permission-mode", "auto")),
            PermissionMode(
                "yolo",
                "yolo (--dangerously-skip-permissions)",
                ("--dangerously-skip-permissions",),
                dangerous=True,
            ),
        ),
        install_hint="see https://docs.claude.com/claude-code",
    ),
    "codex": AgentSpec(
        id="codex",
        binary="codex",
        permission_modes=(
            PermissionMode("normal"),
            PermissionMode("auto", "auto (--full-auto)", ("--full-auto",)),
            PermissionMode(
                "yolo",
                "yolo (--dangerously-bypass-approvals-and-sandbox)",
                ("--dangerously-bypass-approvals-and-sandbox",),
                dangerous=True,
            ),
        ),
        install_hint="install: npm i -g @openai/codex",
    ),
    "cursor": AgentSpec(
        id="cursor",
        binary="cursor-agent",
        permission_modes=(
            PermissionMode("normal"),
            PermissionMode("yolo", "yolo (--yolo)", ("--yolo",), dangerous=True),
        ),
        install_hint="install: curl https://cursor.com/install -fsSL | bash",
    ),
}


def default_agent_catalog() -> dict[str, AgentSpec]:
    """Return a fresh shallow copy of the shipped catalog.

    The :class:`AgentSpec` / :class:`PermissionMode` values are frozen and
    safe to share; copying the *dict* keeps a per-load mutation (the loader
    builds a merged catalog by id) from leaking back into the module-level
    constant.
    """
    return dict(DEFAULT_AGENT_CATALOG)

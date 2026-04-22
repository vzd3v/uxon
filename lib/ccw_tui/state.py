"""Pure TUI state decisions.

This module deliberately imports no Textual objects. Screen/app modules may
interpret these decisions, while fast unit tests can cover the branchy logic
without running a Textual event loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .context import (
    ACTION_COUNT,
    TuiContext,
    TuiSession,
    _digit_hinted_indices,
    _segments,
    _total_items,
)


def should_show_agents_unavailable(
    *,
    enabled_agents: tuple[str, ...],
    availability: Mapping[str, Any],
    already_shown: bool,
) -> bool:
    """Return True when the app should show the all-agents-unavailable modal."""
    if already_shown:
        return False
    if not enabled_agents:
        return False

    resolved = all(
        aid in availability and getattr(availability[aid], "status", "pending") != "pending"
        for aid in enabled_agents
    )
    if not resolved:
        return False

    return all(
        getattr(availability[aid], "status", None) in ("missing", "timeout")
        for aid in enabled_agents
    )


@dataclass(frozen=True)
class LaunchOptionsState:
    visible_agents: tuple[str, ...]
    single_agent: bool
    active_panel: str
    current_agent: str


def visible_agent_ids(
    *,
    enabled_agents: tuple[str, ...],
    availability: Mapping[str, Any],
) -> tuple[str, ...]:
    return tuple(
        aid for aid in enabled_agents
        if availability.get(aid) is None
        or getattr(availability.get(aid), "status", "pending") in ("pending", "ok")
    )


def launch_options_state(
    *,
    enabled_agents: tuple[str, ...],
    default_agent: str,
    availability: Mapping[str, Any],
) -> LaunchOptionsState:
    visible = visible_agent_ids(
        enabled_agents=enabled_agents,
        availability=availability,
    )
    single = len(visible) <= 1
    current = default_agent if default_agent in visible else (visible[0] if visible else default_agent)
    return LaunchOptionsState(
        visible_agents=visible,
        single_agent=single,
        active_panel="mode" if single else "agent",
        current_agent=current,
    )


def pick_visible_agent(
    visible_agents: tuple[str, ...],
    index: int,
    current_agent: str,
) -> str:
    if 0 <= index < len(visible_agents):
        return visible_agents[index]
    return current_agent


def agent_is_pending(agent_id: str, availability: Mapping[str, Any]) -> bool:
    avail_obj = availability.get(agent_id)
    return avail_obj is not None and getattr(avail_obj, "status", None) == "pending"


def agent_list_label(index: int, agent_id: str, availability_obj: Any | None) -> str:
    label = f"{index} {agent_id}"
    if availability_obj is not None and getattr(availability_obj, "status", None) == "pending":
        label += "  (checking…)"
    return label


def mode_item_ids(agent_id: str) -> tuple[str, ...]:
    import ccw_agents

    if agent_id not in ccw_agents.CATALOG:
        return ()
    return tuple(f"mode-{mode.id}" for mode in ccw_agents.CATALOG[agent_id].permission_modes)


def launch_mode_id(agent_id: str, mode_index: int) -> str | None:
    import ccw_agents

    if agent_id not in ccw_agents.CATALOG:
        return None
    modes = ccw_agents.CATALOG[agent_id].permission_modes
    if 0 <= mode_index < len(modes):
        return modes[mode_index].id
    return "normal"


def pick_index(rows: list[tuple[str, str]] | tuple[tuple[str, str], ...], index: int) -> str | None:
    if 0 <= index < len(rows):
        return rows[index][0]
    return None


@dataclass(frozen=True)
class MainIntent:
    kind: str
    index: int | None = None
    user: str = ""
    session_name: str = ""


def main_action_intent(kind: str) -> MainIntent | None:
    mapping = {
        "action-cwd": "launch-cwd",
        "action-new": "launch-new",
        "action-open": "launch-existing",
        "settings": "open-settings",
        "kill-all-global": "kill-all-global",
    }
    target = mapping.get(kind)
    return MainIntent(target) if target else None


def session_intent(session: TuiSession, current_user: str) -> MainIntent:
    return MainIntent(
        "attach",
        user=session.user or current_user,
        session_name=session.name,
    )


def activate_main_index(ctx: TuiContext, idx: int) -> MainIntent | None:
    own_start, other_start, settings_idx, kill_idx, has_super = _segments(ctx)
    if idx < 0 or idx >= _total_items(ctx):
        return None
    if idx < own_start:
        if idx == 0:
            return MainIntent("launch-cwd", index=idx)
        if idx == 1:
            return MainIntent("launch-new", index=idx)
        if idx == 2:
            return MainIntent("launch-existing", index=idx)
        return None
    if idx < other_start:
        session = ctx.sessions[idx - own_start]
        return MainIntent("attach", index=idx, user=ctx.current_user, session_name=session.name)
    if has_super and idx < settings_idx:
        intent = session_intent(ctx.other_sessions[idx - other_start], ctx.current_user)
        return MainIntent(intent.kind, index=idx, user=intent.user, session_name=intent.session_name)
    if has_super and idx == settings_idx:
        return MainIntent("open-settings", index=idx)
    if has_super and idx == kill_idx:
        return MainIntent("kill-all-global", index=idx)
    return None


def digit_jump_intent(ctx: TuiContext, n: int) -> MainIntent | None:
    idx = n - 1
    if idx < 0 or idx >= _total_items(ctx):
        return None
    if idx in _digit_hinted_indices(ctx):
        return activate_main_index(ctx, idx)
    own_start, other_start, settings_idx, kill_idx, has_super = _segments(ctx)
    if has_super and idx in (settings_idx, kill_idx):
        return MainIntent("focus-only", index=idx)
    return None


def confirm_phrase_matches(value: str, phrase: str) -> bool:
    return value.strip() == phrase


def project_name_valid(value: str) -> bool:
    name = value.strip()
    if not name:
        return False
    if "/" in name:
        return False
    if name in (".", ".."):
        return False
    return True


def project_name_error(value: str) -> str:
    name = value.strip()
    if not name:
        return "Name cannot be empty"
    if "/" in name:
        return "Name cannot contain '/'"
    return "Invalid name"

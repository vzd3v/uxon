"""Pure TUI state decisions.

This module deliberately imports no Textual objects. Screen/app modules may
interpret these decisions, while fast unit tests can cover the branchy logic
without running a Textual event loop.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .context import (
    CallbackError,
    LinkHealthStatus,
    ServerStatus,
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


def should_start_agent_probe(*, probe_agents: bool, enabled_agents: tuple[str, ...]) -> bool:
    return probe_agents and bool(enabled_agents)


@dataclass(frozen=True)
class CallbackFailure:
    message: str
    severity: str


def callback_failure_to_toast(prefix: str, exc: CallbackError) -> CallbackFailure:
    return CallbackFailure(f"{prefix}: {exc}", "error")


@dataclass(frozen=True)
class LaunchOptionsState:
    visible_agents: tuple[str, ...]
    single_agent: bool
    active_panel: str
    current_agent: str


@dataclass(frozen=True)
class LaunchOptionsUpdate:
    visible_agents: tuple[str, ...]
    single_agent: bool
    active_panel: str
    current_agent: str
    dismiss: bool


@dataclass(frozen=True)
class LaunchCommitDecision:
    action: str
    mode_id: str | None = None


def visible_agent_ids(
    *,
    enabled_agents: tuple[str, ...],
    availability: Mapping[str, Any],
) -> tuple[str, ...]:
    return tuple(
        aid
        for aid in enabled_agents
        if availability.get(aid) is None
        or getattr(availability.get(aid), "status", "pending") in ("pending", "ok")
    )


def update_launch_options_after_availability(
    *,
    enabled_agents: tuple[str, ...],
    default_agent: str,
    availability: Mapping[str, Any],
    current_agent: str,
    active_panel: str,
) -> LaunchOptionsUpdate:
    visible = visible_agent_ids(
        enabled_agents=enabled_agents,
        availability=availability,
    )
    if not visible:
        return LaunchOptionsUpdate(
            visible_agents=(),
            single_agent=True,
            active_panel="mode",
            current_agent=current_agent or default_agent,
            dismiss=True,
        )
    single = len(visible) <= 1
    if current_agent in visible:
        next_agent = current_agent
    elif default_agent in visible:
        next_agent = default_agent
    else:
        next_agent = visible[0]
    return LaunchOptionsUpdate(
        visible_agents=visible,
        single_agent=single,
        active_panel="mode" if single else active_panel,
        current_agent=next_agent,
        dismiss=False,
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
    current = (
        default_agent if default_agent in visible else (visible[0] if visible else default_agent)
    )
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
    import uxon_agents

    if agent_id not in uxon_agents.CATALOG:
        return ()
    return tuple(f"mode-{mode.id}" for mode in uxon_agents.CATALOG[agent_id].permission_modes)


def launch_mode_id(agent_id: str, mode_index: int) -> str | None:
    import uxon_agents

    if agent_id not in uxon_agents.CATALOG:
        return None
    modes = uxon_agents.CATALOG[agent_id].permission_modes
    if 0 <= mode_index < len(modes):
        return modes[mode_index].id
    return "normal"


def launch_commit_decision(
    *,
    active_panel: str,
    current_agent: str,
    availability: Mapping[str, Any],
    mode_index: int,
) -> LaunchCommitDecision:
    if active_panel == "agent":
        if agent_is_pending(current_agent, availability):
            return LaunchCommitDecision("ignore")
        return LaunchCommitDecision("switch-to-mode")
    mode_id = launch_mode_id(current_agent, mode_index)
    if mode_id is None:
        return LaunchCommitDecision("dismiss")
    return LaunchCommitDecision("commit", mode_id)


def pick_index(rows: list[tuple[str, str]] | tuple[tuple[str, str], ...], index: int) -> str | None:
    if 0 <= index < len(rows):
        return rows[index][0]
    return None


def selected_setting_index(*, row: int, has_git_view: bool, entry_count: int) -> int | None:
    idx = row - 1 if has_git_view else row
    if has_git_view and row == 0:
        return None
    if 0 <= idx < entry_count:
        return idx
    return None


def resettable_setting_key(entry: Any | None) -> str | None:
    if entry is None or not getattr(entry, "editable", False):
        return None
    return entry.spec.key


def server_status_line(status: ServerStatus) -> str:
    parts: list[str] = []
    if status.cpu or status.load:
        cpu = status.cpu or "-"
        load = status.load or "-"
        parts.append(f"cpu {cpu} load {load}")
    if status.ram:
        parts.append(f"ram {status.ram}")
    if status.disk:
        parts.append(f"disk {status.disk}")
    if status.uptime:
        parts.append(f"up {status.uptime}")
    if not parts:
        return "server: unavailable"
    return "server: " + " | ".join(parts)


@dataclass(frozen=True)
class MainStatusLine:
    text: str
    alert: bool


def refresh_tick_glyph(tick: int) -> str:
    glyphs = "-\\|/"
    return glyphs[tick % len(glyphs)]


def main_status_line(
    server_status: ServerStatus,
    link_health_status: LinkHealthStatus,
    refresh_tick: int,
    loading: bool = False,
) -> MainStatusLine:
    summary = (link_health_status.summary or "").strip()
    if loading:
        text = f"uxon {refresh_tick_glyph(refresh_tick)} | server: loading…"
    else:
        text = f"uxon {refresh_tick_glyph(refresh_tick)} | {server_status_line(server_status)}"
    if summary:
        text += f" | ssh-link: {summary}"
    return MainStatusLine(
        text=text,
        alert=link_health_status.state == "error",
    )


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
        return MainIntent(
            intent.kind, index=idx, user=intent.user, session_name=intent.session_name
        )
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

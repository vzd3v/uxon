"""Pure TUI state decisions.

This module deliberately imports no Textual objects. Screen/app modules may
interpret these decisions, while fast unit tests can cover the branchy logic
without running a Textual event loop.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .context import (
    CallbackError,
    LinkHealthStatus,
    ServerStatus,
    TuiContext,
    TuiSession,
    _segments,
    _total_items,
)

if TYPE_CHECKING:
    from .dashboard.ui_state import MainScreenUiState


def effective_agents(
    *,
    configured: tuple[str, ...],
    available_ids: tuple[str, ...],
) -> tuple[str, ...]:
    """Return the agent ids the user can actually launch.

    - ``configured`` non-empty → strict whitelist; return as-is.
    - ``configured`` empty → auto-mode; return every CATALOG id that
      ``available_ids`` reports as installed for ``launch_user``.

    Empty/absent ``[agents].enabled`` in repo config and ``[]`` are
    treated identically — both mean "auto-detect from what is
    installed". Explicit "disable everything" is not supported (YAGNI:
    nobody installs uxon to forbid launching).
    """
    if configured:
        return configured
    from uxon import agents as uxon_agents

    return tuple(aid for aid in uxon_agents.CATALOG if aid in available_ids)


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


def compute_all_missing(
    *,
    enabled_agents: tuple[str, ...],
    availability: Mapping[str, Any],
) -> bool:
    """Return True when every enabled agent has a resolved missing/timeout status.

    Distinct from :func:`should_show_agents_unavailable` because the
    transition-based push gate (``should_push_agents_unavailable``)
    needs the raw "is this state all-missing now" predicate,
    decoupled from the previously-shown latch.
    """
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


def should_push_agents_unavailable(
    *,
    last_all_missing: bool | None,
    current_all_missing: bool,
    modal_already_on_stack: bool,
    pending_launch: bool,
) -> bool:
    """Decide whether to push ``AgentsUnavailableScreen`` on this tick.

    Replaces the per-app-instance ``_agents_popup_shown`` latch with a
    transition-based gate:

    - Push only on the False/None → True transition (no spam if the state
      keeps being "all missing").
    - Never push when the modal is already on the screen stack (defensive).
    - Never push during a launch handoff (``pending_launch`` is set) — the
      launch is taking the user to a TTY and racing a modal push is rude.
    - We deliberately do **not** auto-pop when state recovers; closing a
      modal under the user is hostile and races with concurrent
      ``pop_screen`` paths.
    """
    if not current_all_missing:
        return False
    if modal_already_on_stack:
        return False
    if pending_launch:
        return False
    # Transition gate: only push on (False|None) → True.
    return last_all_missing in (False, None)


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
    """Agent ids the LaunchOptions screen should expose.

    Strict mode (``enabled_agents`` non-empty): the configured list,
    minus any with a resolved ``missing``/``timeout`` status. Pending
    entries stay visible so the row renders as "(checking…)" rather
    than vanishing mid-probe.

    Auto-mode (``enabled_agents`` empty): every ``CATALOG`` id that
    has resolved to ``ok`` in ``availability``. No "missing" rows —
    the auto-mode probe never inserts un-installed entries, so a
    missing/timeout entry would have to be a stale strict-mode hangover.
    """
    if enabled_agents:
        return tuple(
            aid
            for aid in enabled_agents
            if availability.get(aid) is None
            or getattr(availability.get(aid), "status", "pending") in ("pending", "ok")
        )
    from uxon import agents as uxon_agents

    return tuple(
        aid
        for aid in uxon_agents.CATALOG
        if aid in availability and getattr(availability[aid], "status", None) == "ok"
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
    from uxon import agents as uxon_agents

    if agent_id not in uxon_agents.CATALOG:
        return ()
    return tuple(f"mode-{mode.id}" for mode in uxon_agents.CATALOG[agent_id].permission_modes)


def launch_mode_id(agent_id: str, mode_index: int) -> str | None:
    from uxon import agents as uxon_agents

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


def next_launch_panel(current: str, direction: int, order: tuple[str, ...]) -> str:
    """Cycle the active launch-options panel across only the VISIBLE columns.

    ``order`` is the visible-column sequence (a subset of
    ``("agent", "mode", "workspace")`` — AGENT is dropped under a single
    agent, WORKSPACE is absent for a non-git target). ``direction`` is
    +1 (right) or -1 (left); the cycle wraps. An unknown ``current``
    (e.g. the previously-active column is now hidden) snaps to the first
    visible column.
    """
    if not order:
        return current
    if current not in order:
        return order[0]
    idx = order.index(current)
    return order[(idx + direction) % len(order)]


def pick_index(rows: list[tuple[str, str]] | tuple[tuple[str, str], ...], index: int) -> str | None:
    if 0 <= index < len(rows):
        return rows[index][0]
    return None


def filter_existing_projects(
    projects: list[tuple[str, str]] | tuple[tuple[str, str], ...],
    needle: str,
) -> list[tuple[str, str]]:
    """Substring-filter a project list by name (case-insensitive).

    Original order is preserved — the screen sorts by mtime desc when
    it builds the list, and the filter must not reshuffle that. An
    empty (or whitespace-only) needle returns every project.
    """
    n = needle.strip().lower()
    if not n:
        return list(projects)
    return [p for p in projects if n in p[0].lower()]


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
    host: str = ""


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


@dataclass(frozen=True)
class HostHealthBadge:
    """Per-host health summary derived from a ``RemoteSnapshot``.

    Status values:
      - ``loading`` — no snapshot yet (the worker has not produced a result).
      - ``ok`` — last fetch succeeded (``error is None`` and not from cache).
      - ``stale`` — table is rendering cached data (``from_cache=True``);
        the live fetch may have failed (badge then carries ``+ err``).
      - ``down`` — last fetch failed AND no cache exists (no data shown).
    """

    status: str
    text: str


def _format_age_seconds(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _short_error(msg: str | None, *, limit: int = 48) -> str:
    if not msg:
        return "error"
    first = msg.splitlines()[0].strip()
    if len(first) > limit:
        return first[: limit - 1] + "…"
    return first or "error"


# ── Selectors ─────────────────────────────────────────────────────────
#
# Pure, identity-stable selectors over :class:`TuiContext` and the
# per-host ``RemoteSnapshot`` mapping: a selector returns the same
# object across calls when inputs are unchanged by ``is`` comparison,
# so consumers can cache on the result identity.


def select_layout_signature(
    ctx: TuiContext, ui: MainScreenUiState
) -> tuple[bool, bool, bool, bool]:
    """Return the four-bool layout signature for ``MainScreen`` patch-vs-recompose.

    Tuple shape: ``(has_own_sessions, has_super, cross_user_latched,
    kill_visible)``. Pure; memoisation is unnecessary because the
    result is a tuple of bools (cheap to recompute, equality-compared
    by callers).

    Position 2 (``cross_user_latched``) is the monotonic latch that
    drives the USER column: ``True`` once two distinct usernames have
    been observed anywhere in the dashboard model (local own, local
    other, remote peers). Reads from
    :attr:`MainScreenUiState.seen_users` — see
    :func:`uxon.tui.dashboard.seen_users.cross_user_latched` for the
    contract. The latch never resets within a process lifetime, so
    the False→True flip is the only transition that triggers a
    recompose of ``MainScreen`` for this bit.
    """
    from .dashboard.seen_users import cross_user_latched

    has_super = bool(ctx.sudo_caps.reachable_users)
    return (
        bool(ctx.sessions),
        has_super,
        cross_user_latched(ui),
        has_super and (len(ctx.sessions) + len(ctx.other_sessions) > 0),
    )


def host_health_badge(snapshot: Any, *, now: float | None = None) -> HostHealthBadge:
    """Compute a per-host health badge from a (possibly ``None``) snapshot.

    Pure helper: no Textual, no time-source side effects (``now`` is the
    seam). Returned ``text`` is short enough to drop into a section
    header or a HOST-column cell without wrapping.
    """
    if snapshot is None:
        return HostHealthBadge(status="loading", text="loading")
    error = getattr(snapshot, "error", None)
    from_cache = bool(getattr(snapshot, "from_cache", False))
    if error is None and not from_cache:
        return HostHealthBadge(status="ok", text="ok")
    if from_cache:
        cached_at = getattr(snapshot, "cached_at_epoch", None)
        if cached_at is None:
            # from_cache=True but no cached_at — should not happen in practice,
            # treat as "stale" without an age stamp rather than guess.
            text = "cache" if error is None else "cache + err"
            return HostHealthBadge(status="stale", text=text)
        if now is None:
            now = time.time()
        age = _format_age_seconds(int(now - float(cached_at)))
        text = f"cache {age}" if error is None else f"cache {age} + err"
        return HostHealthBadge(status="stale", text=text)
    # error path with no cache — no data shown.
    return HostHealthBadge(status="down", text=f"err: {_short_error(error)}")


def project_name_error(value: str) -> str:
    name = value.strip()
    if not name:
        return "Name cannot be empty"
    if "/" in name:
        return "Name cannot contain '/'"
    return "Invalid name"

"""Pure TUI state decisions.

This module deliberately imports no Textual objects. Screen/app modules may
interpret these decisions, while fast unit tests can cover the branchy logic
without running a Textual event loop.
"""

from __future__ import annotations

import time
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


def compute_all_missing(
    *,
    enabled_agents: tuple[str, ...],
    availability: Mapping[str, Any],
) -> bool:
    """Return True when every enabled agent has a resolved missing/timeout status.

    Distinct from :func:`should_show_agents_unavailable` because the new
    transition-based push gate (``should_push_agents_unavailable``) needs the
    raw "is this state all-missing now" predicate, decoupled from the
    previously-shown latch.
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


def visible_detected_agents(
    *,
    detected: Mapping[str, Any],
    enabled_agents: tuple[str, ...],
    dismissed: list[str],
) -> list[str]:
    """Return the agent ids that should appear in the detected banner.

    An entry is shown when it is detected on the host (``detected``
    map populated by ``probe_host``), is **not** already in
    ``enabled_agents`` (defensive — the worker should have filtered
    these out), and the user has not dismissed it.
    """
    enabled_set = set(enabled_agents)
    out: list[str] = []
    for aid in detected:
        if aid in enabled_set:
            continue
        if aid in dismissed:
            continue
        out.append(aid)
    return out


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


@dataclass(frozen=True)
class HostHealthBadge:
    """Per-host health summary derived from a ``RemoteSnapshot``.

    Stage 6 reads ``RemoteSnapshot.error``/``from_cache``/``cached_at_epoch``
    directly. The richer ``SlotState[T]`` (latency ring, ``in_flight``,
    ``consecutive_failures``) and the p50-latency tooltip land at stage 8.

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


# ── Stage 9 selectors ─────────────────────────────────────────────────
#
# Pure, identity-stable selectors over the existing :class:`TuiContext`
# shape. Stage 8 deliberately deferred the ``TuiContext`` →
# ``TuiState``/``MainData`` split, so these operate on the live ctx +
# per-host ``RemoteSnapshot`` mapping rather than on a future state
# container. The identity-stable contract (selector returns the same
# object across calls when inputs are unchanged by ``is`` comparison)
# still applies and is testable today; the eventual reactive wiring
# will plug into these helpers without changing their signatures.

_REMOTE_ROWS_CACHE: dict[str, Any] = {"key": None, "value": ()}
# Per-host badge cache. Keyed by host name (``cfg.remote_hosts`` is
# stable for the App's lifetime), value is ``(id(snapshot), badge)``.
# Stage 8 commit 4 rewrite: was ``dict[int, HostHealthBadge]`` keyed
# on ``id(snapshot)``; that scheme paired with ``apply``'s allocator
# pressure to evict at exactly the wrong moment (50 simultaneous
# misses on a 50-host churn). Per-host keying naturally bounds the
# cache by ``len(remote_hosts)`` so there is no overflow path; the
# value-id check inside the entry catches the rare CPython recycling
# of an int id.
_HOST_HEALTH_BADGE_CACHE: dict[str, tuple[int, HostHealthBadge]] = {}


def select_remote_rows(
    state: Any,
    hosts: Any,
) -> tuple[tuple[str, dict], ...]:
    """Flatten per-host slot values into a row tuple. Identity-stable.

    Inputs:
        state: A :class:`uxon.tui.tui_state.TuiState` whose
            ``remote: dict[str, SlotState[RemoteSnapshot]]`` field
            holds the per-host slots. Typed as ``Any`` to keep this
            module importable without pulling :mod:`tui_state` into
            its import graph at module-load time.
        hosts: Iterable of ``RemoteHost`` (or anything with a
            ``.name`` attribute) defining display order. Configuration
            owns the order; the slot dict is unordered.

    Mirrors ``MainScreen._flatten_remote_rows`` but as a pure function.
    Skips hosts with no landed snapshot. In the multi-host case,
    attaches ``(own only)`` and ``[<health>]`` badges to the
    displayed host name; single-host puts the badge in the section
    header instead (see ``_remote_header``).

    Memoised: cache key is per-host ``(name, id(slot.value))``. After
    the identity-stable :func:`apply` (commit 4), a no-op tick
    preserves ``id(slot.value)`` even though a fresh ``SlotState``
    was allocated — so the cache hits and downstream Textual code
    can ``is``-compare on the returned tuple to skip a re-render.
    """
    multi_host = len(hosts) > 1
    key_parts: list[tuple[str, int]] = []
    for host in hosts:
        slot = state.remote.get(host.name)
        snap = slot.value if slot is not None else None
        key_parts.append((host.name, id(snap)))
    key = tuple(key_parts)
    if _REMOTE_ROWS_CACHE.get("key") == key:
        return _REMOTE_ROWS_CACHE["value"]
    rows: list[tuple[str, dict]] = []
    for host in hosts:
        slot = state.remote.get(host.name)
        snap = slot.value if slot is not None else None
        if snap is None:
            continue
        limited = bool(getattr(snap, "scope_limited", False))
        display_name = host.name
        if multi_host:
            if limited:
                display_name = f"{display_name} (own only)"
            badge = select_remote_health_badge(host.name, snap)
            display_name = f"{display_name} [{badge.text}]"
        for rec in snap.sessions:
            rows.append((display_name, rec))
    value: tuple[tuple[str, dict], ...] = tuple(rows)
    _REMOTE_ROWS_CACHE["key"] = key
    _REMOTE_ROWS_CACHE["value"] = value
    return value


def select_layout_signature(ctx: TuiContext) -> tuple[bool, bool, bool, bool]:
    """Return the four-bool layout signature for ``MainScreen`` patch-vs-recompose.

    Mirrors ``MainScreen._layout_signature``: ``(has_own_sessions,
    has_super, has_other_sessions, kill_visible)``. Pure; memoisation
    is unnecessary because the result is a tuple of bools (cheap to
    recompute, equality-compared by callers).
    """
    has_super = bool(ctx.sudo_caps.reachable_users)
    return (
        bool(ctx.sessions),
        has_super,
        bool(ctx.other_sessions),
        has_super and (len(ctx.sessions) + len(ctx.other_sessions) > 0),
    )


def select_remote_health_badge(host_name: str, snapshot: Any) -> HostHealthBadge:
    """Identity-stable wrapper over :func:`host_health_badge`.

    Per-host cache: each peer name owns one slot. Replacing the
    snapshot for a host invalidates that host's slot only — other
    hosts keep their cached badge. Cache size is naturally bounded
    by ``len(cfg.remote_hosts)``; no overflow path is needed.

    Stale-detection: the value is keyed on ``id(snapshot)`` *inside*
    the per-host slot. After the identity-stable :func:`apply`
    (commit 4), ``id(slot.value)`` is preserved across a no-op tick,
    so the cache hits without re-deriving. Genuine snapshot
    replacement bumps the id and the slot recomputes.

    The id check also handles the rare CPython id-recycling case: a
    freshly-allocated snapshot for host B can land on an id
    previously held by host A's old snapshot. Per-host keying makes
    this impossible because the lookup is done with the host's name
    first, but the id check is kept as belt-and-braces.

    The ``now`` axis from :func:`host_health_badge` is intentionally
    not exposed here: the badge's "stale + age" text would change on
    every wall-clock tick, defeating the cache. Callers that need
    age-stamped output pass through to :func:`host_health_badge`
    directly.
    """
    snap_id = id(snapshot)
    cached = _HOST_HEALTH_BADGE_CACHE.get(host_name)
    if cached is not None and cached[0] == snap_id:
        return cached[1]
    badge = host_health_badge(snapshot)
    _HOST_HEALTH_BADGE_CACHE[host_name] = (snap_id, badge)
    return badge


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

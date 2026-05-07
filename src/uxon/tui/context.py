"""Pure data structures + CallbackError for the uxon TUI.

This module imports no UI framework (no blessed, no textual). The
TUI's screens and the outer runner both depend on these types, but
non-UI callers (tests, ``uxon.cli`` context builders) can import them
without pulling in any terminal dependency.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .tui_state import TuiState


@dataclass(frozen=True)
class SudoCapability:
    """Per-target sudo snapshot consumed by the TUI.

    ``reachable_users`` is the subset of ``session_users`` the caller
    can sudo into via ``sudo -niu <U>`` (probed once at startup).
    ``can_root`` is the root-NOPASSWD flag used to gate the
    Settings-screen write fallback (``sudo tee`` of root-owned
    config). The set is frozen so consumers can hash / store it
    safely.

    This class lives in ``tui.context`` (rather than alongside the
    probe machinery in ``uxon.sudo_probe``) so the TUI module is
    importable without pulling in ``subprocess``. ``uxon.sudo_probe``
    re-exports the same name so call sites can import it from the
    natural place; both names resolve to this single class.
    """

    reachable_users: frozenset[str] = frozenset()
    can_root: bool = False


# ── Errors ───────────────────────────────────────────────────────────


class CallbackError(Exception):
    """Raised by a TUI callback when the underlying uxon operation failed.

    The message is user-facing: the main loop renders it on the status
    line (or in the post-launch banner) in red. ``uxon.cli`` wraps every
    callback with ``_wrap_tui_callback`` so that ``fail() → SystemExit``
    paths inside uxon surface here with their stderr message intact,
    instead of killing the process silently under the fullscreen TUI.
    """


# ── Data ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LaunchRequest:
    """Describes a tmux invocation the TUI wants the outer loop to fork-and-wait.

    The TUI itself never spawns subprocesses; activation handlers return
    one of these, the main loop exits the fullscreen context, runs
    the ``prelaunch`` commands and then ``cmd``, waits for exit, and
    re-enters the main screen with a refreshed context.
    """

    cmd: tuple[str, ...]
    prelaunch: tuple[tuple[str, ...], ...] = ()
    label: str = ""


def session_name_from_launch_label(label: str) -> str:
    """Extract the bare tmux session name from a LaunchRequest label.

    Labels are constructed as ``"<verb> <session>"`` (verbs ``launch``,
    ``attach``, ``switch-client``) with an optional ``" (nested)"``
    suffix on the switch-client form.  Audit ``session.*`` events take
    the bare session name in the ``session`` field; the labelled form
    breaks cross-event correlation with CLI emits.
    """
    if " " not in label:
        return label
    rest = label.split(" ", 1)[1]
    if rest.endswith(" (nested)"):
        rest = rest[: -len(" (nested)")]
    return rest


@dataclass
class TuiSession:
    """Flattened session data for TUI rendering (decoupled from uxon internals)."""

    name: str
    short: str
    attached: bool
    pid: str
    cpu: str
    ram: str
    created: str
    last_activity: str
    cmd: str
    path: str
    user: str
    # Multi-agent fields (default to backward-compatible values).
    stem: str = ""  # bare project stem, e.g. "myproject"
    agent: str = "claude"  # agent id, e.g. "claude", "codex", "cursor"
    legacy: bool = False  # True when parsed from old cc-<stem> naming


@dataclass(frozen=True)
class ServerStatus:
    """Compact host health snapshot rendered on the main TUI screen."""

    load: str = ""
    cpu: str = ""
    ram: str = ""
    disk: str = ""
    uptime: str = ""


@dataclass(frozen=True)
class LinkHealthStatus:
    """Async SSH-path health probe rendered on the main TUI screen."""

    state: str = "hidden"  # hidden | ok | error | info
    summary: str = ""


@dataclass
class TuiContext:
    """Everything the TUI needs from uxon to operate."""

    sessions: list[TuiSession]  # sessions owned by current_user
    total_cpu: str
    total_ram: str
    version: str
    cwd: str
    cwd_short: str
    new_project_root: str
    existing_projects: list[tuple[str, str]]  # (name, compact_mtime) under new_project_root
    server_status: ServerStatus = field(default_factory=ServerStatus)
    link_health_status: LinkHealthStatus = field(default_factory=LinkHealthStatus)
    # ``refresh_tick`` is declared here so pyright sees the
    # attribute, but the property descriptor installed *after the
    # class body* (see below) replaces this default at runtime —
    # reads/writes go through ``self._state.refresh_tick`` when a
    # state is linked, otherwise through a private legacy slot.
    # ``init=False`` keeps the kwarg out of ``__init__`` (no caller
    # passes ``refresh_tick=`` today).
    refresh_tick: int = field(default=0, init=False)
    tui_refresh_interval_seconds: float = 2.0
    tui_ssh_refresh_interval_seconds: float = 10.0
    # Multi-host transport knobs. Forwarded into per-host fetch
    # closures by ``cli._build_tui_context`` and snapshotted into
    # :class:`uxon.tui.config.TuiConfig` at App-construction time.
    # Defaults mirror :data:`uxon.cli.DEFAULT_CONFIG` so test fixtures
    # that build a bare ``TuiContext`` keep working unchanged.
    ssh_multiplex: str = "auto"
    fetch_concurrency: int = 16

    # True until the first real refresh lands. The TUI distinguishes this
    # from a loaded-but-empty state — skeleton ctx renders "Loading…" in
    # session areas, server status and existing-projects detail; a fully
    # loaded ctx with no sessions renders "No active sessions." instead.
    loading: bool = False

    # Whether ``cwd`` is a valid launch target for ``launch_user`` under
    # the current policy: write access, plus membership in
    # ``allowed_roots`` when that whitelist is non-empty. Field name is
    # historical — the predicate is broader than write-access alone.
    # Three-valued:
    #   None  — probe still in flight; row stays enabled, activation
    #           runs a synchronous fallback check before launching.
    #   True  — launchable; row enabled, no detail hint.
    #   False — not launchable; row dimmed, detail says so.
    cwd_writable: bool | None = None

    current_user: str = ""
    # Per-target sudo capability. ``reachable_users`` gates the "Other
    # users' sessions" block + ``kill-all-reachable`` action;
    # ``can_root`` gates the Settings-screen write fallback. Replaces
    # the legacy single-boolean ``has_sudo`` gate.
    sudo_caps: SudoCapability = field(default_factory=SudoCapability)
    # Users in ``session_users`` the per-target probe could not reach.
    # Surfaced in the TUI's "(N/M users reachable)" hint and on
    # ``uxon list --all-users`` stderr / JSON.
    scope_skipped_users: tuple[str, ...] = ()
    other_sessions: list[TuiSession] = field(default_factory=list)  # sessions of other users

    # Multi-agent fields (Task 7+)
    enabled_agents: tuple[str, ...] = ("claude",)
    default_agent: str = "claude"
    launch_user: str = ""
    # Maps agent_id → AgentAvailability (status: "pending"|"ok"|"missing"|"timeout")
    agent_availability: dict[str, Any] = field(default_factory=dict)
    # Maps agent_id → BinaryStatus for agents installed on the host but not
    # listed in ``enabled_agents``. Populated by the ``probe_host`` worker so
    # the main screen can suggest enabling them.
    detected_agents: dict[str, Any] = field(default_factory=dict)
    # Whether the repo-config file is writable by the current user (directly
    # via ``os.access`` or indirectly via passwordless sudo). Used by the
    # detected-agents banner to decide whether the ``[a]`` action is live.
    repo_config_writable: bool = False

    # Callbacks — TUI calls these, uxon provides them.
    # Launch/attach callbacks return a LaunchRequest; the outer run() loop
    # runs the command and re-enters the TUI main screen on exit.
    on_attach: Callable[[str, str], LaunchRequest] = lambda user, name: LaunchRequest(
        cmd=("true",), label="noop-attach"
    )
    on_kill: Callable[[str, str], None] = lambda user, name: None  # (user, session) -> kill
    on_kill_all: Callable[[], None] = lambda: None  # kill all own sessions
    on_kill_all_global: Callable[[], None] = lambda: None  # kill all sessions across users
    # Multi-host per-session kill (3.4.0). Args: (host_name, user, session).
    # Implementation runs ``uxon kill --force --host <h> --user <u> <s>``
    # over SSH on the local CLI side; the peer's own ``uxon kill`` does the
    # per-target sudo gating. Bulk kill remains strictly local — no
    # ``on_remote_kill_all`` exists by design.
    on_remote_kill: Callable[[str, str, str], None] = (
        lambda host, user, name: None  # (host, user, session) -> kill on peer
    )
    # Multi-host per-session attach (parallel to on_remote_kill).
    # Args: (host_name, user, session). Implementation builds an
    # interactive ssh LaunchRequest via build_peer_ssh_argv; the TUI
    # hands it to request_launch (fork-and-wait, returns to TUI on
    # tmux detach).
    on_remote_attach: Callable[[str, str, str], LaunchRequest] = lambda host, user, name: (
        LaunchRequest(cmd=("true",), label="noop-remote-attach")
    )
    on_refresh: Callable[[], TuiContext] = lambda: None  # type: ignore[return-value]
    on_probe_link_health: Callable[[], Any] = lambda: None
    # Returns True if launch_user has write access to ``cwd``. Wired by
    # ``uxon.cli`` — uses ``os.access`` when launch_user == caller, otherwise
    # ``sudo -iu launch_user test -w <cwd>``. App runs it in a worker
    # thread on mount when ``cwd_writable`` is None; activation also
    # calls it synchronously as a fallback if the probe hasn't landed.
    on_probe_cwd_writable: Callable[[], bool] = lambda: True
    on_launch_cwd: Callable[[str, str], LaunchRequest] = lambda agent_id, mode_id: LaunchRequest(
        cmd=("true",), label="noop-launch-cwd"
    )
    on_launch_new: Callable[[str, str, str, str], LaunchRequest] = (
        lambda name, agent_id, mode_id, git_profile: LaunchRequest(
            cmd=("true",), label="noop-launch-new"
        )
    )
    on_launch_existing: Callable[[str, str, str], LaunchRequest] = lambda name, agent_id, mode_id: (
        LaunchRequest(cmd=("true",), label="noop-launch-existing")
    )

    # Git remote on new project — display only. The TUI never edits these.
    git_create_enabled: bool = False
    default_git_remote_profile: str = ""
    # Each entry: (profile_name, description string like "github.com/<owner> via <creds_user> [gh]")
    git_remote_profile_options: list[tuple[str, str]] = field(default_factory=list)

    # Settings (superuser-only). The TUI delegates all file I/O through these.
    get_settings_entries: Callable[[], list] = lambda: []
    on_setting_save: Callable[[str, Any], None] = lambda key, value: None
    on_setting_remove: Callable[[str], None] = lambda key: None
    on_setting_save_mapping: Callable[[str, dict], None] = lambda key, mapping: None
    get_git_remote_profile_rows: Callable[[], list] = lambda: []

    # Detected-agents banner callbacks. ``on_enable_detected_agent`` mutates
    # ``[agents].enabled`` in repo config; ``on_dismiss_detected_agent``
    # appends to the per-user dismissed-list state file. ``get_dismissed``
    # is read each tick so external state edits show up after a refresh.
    on_enable_detected_agent: Callable[[str], None] = lambda agent_id: None
    on_dismiss_detected_agent: Callable[[str], None] = lambda agent_id: None
    get_dismissed_detected_agents: Callable[[], list[str]] = list

    # Multi-host (Task #11): peer machines polled over SSH for their
    # session lists. ``remote_hosts`` is the static config (parsed
    # once at load_config time); ``remote_snapshots`` is the live
    # state, keyed by ``RemoteHost.name`` and populated by per-host
    # refresh sources. An empty ``remote_hosts`` disables the whole
    # block — no Remote-sessions table is rendered, no SSH workers
    # are kicked.
    remote_hosts: list = field(default_factory=list)  # list[RemoteHost]

    # ── Dashboard table preferences (commit 10) ──────────────────────
    # Mirror the matching fields on :class:`uxon.cli.Config`.
    # ``tui_table_columns is None`` means "use registry defaults"; an
    # explicit tuple is the user's column order from
    # ``[tui.table] columns = [...]``. ``tui_table_default_sort_by`` is
    # the active sort column id at first paint (validated by
    # ``cli.load_config`` to the registry's known ids; falls back to
    # ``"cpu"`` when the config carries an unknown id).
    tui_table_columns: tuple[str, ...] | None = None
    tui_table_default_sort_by: str = "cpu"
    # ``remote_snapshots`` is exposed via the property defined after
    # the class body. Reads return either a flattened view of
    # ``self._state.remote`` (when a state is linked) or the legacy
    # dict slot below. Writes go to the legacy slot only — the
    # dispatcher mutates ``state.remote`` directly via ``apply``.
    # Test fixtures keep passing ``remote_snapshots={...}`` as a
    # kwarg, which lands in the legacy slot for unit tests that
    # don't build an App.
    remote_snapshots: dict = field(default_factory=dict)  # dict[str, RemoteSnapshot]

    # Pluggable refresh sources. Each entry is a ``SourceSpec`` (see
    # ``uxon.tui.refresh``) describing one asynchronous data stream:
    # a fetcher, a cadence-attribute name, and a per-source worker
    # identity. The app fans out ``kick_refresh`` across this list, so
    # adding a new stream (e.g. a remote-host session collector) is a
    # declarative append rather than a wiring change in ``app.py``.
    #
    # ``None`` (the default) means "no registered sources" — used by
    # tests and the skeleton context. The CLI's ``_build_tui_context``
    # populates this with the ``main_ctx_rebuild`` source that wraps
    # ``on_refresh()``; future hosts add more entries here.
    refresh_sources: list = field(default_factory=list)

    # Linked :class:`uxon.tui.tui_state.TuiState` — the App sets this
    # at ``__init__`` time so the canonical ``refresh_tick`` (and
    # later: every async slot) is shared between the live ctx and the
    # state container. Defaults to ``None`` for tests and the
    # skeleton ctx that don't run inside an App; the property
    # accessors below fall back to a private legacy slot. Excluded
    # from ``__repr__`` to keep test failures readable.
    _state: TuiState | None = field(default=None, repr=False, compare=False)


# ── refresh_tick property ───────────────────────────────────────────
#
# Defined after the dataclass body so the property descriptor is not
# shadowed by the @dataclass-generated ``__init__`` field assignment.
# When ``_state`` is linked (App is running), reads/writes go through
# ``state.refresh_tick``; otherwise a private legacy slot in
# ``__dict__`` keeps the dataclass-style attribute semantics intact.
#
# Stage 8 commit 3 introduces this proxy; commit 6b makes
# ``state.refresh_tick`` canonical and the legacy slot becomes dead
# weight (removed in commit 10 with the rest of the shim).


def _tui_refresh_tick_get(self: TuiContext) -> int:
    state = getattr(self, "_state", None)
    if state is not None:
        return state.refresh_tick
    return self.__dict__.get("_legacy_refresh_tick", 0)


def _tui_refresh_tick_set(self: TuiContext, value: int) -> None:
    value = int(value)
    state = getattr(self, "_state", None)
    if state is not None:
        state.refresh_tick = value
    self.__dict__["_legacy_refresh_tick"] = value


TuiContext.refresh_tick = property(  # type: ignore[assignment]
    _tui_refresh_tick_get,
    _tui_refresh_tick_set,
)


# ── remote_snapshots property (read-through view onto state.remote) ─
#
# Stage 8 commit 4: ``state.remote`` is the canonical store. Reads
# through the shim flatten the slot dict to the legacy
# ``dict[str, RemoteSnapshot]`` shape; writes (rare; mostly test
# fixtures setting via the constructor kwarg) land on a private
# legacy dict so test paths that don't run inside an App keep working.
#
# The flattened view is rebuilt on every access — sub-optimal, but
# selectors cache around it (``select_remote_rows`` keys on
# ``id(slot.value)`` not ``id(snapshots)``) so the rebuild cost is
# bounded by the number of configured hosts.


def _tui_remote_snapshots_get(self: TuiContext) -> dict:
    state = getattr(self, "_state", None)
    if state is not None and state.remote:
        return {name: slot.value for name, slot in state.remote.items() if slot.value is not None}
    return self.__dict__.get("_legacy_remote_snapshots", {})


def _tui_remote_snapshots_set(self: TuiContext, value: dict) -> None:
    self.__dict__["_legacy_remote_snapshots"] = value


TuiContext.remote_snapshots = property(  # type: ignore[assignment]
    _tui_remote_snapshots_get,
    _tui_remote_snapshots_set,
)


# ── agent_availability / detected_agents shim properties ────────────
#
# Stage 8 commit 5a: the canonical store moves to
# ``state.agent_availability`` and ``state.detected_agents`` (each a
# :class:`SlotState[dict[...]]`). The shim properties read
# ``state.<slot>.value`` when a state is linked — and crucially
# *expose the same dict object* the slot holds, so today's
# worker-thread in-place mutations
# (``self.ctx.agent_availability[aid] = …``) continue to land on
# state. This commit does not change semantics: the worker-thread
# race is identical to today's. The race fix lands in commit 5b
# (``_probe_host_worker`` builds local dicts and posts a
# :class:`SlotResult`; the on-loop handler runs ``apply``).


def _availability_get(self: TuiContext) -> dict:
    state = getattr(self, "_state", None)
    if state is not None and state.agent_availability.value is not None:
        return state.agent_availability.value
    return self.__dict__.get("_legacy_agent_availability", {})


def _availability_set(self: TuiContext, value: dict) -> None:
    self.__dict__["_legacy_agent_availability"] = value


def _detected_get(self: TuiContext) -> dict:
    state = getattr(self, "_state", None)
    if state is not None and state.detected_agents.value is not None:
        return state.detected_agents.value
    return self.__dict__.get("_legacy_detected_agents", {})


def _detected_set(self: TuiContext, value: dict) -> None:
    self.__dict__["_legacy_detected_agents"] = value


TuiContext.agent_availability = property(  # type: ignore[assignment]
    _availability_get,
    _availability_set,
)
TuiContext.detected_agents = property(  # type: ignore[assignment]
    _detected_get,
    _detected_set,
)


# ── link_health_status / cwd_writable shim properties ───────────────
#
# Stage 8 commit 6: ``state.link_health`` and ``state.cwd_writable``
# become canonical. Unlike ``agent_availability`` / ``detected_agents``
# (commit 5), these were never thread-race targets — both the
# link-health and cwd probes already posted messages and the on-loop
# handlers wrote ``ctx.<field>`` single-threaded. The migration is a
# data-shape rename so the carry-list can disappear, plus the
# cwd-change invalidation in the rebuild dispatcher.


def _link_health_get(self: TuiContext) -> LinkHealthStatus:
    state = getattr(self, "_state", None)
    if state is not None and state.link_health.value is not None:
        return state.link_health.value
    return self.__dict__.get("_legacy_link_health_status", LinkHealthStatus())


def _link_health_set(self: TuiContext, value: LinkHealthStatus) -> None:
    self.__dict__["_legacy_link_health_status"] = value


def _cwd_writable_get(self: TuiContext) -> bool | None:
    """Return the cached cwd-writable flag.

    Three-valued semantics survive the slot migration:
      ``None``  — probe still in flight or never ran
      ``True``  — launchable
      ``False`` — not launchable

    Pre-commit-6 ``ctx.cwd_writable is None`` doubled as the
    "loading" sentinel. Post-commit-6 the loading-vs-loaded
    distinction is structural: ``state.cwd_writable.last_attempt_at
    is None`` means never-loaded, regardless of value. Existing
    readers that only need the tri-state result (the launch-row
    decoration) still see the ``bool | None`` value through this
    shim.
    """
    state = getattr(self, "_state", None)
    if state is not None and state.cwd_writable.last_attempt_at is not None:
        return state.cwd_writable.value
    return self.__dict__.get("_legacy_cwd_writable", None)


def _cwd_writable_set(self: TuiContext, value: bool | None) -> None:
    self.__dict__["_legacy_cwd_writable"] = value


TuiContext.link_health_status = property(  # type: ignore[assignment]
    _link_health_get,
    _link_health_set,
)
TuiContext.cwd_writable = property(  # type: ignore[assignment]
    _cwd_writable_get,
    _cwd_writable_set,
)


# Number of action items at the top of the main list.
#
# Historically a loose module-level constant; as of PR 9 (2026-04-18)
# this is derived from :data:`_ACTION_KINDS` which is itself the
# canonical description of "what are the action rows". Keep
# ``ACTION_COUNT`` for backward compatibility with tests and for
# readability in segment arithmetic; new code should use
# :func:`build_items` / item.kind dispatch instead of raw indices.
_ACTION_KINDS: tuple[str, ...] = ("action-cwd", "action-new", "action-open")
ACTION_COUNT = len(_ACTION_KINDS)


@dataclass(frozen=True)
class Item:
    """One row on the main TUI screen, described by identity rather than index.

    ``kind`` names the semantic role of the row. ``digit_hint`` is the
    digit that would activate it on keypress, or None if the row cannot
    be reached by a digit jump (Settings, Kill-ALL).

    This is the type-safe replacement for the integer-cursor scheme.
    Activation should dispatch on ``kind``, not on the position of the
    row inside the flat list — session-count changes shift every index
    below the new/removed session, but ``kind`` is stable.
    """

    kind: str  # one of: action-cwd, action-new, action-open,
    #                    own-session, other-session,
    #                    settings, kill-all-global
    label: str
    enabled: bool = True
    # Payloads (only one is populated per kind):
    session: TuiSession | None = None
    digit_hint: int | None = None  # 1..9, or None if not digit-reachable


def build_items(ctx: TuiContext) -> list[Item]:
    """Materialise the main-screen row list as a typed list of :class:`Item`.

    The returned list is the source of truth for what's on the main
    screen. Its order is the same order the current renderer uses, so
    integer indices computed by :func:`_segments` align 1:1 with
    positions in this list.

    Invariant: item identity (kind + label) is stable under session
    count changes for the action rows and for Settings / Kill-ALL;
    session rows have identity "own-session:<name>" / "other-session:
    <user>/<name>" so their position shifts but the item that used to
    be "Open existing project" remains the same Item.
    """
    items: list[Item] = []
    # Actions (indices 0..ACTION_COUNT-1). Digit hints are 1..3.
    items.append(
        Item(
            kind="action-cwd",
            label="New session in current folder",
            enabled=ctx.cwd_writable is not False,
            digit_hint=1,
        )
    )
    items.append(
        Item(
            kind="action-new",
            label="Create new project",
            enabled=True,
            digit_hint=2,
        )
    )
    items.append(
        Item(
            kind="action-open",
            label="Open existing project",
            enabled=bool(ctx.existing_projects),
            digit_hint=3,
        )
    )
    # Own sessions
    for i, s in enumerate(ctx.sessions):
        pos = ACTION_COUNT + i  # 0-based position in the final list
        hint = pos + 1 if 1 <= pos + 1 <= 9 else None
        items.append(
            Item(
                kind="own-session",
                label=s.short,
                enabled=True,
                session=s,
                digit_hint=hint,
            )
        )
    # Superuser block: other-user sessions, settings, kill-all-reachable.
    # Visibility gate is now per-target sudo: any reachable peer user
    # exposes the block. Settings remains gated on the same predicate
    # for backward layout compatibility — its writability separately
    # depends on ``sudo_caps.can_root`` and is wired through
    # ``repo_config_writable`` in ``cli._build_tui_context``.
    has_super = bool(ctx.sudo_caps.reachable_users)
    if has_super:
        for i, s in enumerate(ctx.other_sessions):
            pos = ACTION_COUNT + len(ctx.sessions) + i
            hint = pos + 1 if 1 <= pos + 1 <= 9 else None
            items.append(
                Item(
                    kind="other-session",
                    label=f"{s.user}/{s.short}",
                    enabled=True,
                    session=s,
                    digit_hint=hint,
                )
            )
        # Settings: no digit_hint — PR 2 invariant.
        items.append(
            Item(
                kind="settings",
                label="⚙ Settings",
                enabled=True,
            )
        )
        total_sessions = len(ctx.sessions) + len(ctx.other_sessions)
        if total_sessions > 0:
            # Kill-ALL (reachable users): no digit_hint — PR 2 invariant.
            # ``kind`` is kept as the legacy ``"kill-all-global"`` string
            # so the screen / state dispatch tables don't all need to
            # rename in lock-step with the user-visible relabel.
            items.append(
                Item(
                    kind="kill-all-global",
                    label=f"⚡ Kill ALL ({total_sessions})",
                    enabled=True,
                )
            )
    return items


# ── Segment / index map ─────────────────────────────────────────────
#
# Without sudo:
#   [actions: 0..ACTION_COUNT) | [own: ACTION_COUNT..ACTION_COUNT+len(own))
#
# With sudo (superuser block always available):
#   ... | [own] | [other-user sessions] | ⚙ Settings | [Kill ALL (all users)]
#                                                     ^ only when any session exists


def _segments(ctx: TuiContext) -> tuple[int, int, int, int, bool]:
    """Return (own_start, other_start, settings_idx, kill_global_idx, has_super).

    Indexes that don't apply return -1. ``has_super`` is True iff the
    caller has at least one reachable peer user via per-target sudo
    (``ctx.sudo_caps.reachable_users``).
    """
    own_start = ACTION_COUNT
    other_start = own_start + len(ctx.sessions)
    has_super = bool(ctx.sudo_caps.reachable_users)
    if not has_super:
        return own_start, other_start, -1, -1, False
    settings_idx = other_start + len(ctx.other_sessions)
    total_sessions = len(ctx.sessions) + len(ctx.other_sessions)
    kill_global_idx = settings_idx + 1 if total_sessions > 0 else -1
    return own_start, other_start, settings_idx, kill_global_idx, True


def _total_items(ctx: TuiContext) -> int:
    _, _, settings_idx, kill_idx, has_super = _segments(ctx)
    if not has_super:
        return ACTION_COUNT + len(ctx.sessions)
    if kill_idx >= 0:
        return kill_idx + 1
    return settings_idx + 1


def _digit_hinted_indices(ctx: TuiContext) -> set[int]:
    """Return the set of item indices reachable via a digit keypress.

    Digit 1..9 maps to index 0..8. Only items whose index is in this set
    may be activated by a digit keypress. Settings and Kill-ALL are
    deliberately excluded — they are non-destructive-to-read but
    surprising-to-land-on for a new user, and on empty superuser state
    `settings_idx` collapses to `ACTION_COUNT` which makes a mis-typed
    digit dangerously ambiguous. Both remain reachable via
    arrow-down + Enter, which is a deliberate two-step gesture.
    """
    own_start, other_start, settings_idx, kill_idx, has_super = _segments(ctx)
    total = _total_items(ctx)
    allowed: set[int] = set()
    # Actions (0..ACTION_COUNT-1)
    for i in range(min(ACTION_COUNT, total)):
        allowed.add(i)
    # Own sessions
    for i in range(own_start, min(other_start, total)):
        allowed.add(i)
    # Other users' sessions (still session rows, safe to jump to)
    if has_super:
        other_end = settings_idx if settings_idx >= 0 else total
        for i in range(other_start, min(other_end, total)):
            allowed.add(i)
    # Settings and Kill-ALL are intentionally excluded.
    return allowed

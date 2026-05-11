"""Frozen :class:`TuiConfig` — the immutable side of the TUI's state.

The TUI's state is split across three containers:

* **Configuration** — values fixed for the lifetime of the App
  (``enabled_agents``, refresh cadences, callbacks, the
  ``remote_hosts`` registry, …). Lives in :class:`TuiConfig`.
* **Async slots** — per-source state mutated by message-loop
  handlers. Lives in :class:`uxon.tui.state.TuiState`.
* **Rebuild output** — fields the local rebuild source emits each
  tick. Lives in :class:`uxon.tui.main_data.MainData`.

The dataclass is ``frozen=True`` so the lifetime invariant
("immutable across rebuilds") is type-level.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from uxon.remote_hosts import RemoteHost
    from uxon.tui.refresh import SourceSpec

    from .context import LaunchRequest, TuiContext


@dataclass(frozen=True)
class TuiConfig:
    """Immutable configuration snapshot consumed by the TUI.

    Populated from a :class:`TuiContext` at App-construction time via
    :meth:`from_context`. The fields here are exactly the ones that do
    not change across a rebuild tick — ``on_refresh`` returns a fresh
    ctx with new sessions / server_status / cwd_short, but the
    callbacks, the remote-hosts registry, the refresh-source list, and
    the cadence knobs are stable for the App's lifetime.

    Callable fields (``on_attach``, ``on_kill``, …) are stored by
    reference. They make the dataclass non-equality-friendly (function
    identities differ even when wrapping the same closure), which is
    why we deliberately avoid asserting structural equality between
    two :class:`TuiConfig` instances. The known cleanliness debt
    (callbacks are injected behaviour, not configuration) is filed
    in the migration plan; this commit keeps them on
    :class:`TuiConfig` to land the structural split first.

    Tuple-of-pairs is used for ``git_remote_profile_options`` because
    a frozen container with ``list`` would fail at construction time
    in any code path that re-uses the same options across instances —
    and because tuples make identity-stability assertions across
    :meth:`from_context` calls meaningful.
    """

    # ── Core identity ────────────────────────────────────────────────
    current_user: str
    launch_user: str

    # ── Multi-agent ──────────────────────────────────────────────────
    enabled_agents: tuple[str, ...]
    default_agent: str

    # ── Cadence knobs ────────────────────────────────────────────────
    tui_refresh_interval_seconds: float
    tui_ssh_refresh_interval_seconds: float
    tui_render_debounce_ms: int
    tui_render_max_latency_ms: int

    # ── Multi-host transport ─────────────────────────────────────────
    ssh_multiplex: str
    fetch_concurrency: int
    remote_hosts: tuple[RemoteHost, ...]
    refresh_sources: tuple[SourceSpec, ...]

    # ── Git-remote display options ───────────────────────────────────
    git_create_enabled: bool
    default_git_remote_profile: str
    git_remote_profile_options: tuple[tuple[str, str], ...]

    # ── Callbacks (injected by ``cli._build_tui_context``) ───────────
    on_attach: Callable[[str, str], LaunchRequest]
    on_kill: Callable[[str, str], None]
    on_kill_all: Callable[[], None]
    on_kill_all_global: Callable[[], None]
    on_remote_kill: Callable[[str, str, str], None]
    on_remote_attach: Callable[[str, str, str], LaunchRequest]
    on_refresh: Callable[[], TuiContext]
    on_probe_link_health: Callable[[], Any]
    on_probe_cwd_writable: Callable[[], bool]
    on_launch_cwd: Callable[[str, str], LaunchRequest]
    on_launch_new: Callable[[str, str, str, str], LaunchRequest]
    on_launch_existing: Callable[[str, str, str], LaunchRequest]
    get_settings_entries: Callable[[], list]
    on_setting_save: Callable[[str, Any], None]
    on_setting_remove: Callable[[str], None]
    on_setting_save_mapping: Callable[[str, dict], None]
    get_git_remote_profile_rows: Callable[[], list]
    on_enable_detected_agent: Callable[[str], None]
    on_dismiss_detected_agent: Callable[[str], None]
    get_dismissed_detected_agents: Callable[[], list[str]]

    @classmethod
    def from_context(cls, ctx: TuiContext) -> TuiConfig:
        """Snapshot the immutable side of ``ctx`` into a :class:`TuiConfig`.

        Run once per App instance at ``__init__`` time. The returned
        object is shared across rebuilds; ``MainScreen`` and modals
        read from it instead of going through the live ``TuiContext``
        for fields that never change.

        ``ssh_multiplex`` and ``fetch_concurrency`` default to the
        values set on ``TuiContext`` (themselves seeded from the
        loaded :class:`uxon.cli.Config`). They live here so future
        scheduler / breaker work has one place to look.
        """
        return cls(
            current_user=ctx.current_user,
            launch_user=ctx.launch_user,
            enabled_agents=tuple(ctx.enabled_agents),
            default_agent=ctx.default_agent,
            tui_refresh_interval_seconds=float(ctx.tui_refresh_interval_seconds),
            tui_ssh_refresh_interval_seconds=float(ctx.tui_ssh_refresh_interval_seconds),
            tui_render_debounce_ms=int(ctx.tui_render_debounce_ms),
            tui_render_max_latency_ms=int(ctx.tui_render_max_latency_ms),
            ssh_multiplex=ctx.ssh_multiplex,
            fetch_concurrency=ctx.fetch_concurrency,
            remote_hosts=tuple(ctx.remote_hosts),
            refresh_sources=tuple(ctx.refresh_sources or ()),
            git_create_enabled=ctx.git_create_enabled,
            default_git_remote_profile=ctx.default_git_remote_profile,
            git_remote_profile_options=tuple(
                (name, desc) for (name, desc) in ctx.git_remote_profile_options
            ),
            on_attach=ctx.on_attach,
            on_kill=ctx.on_kill,
            on_kill_all=ctx.on_kill_all,
            on_kill_all_global=ctx.on_kill_all_global,
            on_remote_kill=ctx.on_remote_kill,
            on_remote_attach=ctx.on_remote_attach,
            on_refresh=ctx.on_refresh,
            on_probe_link_health=ctx.on_probe_link_health,
            on_probe_cwd_writable=ctx.on_probe_cwd_writable,
            on_launch_cwd=ctx.on_launch_cwd,
            on_launch_new=ctx.on_launch_new,
            on_launch_existing=ctx.on_launch_existing,
            get_settings_entries=ctx.get_settings_entries,
            on_setting_save=ctx.on_setting_save,
            on_setting_remove=ctx.on_setting_remove,
            on_setting_save_mapping=ctx.on_setting_save_mapping,
            get_git_remote_profile_rows=ctx.get_git_remote_profile_rows,
            on_enable_detected_agent=ctx.on_enable_detected_agent,
            on_dismiss_detected_agent=ctx.on_dismiss_detected_agent,
            get_dismissed_detected_agents=ctx.get_dismissed_detected_agents,
        )

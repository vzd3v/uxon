# SPDX-License-Identifier: MIT
"""Builder for the live :class:`TuiContext` from session data.

:func:`build_tui_context` constructs a fresh :class:`TuiBridge` on every
refresh, probes the live session/sudo/remote state, wraps each bridge
callback with :func:`_wrap_tui_callback`, and assembles the immutable
``TuiContext`` the TUI renders. The nested :func:`_make_remote_fetch`
closure is kept co-located here on purpose: it captures the per-build
fetch semaphore and per-host circuit breaker, so the per-build state
contract lives in one place.

This module lives in ``tui/`` and may import ``domain`` / ``infra`` /
``app`` / ``tui``; it must NOT import ``uxon.cli``.
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING, Any

from uxon.domain.config import Config
from uxon.domain.format import compact_time, fmt_epoch, format_cpu_pct, format_rss_kib
from uxon.domain.session import SessionInfo, to_tui_session
from uxon.infra import (
    host_status_probe,
    identity,
    sessions_probe,
    version_probe,
)
from uxon.tui.bridge import TuiBridge
from uxon.tui.callback_wrap import _wrap_tui_callback

if TYPE_CHECKING:
    from uxon.domain.sudo import SudoCapability
    from uxon.tui.context import TuiContext


def _list_existing_projects(cfg: Config, launch_user: str, root: str) -> list[tuple[str, str]]:
    """List ``(name, compact_mtime)`` under ``new_project_root``, sorted by name.

    ``compact_mtime`` uses :func:`compact_time`: ``HH:MM`` if the
    directory was last modified today, ``MM-DD`` otherwise. ``"-"``
    when the stat call fails.
    """
    from uxon.infra.execution import list_directories

    entries = list_directories(cfg, launch_user, root)
    result: list[tuple[str, str]] = []
    for entry in entries:
        result.append((entry.name, compact_time(fmt_epoch(str(entry.mtime)))))
    return result


def build_tui_context(
    cfg: Config,
    caller_user: str,
    launch_user: str,
    cwd: str,
    *,
    skeleton: bool = False,
    sudo_caps_override: SudoCapability | None = None,
) -> TuiContext:
    """Build a TuiContext from live session data.

    When ``skeleton=True`` we skip every blocking I/O call (tmux, sudo
    probes, project directory scans) and return a minimal context with
    ``loading=True``. The TUI mounts immediately and a background worker
    triggers ``on_refresh`` (which calls this again with
    ``skeleton=False``) to fill in the real data.

    ``sudo_caps_override`` lets the caller (typically ``on_refresh``)
    reuse a previously-probed :class:`SudoCapability` instead of
    re-running the probe. Probing is one-shot at startup; new sudo grants are picked
    up by restarting ``uxon``, not by polling. When ``None`` and
    ``skeleton=False``, the function probes once.
    """
    from uxon.domain.status import ServerStatus
    from uxon.domain.sudo import SudoCapability
    from uxon.infra.sudo_probe import probe_sudo_capability
    from uxon.tui.context import (  # noqa: PLC0415
        CallbackError,
        LaunchProfileOption,
        TuiContext,
    )

    bridge = TuiBridge(cfg, caller_user, launch_user, cwd)

    if skeleton:
        # Skeleton ctx skips the per-target probe — it's the fast first
        # frame, and the real probe runs below when the worker calls
        # back with skeleton=False.
        sudo_caps = SudoCapability()
        own: list[SessionInfo] = []
        other: list[SessionInfo] = []
        skipped_users: tuple[str, ...] = ()
    else:
        from uxon.infra import demo as _uxon_demo_ctx  # noqa: PLC0415

        _demo_dir = _uxon_demo_ctx.demo_hosts_dir()
        if _demo_dir is not None:
            # Single demo seam for local sessions: pull the agent-user
            # scope and per-user records straight from _local.json.
            # Bypasses sudo probe + tmux.tmux_socket_path (the production
            # collectors reject synthetic users that don't exist as
            # OS accounts) and keeps every demo-only branch inside this
            # one block.
            scope_users = _uxon_demo_ctx.load_demo_local_scope_users(_demo_dir)
            own = _uxon_demo_ctx.load_demo_local_sessions(_demo_dir, launch_user)
            other = []
            for _u in scope_users:
                if _u == launch_user:
                    continue
                other.extend(_uxon_demo_ctx.load_demo_local_sessions(_demo_dir, _u))
            sudo_caps = SudoCapability(
                reachable_users=frozenset(u for u in scope_users if u != launch_user)
            )
            skipped_users = ()
        else:
            # One-shot probe: the candidate set is ``session_users \ {self}``.
            # Self is filtered before probing because ``sudo -n -H -u <self>``
            # trivially succeeds and would inflate ``reachable_users``
            # with a meaningless entry.
            candidates = [
                u for u in identity.resolve_all_session_users(cfg, launch_user) if u != launch_user
            ]
            if sudo_caps_override is not None:
                sudo_caps = sudo_caps_override
            else:
                sudo_caps = probe_sudo_capability(cfg, candidates)
            own = sessions_probe.collect_sessions([launch_user], cfg)

            # Other-user sessions are scoped to the *reachable* subset.
            # Unreachable candidates are surfaced separately so the TUI
            # can show the "(2/4 users reachable)" hint.
            if sudo_caps.reachable_users:
                other = sessions_probe.collect_sessions(sorted(sudo_caps.reachable_users), cfg)
            else:
                other = []
            skipped_users = tuple(
                sorted(u for u in candidates if u not in sudo_caps.reachable_users)
            )
        # ``list.peek`` fires when the TUI actually
        # enumerates cross-user sessions (gated by ``enable_all_users_list``
        # and ``reachable_users`` being non-empty).  CLI ``uxon list
        # --all-users`` emits its own ``list.peek`` from the list block;
        # the TUI refresh path is the second documented site and was
        # previously silent.
        if cfg.enable_all_users_list and sudo_caps.reachable_users:
            from uxon.infra import audit as _audit

            _audit.audit(
                "list.peek",
                scope_users=sorted({launch_user, *sudo_caps.reachable_users}),
                scope_skipped=list(skipped_users),
            )

        own.sort(key=lambda s: s.name)
        other.sort(key=lambda s: (s.user, s.name))

    bridge.sudo_caps = sudo_caps
    # Subtlety: a *skeleton* ctx has empty placeholder caps, not real
    # ones. If we captured those, the first real load would reuse the
    # empty placeholder and never probe. So skeleton's on_refresh passes
    # None, which forces the probe on the first non-skeleton load. Every
    # refresh after that reuses the captured real caps.
    bridge.captured_sudo_caps = None if skeleton else sudo_caps

    tui_own = [to_tui_session(s, cfg.session_prefix, cfg.legacy_session_prefixes) for s in own]
    tui_other = [to_tui_session(s, cfg.session_prefix, cfg.legacy_session_prefixes) for s in other]

    total_cpu = format_cpu_pct(sum(s.cpu_pct for s in own) + sum(s.cpu_pct for s in other))
    total_ram = format_rss_kib(sum(s.rss_kib for s in own) + sum(s.rss_kib for s in other))

    home = os.path.expanduser("~")
    cwd_short = cwd.replace(home, "~") if cwd.startswith(home) else cwd

    # Wrap all callbacks so failures surface on the TUI status line instead of
    # killing uxon silently (blessed's fullscreen context hides stderr + tracebacks).
    _CbErr = CallbackError
    on_attach = _wrap_tui_callback(bridge.on_attach, _CbErr)
    on_kill = _wrap_tui_callback(bridge.on_kill, _CbErr)
    on_kill_all = _wrap_tui_callback(bridge.on_kill_all, _CbErr)
    on_kill_all_reachable = _wrap_tui_callback(bridge.on_kill_all_reachable, _CbErr)
    on_remote_kill = _wrap_tui_callback(bridge.on_remote_kill, _CbErr)
    # on_remote_attach already raises CallbackError directly (see
    # TuiBridge.on_remote_attach) — no _wrap_tui_callback shim needed.
    on_remote_attach = bridge.on_remote_attach
    on_refresh = _wrap_tui_callback(bridge.on_refresh, _CbErr)
    on_probe_link_health = _wrap_tui_callback(bridge.on_probe_link_health, _CbErr)
    on_probe_cwd_writable = _wrap_tui_callback(bridge.on_probe_cwd_writable, _CbErr)
    on_probe_dir_launchable = _wrap_tui_callback(bridge.on_probe_dir_launchable, _CbErr)
    on_launch_cwd = _wrap_tui_callback(bridge.on_launch_cwd, _CbErr)
    on_launch_new = _wrap_tui_callback(bridge.on_launch_new, _CbErr)
    on_launch_existing = _wrap_tui_callback(bridge.on_launch_existing, _CbErr)
    on_runtime_gate = _wrap_tui_callback(bridge.on_runtime_gate, _CbErr)
    on_probe_existing_sessions = _wrap_tui_callback(bridge.on_probe_existing_sessions, _CbErr)
    on_git_remote_options = _wrap_tui_callback(bridge.on_git_remote_options, _CbErr)
    on_probe_worktrees = _wrap_tui_callback(bridge.on_probe_worktrees, _CbErr)
    on_create_worktree = _wrap_tui_callback(bridge.on_create_worktree, _CbErr)
    on_launch_existing_worktree = _wrap_tui_callback(bridge.on_launch_existing_worktree, _CbErr)
    on_probe_existing_worktree_sessions = _wrap_tui_callback(
        bridge.on_probe_existing_worktree_sessions, _CbErr
    )
    get_settings_entries = _wrap_tui_callback(bridge.get_settings_entries, _CbErr)
    on_setting_save = _wrap_tui_callback(bridge.on_setting_save, _CbErr)
    on_setting_remove = _wrap_tui_callback(bridge.on_setting_remove, _CbErr)
    on_setting_save_mapping = _wrap_tui_callback(bridge.on_setting_save_mapping, _CbErr)
    get_git_remote_profile_rows = _wrap_tui_callback(bridge.get_git_remote_profile_rows, _CbErr)

    # Reflects whether the "new session in current folder" row should be
    # enabled — same predicate the click handler will apply, so the row
    # state never lies. Same-user fast path runs synchronously (os.access
    # under the hood); cross-user case leaves the value None so the TUI
    # ships the first frame fast and an app worker probes via sudo
    # without blocking the event loop.
    import uxon.app.launch as launch_app
    from uxon.infra.execution import resolve_target

    if (
        identity.process_user() == launch_user
        and resolve_target(cfg, launch_user).backend.kind == "local"
    ):
        cwd_writable: bool | None = launch_app.is_launch_target_allowed(cfg, launch_user, cwd)
    else:
        cwd_writable = None

    default_launch_user = identity.resolve_launch_user(cfg, caller_user)
    enabled_profiles = cfg.launch.effective_enabled_profiles
    launch_profile_options: dict[str, LaunchProfileOption] = {}
    for profile_id in enabled_profiles:
        profile = cfg.launch.profiles.get(profile_id)
        if profile is None:
            continue
        profile_launch_user = profile.launch_user or default_launch_user
        launch_profile_options[profile_id] = LaunchProfileOption(
            id=profile.id,
            label=profile.display_name or profile.id,
            agent=profile.agent,
            launch_user=profile_launch_user,
            runtime=profile.runtime,
        )

    from uxon.infra import agents as _uxon_agents

    agent_availability = {
        pid: _uxon_agents.AgentAvailability(status="pending")
        for pid in enabled_profiles
        if not cfg.launch.auto_mode
    }

    if skeleton:
        existing_projects: list[tuple[str, str]] = []
        server_status = ServerStatus()
    else:
        existing_projects = _list_existing_projects(cfg, launch_user, cfg.new_project_root)
        server_status = host_status_probe.read_server_status(cfg, launch_user, cfg.new_project_root)

    # Pluggable refresh sources include one local context rebuild and one
    # source per configured remote host.
    # The skeleton ctx still gets the full source list. SourceSpec
    # construction is pure (just stores names + lambdas), no I/O, so
    # there is no cost to wiring it on the fast-path. The ctx is what
    # ``MainScreen.on_mount`` reads to fan out the initial refresh —
    # an empty list there means the "Loading sessions…" placeholder
    # never gets replaced.
    from uxon.domain.host_breaker import BreakerSpec, HostBreaker
    from uxon.domain.wire_schema import RemoteSnapshot
    from uxon.infra.remote.cache import read_cached_snapshot
    from uxon.infra.remote.collector import fetch_remote_snapshot
    from uxon.tui.refresh import SourceSpec

    # ``main_ctx_rebuild`` returns a fresh ``TuiContext``. The app's
    # source-result handler routes this into ``apply_loaded_ctx``.
    # The lambda captures ``on_refresh`` by name; by the time the
    # registry runs the fetch on a worker thread, ``on_refresh`` has
    # already been replaced (a few lines above) by its
    # ``_wrap_tui_callback`` shim. So a SystemExit / ``fail()`` from
    # inside the rebuild surfaces as ``CallbackError``, which
    # ``run_source`` captures into ``SourceResult.error`` for
    # fail-soft delivery.
    refresh_sources: list = [
        SourceSpec(
            name="main_ctx_rebuild",
            fetch=lambda: on_refresh(),
            cadence_seconds_attr="tui_refresh_interval_seconds",
            kick_on_mount=True,
        ),
    ]
    # One source per configured remote host. Each runs in its own
    # worker group (``refresh:remote:<name>``) so a slow / dead
    # peer can never stall the local-sessions stream or another
    # peer's poll. Cadence is the dedicated SSH interval — peers
    # are polled less aggressively than the local tmux stream.
    # Fleet-wide fetch-concurrency cap. Without this a 50-host peer
    # set recovering from an outage launches 50 concurrent
    # ``subprocess.Popen`` calls (each holding ≥3 pipe FDs), which
    # saturates the default 1024-FD ulimit before scheduling becomes
    # the bottleneck. Scope is per-``TuiContext`` instance — matches
    # the spec's "no worker survives App teardown" contract.
    fetch_sem = threading.Semaphore(cfg.fetch_concurrency)
    bridge._fetch_sem = fetch_sem

    # Per-host circuit breaker. One :class:`HostBreaker` per peer,
    # keyed by host name and captured by ``_make_remote_fetch``. The
    # breaker decides whether an SSH attempt fires; when open, the
    # fetcher short-circuits to a cache-only snapshot so the UI keeps
    # rendering the last good payload without the cost of yet another
    # doomed connect. ``BreakerSpec`` defaults are intentional — no
    # The breaker uses one fixed package policy across peers.
    host_breakers: dict[str, HostBreaker] = bridge._host_breakers

    def _make_remote_fetch(h, sem, multiplex, persist_seconds, breaker):
        def _fetch():
            # Breaker is the first gate: if it says "do not attempt",
            # skip the SSH layer entirely. We still produce a
            # ``RemoteSnapshot`` so the UI sees something — load the
            # last good cache if we have one; otherwise an empty
            # error snapshot. Either way the cadence-driven retry
            # path will get its next chance the moment the breaker
            # half-opens.
            if not breaker.should_attempt():
                cached = read_cached_snapshot(h.name)
                if cached is not None:
                    return cached
                return RemoteSnapshot(
                    host_name=h.name,
                    fetched_at_epoch=0.0,
                    from_cache=False,
                    error="circuit breaker open",
                    sessions=[],
                    cached_at_epoch=None,
                )
            breaker.mark_inflight()
            try:
                sem.acquire()
                try:
                    snap = fetch_remote_snapshot(
                        h,
                        ssh_multiplex=multiplex,
                        ssh_control_persist_seconds=persist_seconds,
                    )
                finally:
                    sem.release()
                # Translate the snapshot's success/failure into the
                # breaker's outcome reporting. A cache-fallback
                # snapshot (``from_cache=True``, ``error=<live>``)
                # means the live fetch did not succeed — count as
                # failure. ``error is None and not from_cache`` is a
                # real live success.
                if snap.error is None and not snap.from_cache:
                    breaker.on_success()
                else:
                    breaker.on_failure()
                return snap
            finally:
                # Defence in depth: if ``fetch_remote_snapshot`` ever
                # propagates (KeyboardInterrupt mid-tick, a test mock
                # that raises), the breaker must NOT stay in_flight=True
                # forever — that would permanently block the host's
                # next probe. ``on_success`` / ``on_failure`` already
                # clear the gate as a safety net, but the contract is
                # this finally.
                breaker.clear_inflight()

        return _fetch

    for host in cfg.remote_hosts:
        host_breakers[host.name] = HostBreaker(BreakerSpec())
        # Per-host cadence: ``host.interval`` (if set) wins over the
        # fleet-global ``tui_ssh_refresh_interval_seconds``. We pass
        # cadence_seconds_attr=None so the timer reads the explicit
        # value rather than a global cadence attribute.
        host_cadence = (
            float(host.interval)
            if host.interval is not None
            else float(cfg.tui_ssh_refresh_interval_seconds)
        )
        refresh_sources.append(
            SourceSpec(
                name=f"remote:{host.name}",
                fetch=_make_remote_fetch(
                    host,
                    fetch_sem,
                    cfg.ssh_multiplex,
                    cfg.ssh_control_persist_seconds,
                    host_breakers[host.name],
                ),
                cadence_seconds_attr=None,
                cadence_seconds=host_cadence,
                kick_on_mount=True,
            )
        )

    # Local /proc snapshot for the HostStatusBar's locals bucket. Skip
    # on the skeleton tick (first frame must paint immediately); on the
    # real refresh tick treat probe failure as "pending…" — the
    # selector renders the absence rather than the error.
    host_stats: Any = None
    if not skeleton:
        try:
            from uxon.infra.probes import read_host_stats

            host_stats = read_host_stats()
        except Exception:  # pragma: no cover — defensive
            host_stats = None

    return TuiContext(
        sessions=tui_own,
        total_cpu=total_cpu,
        total_ram=total_ram,
        version=version_probe.format_version(),
        cwd=cwd,
        cwd_short=cwd_short,
        new_project_root=cfg.new_project_root,
        existing_projects=existing_projects,
        server_status=server_status,
        loading=skeleton,
        host_stats=host_stats,
        tui_refresh_interval_seconds=cfg.tui_refresh_interval_seconds,
        tui_ssh_refresh_interval_seconds=cfg.tui_ssh_refresh_interval_seconds,
        ssh_multiplex=cfg.ssh_multiplex,
        ssh_control_persist_seconds=cfg.ssh_control_persist_seconds,
        fetch_concurrency=cfg.fetch_concurrency,
        cwd_writable=cwd_writable,
        current_user=launch_user,
        sudo_caps=sudo_caps,
        scope_skipped_users=skipped_users,
        other_sessions=tui_other,
        enabled_profiles=enabled_profiles,
        default_profile=cfg.launch.default_profile,
        launch_profiles=launch_profile_options,
        launch_auto_mode=cfg.launch.auto_mode,
        agents=dict(cfg.agents),
        execution=cfg.execution,
        launch_user=launch_user,
        agent_availability=agent_availability,
        on_attach=on_attach,
        on_kill=on_kill,
        on_kill_all=on_kill_all,
        on_kill_all_reachable=on_kill_all_reachable,
        on_remote_kill=on_remote_kill,
        on_remote_attach=on_remote_attach,
        on_refresh=on_refresh,
        on_probe_link_health=on_probe_link_health,
        on_probe_cwd_writable=on_probe_cwd_writable,
        on_probe_dir_launchable=on_probe_dir_launchable,
        on_launch_cwd=on_launch_cwd,
        on_launch_new=on_launch_new,
        on_launch_existing=on_launch_existing,
        on_runtime_gate=on_runtime_gate,
        on_probe_existing_sessions=on_probe_existing_sessions,
        on_git_remote_options=on_git_remote_options,
        on_probe_worktrees=on_probe_worktrees,
        on_create_worktree=on_create_worktree,
        on_launch_existing_worktree=on_launch_existing_worktree,
        on_probe_existing_worktree_sessions=on_probe_existing_worktree_sessions,
        get_settings_entries=get_settings_entries,
        on_setting_save=on_setting_save,
        on_setting_remove=on_setting_remove,
        on_setting_save_mapping=on_setting_save_mapping,
        get_git_remote_profile_rows=get_git_remote_profile_rows,
        git_create_enabled=cfg.git_create_enabled,
        refresh_sources=refresh_sources,
        remote_hosts=list(cfg.remote_hosts),
        tui_table_columns=cfg.tui_table_columns,
        tui_table_default_view=cfg.tui_table_default_view,
        tui_search_fields=cfg.tui_search_fields,
        tui_color_palette=cfg.tui_color_palette,
        local_host_color=cfg.local_host_color,
    )

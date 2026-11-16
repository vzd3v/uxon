"""Refresh-source landing reducers (pure given the App + event).

These are the free-function bodies of what used to be
``UxonApp._handle_main_ctx_rebuild`` / ``UxonApp._handle_remote_snapshot`` /
``UxonApp._build_source_dispatch`` so the
landed-result → state reduction is testable and reviewable on its own.

The App keeps the thin ``on__refresh_source_landed`` / ``_render_dirty``
shells (Textual message-routing surface) and calls :func:`build_source_dispatch`
once per instance to wire ``source-id → reducer``.

This module imports no Textual at import time — ``MainScreen`` is imported
lazily inside the reducer (only for the ``screen_stack`` ``isinstance`` check),
keeping the module's import graph Textual-free. The reducers reach the event
loop only through the injected App (``app.notify`` / ``app._render.request``),
never by importing Textual primitives.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

from uxon.infra.events import debug as _debug

from .context import TuiContext

if TYPE_CHECKING:
    from .app import UxonApp
    from .messages import _RefreshSourceLanded


def _source_key(ctx: TuiContext, name: str) -> str:
    return next((spec.key for spec in ctx.refresh_sources or () if spec.name == name), "")


def handle_main_ctx_rebuild(app: UxonApp, event: _RefreshSourceLanded) -> None:
    """Apply a ``main_ctx_rebuild`` landing into canonical state.

    Publishes sessions/configuration and advances ``state.refresh_tick``.
    Requests a render via the scheduler; the scheduler decides
    when ``apply_loaded_ctx`` actually fires.
    """
    from .screens.main import MainScreen

    if event.error:
        app.notify(f"Refresh failed: {event.error}", severity="error", timeout=6)
        return
    ctx = event.value if isinstance(event.value, TuiContext) else None
    if ctx is None:
        return
    target_changed = False
    # Session/config collection must not replace independently loaded data.
    if any(spec.name == "local_projects" for spec in ctx.refresh_sources or ()):
        previous = app.ctx
        same_root = _source_key(ctx, "local_projects") == _source_key(previous, "local_projects")
        target_changed = not same_root
        ctx = replace(
            ctx,
            existing_projects=previous.existing_projects if same_root else [],
            projects_loading=previous.projects_loading if same_root else True,
            projects_error=previous.projects_error if same_root else "",
            server_status=previous.server_status if same_root else ctx.server_status,
            stats_loading=previous.stats_loading if same_root else True,
            host_stats=previous.host_stats if same_root else None,
        )
    if not app._first_data_landed_logged:
        app._first_data_landed_logged = True
        _debug(
            "startup",
            at="first_data_landed",
            source=event.name,
            ts=time.monotonic(),
        )
    from .main_data import MainData

    app.state.refresh_tick += 1
    app.state.main = MainData.from_context(ctx)
    app.ctx = ctx
    app._latest_ctx = ctx
    screen = next((s for s in app.screen_stack if isinstance(s, MainScreen)), None)
    if screen is not None and hasattr(screen, "_id"):
        screen.loading = ctx.loading
    app._render.request("main_ctx")
    if target_changed:
        for spec in ctx.refresh_sources or ():
            if spec.name in {"local_projects", "local_stats"}:
                app._worker_coord.kick_source(spec)


def _apply_local_context(app: UxonApp, ctx: TuiContext, kind: str) -> None:
    from .main_data import MainData

    app.ctx = ctx
    app.state.main = MainData.from_context(ctx)
    app._latest_ctx = ctx
    app._render.request(kind)


def handle_projects(app: UxonApp, event: _RefreshSourceLanded) -> None:
    from .main_data import ProjectsData
    from .screens.existing import ExistingProjectScreen

    data = event.value
    if event.source_key and event.source_key != _source_key(app.ctx, event.name):
        return
    if not event.error and not isinstance(data, ProjectsData):
        return
    if isinstance(data, ProjectsData) and data.root != app.ctx.new_project_root:
        return  # A read started before the project root changed.
    ctx = replace(
        app.ctx,
        projects_loading=False,
        projects_error=event.error,
        existing_projects=list(data.projects)
        if isinstance(data, ProjectsData) and not event.error
        else app.ctx.existing_projects,
    )
    if (ctx.existing_projects, ctx.projects_loading, ctx.projects_error) != (
        app.ctx.existing_projects,
        app.ctx.projects_loading,
        app.ctx.projects_error,
    ):
        # An unchanged poll must not rebuild the modal's list or reset selection.
        for screen in app.screen_stack:
            if (
                isinstance(screen, ExistingProjectScreen)
                and screen.project_root == ctx.new_project_root
            ):
                screen.call_later(screen.update_root, ctx.existing_projects, ctx.projects_error)
        _apply_local_context(app, ctx, "projects")


def handle_local_stats(app: UxonApp, event: _RefreshSourceLanded) -> None:
    from .main_data import HostData

    data = event.value
    if event.source_key and event.source_key != _source_key(app.ctx, event.name):
        return
    if event.error:
        app.notify(f"Host statistics failed: {event.error}", severity="error", timeout=6)
        return
    if not isinstance(data, HostData):
        return
    ctx = replace(
        app.ctx, server_status=data.server_status, host_stats=data.host_stats, stats_loading=False
    )
    if (ctx.server_status, ctx.host_stats, ctx.stats_loading) != (
        app.ctx.server_status,
        app.ctx.host_stats,
        app.ctx.stats_loading,
    ):
        _apply_local_context(app, ctx, "stats")


def handle_remote_snapshot(app: UxonApp, event: _RefreshSourceLanded) -> None:
    """Apply a remote-host snapshot via the slot store.

    Source name is ``remote:<host>``. The fetcher always returns a
    :class:`RemoteSnapshot` (the collector is fail-soft); we wrap
    it in a :class:`SlotResult` and fold it into
    ``state.remote[host]`` via the pure :func:`slot_state.apply`.
    ``elapsed_ms`` from the fetcher is surfaced on the slot's
    ring so the latency-p50 tooltip has data.
    """
    host_name = event.name[len("remote:") :]
    from uxon.domain.wire_schema import RemoteSnapshot

    from .slot_state import SlotResult, SlotState
    from .slot_state import apply as apply_slot

    # Resolve attempted_at from the source result's timing
    # signature: ``time.time()`` at landing is close enough — the
    # fetcher itself doesn't emit a timestamp, and the wall-clock
    # at dispatch is what staleness logic compares against.
    attempted_at = time.time()

    snap = event.value if isinstance(event.value, RemoteSnapshot) else None
    if snap is None and not event.error:
        # Defensive: a remote source landed with neither a
        # RemoteSnapshot nor an error — drop without mutating.
        return

    error = event.error or (snap.error if snap is not None else None)
    if snap is not None and not snap.is_live:
        error = error or "unconfirmed remote snapshot"
    if error:
        snap = (
            replace(snap, sessions=[], host_stats=None, scope_skipped=[], error=error)
            if snap
            else RemoteSnapshot(
                host_name=host_name,
                fetched_at_epoch=attempted_at,
                error=error,
                error_code="fetch_error",
            )
        )
    result: SlotResult[RemoteSnapshot] = SlotResult(
        value=snap,
        error=error,
        elapsed_ms=event.elapsed_ms,
        attempted_at=attempted_at,
    )
    prev = app.state.remote.get(host_name) or SlotState[RemoteSnapshot]()
    updated = apply_slot(prev, result)
    # Generic slots preserve successful values; remote failures instead keep
    # their diagnostic DTO while excluding all unconfirmed session data.
    app.state.remote[host_name] = replace(updated, value=snap) if error else updated

    # Fold peer-emitted users into the cross-user latch accumulator.
    # If this snapshot brings the very first foreign user, the latch
    # flips False→True — request the ``main_ctx`` render kind so
    # :meth:`MainScreen.apply_loaded_ctx` re-evaluates the layout
    # signature (and mounts the USER column). Otherwise the
    # ``remote`` fast path is enough — row-level diff against the
    # updated slot, no recompose.
    prev_latched = len(app.main_ui.seen_users) > 1
    if snap is not None and snap.is_live and not error:
        for rec in snap.sessions:
            user = rec.get("user") if isinstance(rec, dict) else None
            if user:
                app.main_ui.seen_users.add(str(user))
    new_latched = len(app.main_ui.seen_users) > 1
    if not prev_latched and new_latched:
        app._render.request("main_ctx")
    else:
        app._render.request("remote")


def build_source_dispatch(
    app: UxonApp,
) -> tuple[
    dict[str, Callable[[_RefreshSourceLanded], None]],
    list[tuple[str, Callable[[_RefreshSourceLanded], None]]],
]:
    """Construct the (exact, prefix) dispatch registries bound to ``app``.

    Inspected by :meth:`UxonApp.on__refresh_source_landed` and by unit
    tests (no Pilot required). Adding a new source means adding an entry
    here in the same change as the source-spec registration.

    Handlers bind the free reducers to ``app`` through ``lambda event`` closures.
    """
    exact: dict[str, Callable[[_RefreshSourceLanded], None]] = {
        "main_ctx_rebuild": lambda event: handle_main_ctx_rebuild(app, event),
        "local_projects": lambda event: handle_projects(app, event),
        "local_stats": lambda event: handle_local_stats(app, event),
    }
    # Prefix matchers are scanned in order; the first match wins.
    prefix: list[tuple[str, Callable[[_RefreshSourceLanded], None]]] = [
        ("remote:", lambda event: handle_remote_snapshot(app, event)),
    ]
    return exact, prefix

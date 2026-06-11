"""Refresh-source landing reducers (pure given the App + event).

These are the free-function bodies of what used to be
``UxonApp._handle_main_ctx_rebuild`` / ``UxonApp._handle_remote_snapshot`` /
``UxonApp._build_source_dispatch``. Extracted in Phase P7 so the
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
from typing import TYPE_CHECKING

from uxon.infra.events import debug as _debug

from .context import TuiContext

if TYPE_CHECKING:
    from .app import UxonApp
    from .messages import _RefreshSourceLanded


def handle_main_ctx_rebuild(app: UxonApp, event: _RefreshSourceLanded) -> None:
    """Apply a ``main_ctx_rebuild`` landing into canonical state.

    Sole writer of ``state.refresh_tick`` and ``state.main``.
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
    app._latest_ctx = ctx
    screen = next((s for s in app.screen_stack if isinstance(s, MainScreen)), None)
    if screen is not None and hasattr(screen, "_id"):
        screen.loading = app.state.main is None
    app._render.request("main_ctx")


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

    from_cache = bool(getattr(snap, "from_cache", False)) if snap is not None else False
    result: SlotResult[RemoteSnapshot] = SlotResult(
        value=snap,
        error=event.error or None,
        elapsed_ms=event.elapsed_ms,
        attempted_at=attempted_at,
        from_cache=from_cache,
    )
    prev = app.state.remote.get(host_name) or SlotState[RemoteSnapshot]()
    app.state.remote[host_name] = apply_slot(prev, result)

    # Fold peer-emitted users into the cross-user latch accumulator.
    # If this snapshot brings the very first foreign user, the latch
    # flips False→True — request the ``main_ctx`` render kind so
    # :meth:`MainScreen.apply_loaded_ctx` re-evaluates the layout
    # signature (and mounts the USER column). Otherwise the
    # ``remote`` fast path is enough — row-level diff against the
    # updated slot, no recompose.
    prev_latched = len(app.main_ui.seen_users) > 1
    if snap is not None:
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

    Handlers are partially-applied free reducers (``lambda event``
    closing over ``app``) so the routing surface stays identical to the
    pre-P7 bound-method registry while the reducer bodies live here.
    """
    exact: dict[str, Callable[[_RefreshSourceLanded], None]] = {
        "main_ctx_rebuild": lambda event: handle_main_ctx_rebuild(app, event),
    }
    # Prefix matchers are scanned in order; the first match wins.
    prefix: list[tuple[str, Callable[[_RefreshSourceLanded], None]]] = [
        ("remote:", lambda event: handle_remote_snapshot(app, event)),
    ]
    return exact, prefix

"""Pluggable refresh-source registry for the uxon TUI.

A *refresh source* is a piece of TUI state that updates asynchronously:
a fetcher (run inside a worker thread), a cadence (how often the
periodic timer kicks it), and a per-source identity (used as the
worker group, the in-flight gate key, and the debug topic suffix).

The registry exists so that adding a new asynchronous data stream —
e.g. a remote-host session collector that may block for seconds on
SSH — is a *declarative* addition rather than a wiring change in
``app.py``. Each source runs in its own worker, posts its result via
:class:`uxon.tui.app._RefreshSourceLanded`, and is in-flight-gated
independently from the rest. A slow or hung source can never delay
another source's update.

Pure data + ``Callable`` types. No textual / no subprocess / no I/O.
``run_source`` is the *only* function that actually invokes a fetcher,
and it is fail-soft by construction — fetchers may raise; their
exceptions are captured into :attr:`SourceResult.error` and never
propagate out of the worker.

Note on scope: the existing ``host_probe``, ``link_health``, and
``cwd_writable`` streams predate the registry and remain bespoke for
now. They are already independent workers with their own messages and
gates, so migrating them is a cleanup, not a correctness fix. New
asynchronous streams should be added through the registry.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class SourceSpec:
    """Declarative description of one refresh source.

    Attributes:
        name: Stable identifier. Used verbatim as the worker group
            (``f"refresh:{name}"``), the in-flight handle key, and the
            ``source=`` field in ``UXON_DEBUG=refresh`` log lines.
            ASCII, no whitespace, unique within a registry.
        fetch: Callable run inside a worker thread. Returns an opaque
            snapshot — the consumer (a handler dispatched on ``name``)
            knows how to interpret it. May raise; ``run_source``
            catches and stores into :attr:`SourceResult.error`.
        cadence_seconds_attr: Name of a :class:`TuiContext` attribute
            holding the refresh interval in seconds. The app reads
            this attribute at mount time to schedule a ``set_interval``
            timer that re-kicks the source. ``None`` means "no
            periodic timer" — useful for one-shot sources that only
            run once on mount.
        kick_on_mount: Whether the source should be kicked once at
            mount time, before the first periodic tick. Defaults to
            True (matches the legacy ``kick_refresh`` initial-load
            behaviour).
    """

    name: str
    fetch: Callable[[], object]
    cadence_seconds_attr: str | None = "tui_refresh_interval_seconds"
    kick_on_mount: bool = True


@dataclass(frozen=True)
class SourceResult:
    """Outcome of running a fetcher in a worker.

    Always returned by :func:`run_source`. The fetcher itself may
    raise; the wrapper captures the exception's message into
    :attr:`error` and sets :attr:`value` to ``None``. Consumers MUST
    check ``error`` before using ``value``.

    Attributes:
        name: Source identity (mirrors :attr:`SourceSpec.name`).
        value: Whatever the fetcher returned, or ``None`` on error.
        error: Short error message, or ``None`` on success.
        elapsed_ms: Wall time the fetcher took, in milliseconds.
            Useful for ``UXON_DEBUG=refresh`` cost diagnosis.
    """

    name: str
    value: object
    error: str | None
    elapsed_ms: int


def run_source(spec: SourceSpec) -> SourceResult:
    """Invoke ``spec.fetch`` with fail-soft semantics. Never raises a
    plain ``Exception``.

    Captures any ``Exception`` subclass into ``SourceResult.error`` so
    a misbehaving source cannot crash the worker thread or the event
    loop. ``BaseException`` subclasses that signal control flow rather
    than failure (``KeyboardInterrupt``, ``SystemExit``, and the
    ``BaseException``-derived ``asyncio.CancelledError`` in modern
    Pythons) propagate intentionally — Ctrl-C, process termination,
    and worker cancellation must remain effective.
    """
    t0 = time.monotonic()
    try:
        value = spec.fetch()
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        return SourceResult(
            name=spec.name,
            value=None,
            error=str(exc) or exc.__class__.__name__,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )
    return SourceResult(
        name=spec.name,
        value=value,
        error=None,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
    )

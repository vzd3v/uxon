"""Structured JSONL event log for the uxon TUI.

Every user-visible transition in the TUI writes one JSON line to
``${XDG_STATE_HOME:-~/.local/state}/uxon/tui-{launch_user}-YYYYMMDD.log``
(override with ``UXON_LOG_DIR``). Format is newline-delimited JSON;
each line is self-describing. Log writes are best-effort — a failure
here must NEVER crash the TUI or propagate into the fullscreen TUI
context.

Fields (all optional except ``ts`` and ``event``):
  ts              ISO-8601 UTC timestamp with seconds precision
  caller_user     real caller username (``os.getlogin()`` or $USER)
  launch_user     effective launch user (may differ under sudo)
  screen          the :class:`Screen` the event originated from
  event           short event name (``key``, ``activate``, ``launch``,
                   ``launch_completed``, ``refresh``, …)
  action          mapped action name from SCREEN_KEYMAP, when known
  key             raw key representation, for ``event == "key"``
  item_kind       kind of item activated, for ``event == "activate"``
  outcome         terminal outcome string (``ok``, ``cancel``,
                   ``rc=5``, ``error:<msg>``)
  extra           free-form dict for event-specific fields
"""

from __future__ import annotations

import os
from typing import Any

import platformdirs
import structlog

# Module-level structlog renderer for the off-by-default debug channel.
# We bypass structlog's logger configuration on purpose: the debug log
# is a per-day, per-user file whose path is recomputed per call (so a
# long-running TUI rolls cleanly across midnight, and so tests can
# patch ``_log_dir``). The renderer turns a record dict into one JSON
# line; we write it ourselves. This keeps the on-disk shape
# byte-identical to the prior ``json.dumps(...)`` output and to the
# format consumers (tests, ``jq`` pipelines) rely on.
_DEBUG_RENDERER: structlog.processors.JSONRenderer = structlog.processors.JSONRenderer(
    sort_keys=False,
    ensure_ascii=False,
)


def _default_log_dir() -> str:
    """Return the XDG-derived default log directory.

    Honours ``XDG_STATE_HOME``; falls back to ``~/.local/state``.
    Resolution is delegated to :mod:`platformdirs`, which honours the
    same env var on Linux. ``UXON_LOG_DIR`` overrides this in
    :func:`_log_dir`.
    """
    return platformdirs.user_state_dir("uxon", appauthor=False)


# Snapshot of the default at import time. Kept for backward-compat
# with code that imports the constant directly; live lookups go
# through ``_log_dir()``.
LOG_DIR = _default_log_dir()


def _log_dir() -> str:
    """Return the log directory, honouring ``UXON_LOG_DIR``."""
    return os.environ.get("UXON_LOG_DIR") or _default_log_dir()


# ── Debug logging (off by default; enable via UXON_DEBUG env) ────────
#
# Internal-only diagnostic channel. Off in production; instrumentation
# call sites stay in place and cost a single ``frozenset`` truthiness
# check when disabled. Goes to ``tui-debug-{user}-{date}.log`` next to
# the user-facing event log. Full design and conventions:
# ``docs/agents/debug-logging.md``.


def _parse_debug_topics() -> frozenset[str]:
    """Resolve ``UXON_DEBUG`` once at import. Empty → channel off."""
    raw = os.environ.get("UXON_DEBUG", "").strip().lower()
    if not raw:
        return frozenset()
    if raw in {"1", "true", "all", "*", "yes", "on"}:
        return frozenset({"*"})
    return frozenset(t.strip() for t in raw.split(",") if t.strip())


_DEBUG_TOPICS: frozenset[str] = _parse_debug_topics()


def debug(topic: str, **fields: Any) -> None:
    """Append one JSON line to the debug log iff ``UXON_DEBUG`` enables ``topic``.

    No-op when ``UXON_DEBUG`` is unset (one ``frozenset`` truthiness
    check), so call sites can be left in place after a bug is fixed —
    they cost nothing in production. Never raises.

    Output: ``${XDG_STATE_HOME:-~/.local/state}/uxon/tui-debug-{user}-{YYYYMMDD}.log``
    (honours ``UXON_LOG_DIR`` like the event log).

    Topic is required; arbitrary keyword fields merge into the JSON
    record. See ``docs/agents/debug-logging.md`` for conventions and
    the topic registry.
    """
    if not _DEBUG_TOPICS:
        return
    if "*" not in _DEBUG_TOPICS and topic not in _DEBUG_TOPICS:
        return
    try:
        import datetime

        now = datetime.datetime.now(datetime.UTC)
        record: dict[str, Any] = {
            "ts": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "topic": topic,
        }
        record.update(fields)

        log_dir = _log_dir()
        try:
            os.makedirs(log_dir, mode=0o2775, exist_ok=True)
        except OSError:
            pass

        user = os.environ.get("SUDO_USER") or os.environ.get("USER", "unknown")
        date_str = now.strftime("%Y%m%d")
        path = os.path.join(log_dir, f"tui-debug-{user}-{date_str}.log")
        # Render via structlog's JSONRenderer for shape parity with the
        # rest of the structured-logging surface; the on-disk format
        # remains one ``{"ts": ..., "topic": ..., ...}`` JSON object per
        # line. The first positional argument (logger) is unused by
        # the JSON processor.
        # JSONRenderer is statically typed ``str | bytes``; with our
        # config (no bytes serializer) it always returns ``str`` here.
        line = _DEBUG_RENDERER(None, "debug", record)
        assert isinstance(line, str)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        # Telemetry, not a correctness path — never crash the TUI.
        return


# ── Metrics (off by default; enable via UXON_METRICS=1) ──────────────
#
# Stage 10b — opt-in JSONL of source-attempt records, rotated at 1 MiB
# into ``.1`` and ``.2`` (cap 3 files total). Telemetry, not a
# correctness path: failures are swallowed, never raised. The path
# lives next to the event-log + debug-log under platformdirs'
# ``user_state_dir("uxon")``.

# Test seam: rotation threshold in bytes. Production default is 1 MiB
# (per spec). Tests override to a small value to exercise rotation
# without writing megabytes.
_METRICS_ROTATE_BYTES: int = 1024 * 1024


def _metrics_enabled() -> bool:
    """True iff ``UXON_METRICS`` is set to a truthy value.

    Resolved per-call (not snapshotted at import) so tests can flip
    the env var and the production process picks up an operator
    runtime change without restart.
    """
    raw = os.environ.get("UXON_METRICS", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _metrics_path() -> str:
    """Return the metrics-log path. Honours ``UXON_LOG_DIR``."""
    return os.path.join(_log_dir(), "metrics.jsonl")


def _rotate_metrics(path: str) -> None:
    """Shift ``metrics.jsonl`` → ``.1``, ``.1`` → ``.2``; drop ``.2``.

    Cap is 3 files total (``metrics.jsonl``, ``.1``, ``.2``). Old ``.2``
    is removed. Never raises — rotation is best-effort.
    """
    try:
        old2 = path + ".2"
        old1 = path + ".1"
        if os.path.exists(old2):
            try:
                os.remove(old2)
            except OSError:
                pass
        if os.path.exists(old1):
            try:
                os.rename(old1, old2)
            except OSError:
                pass
        if os.path.exists(path):
            try:
                os.rename(path, old1)
            except OSError:
                pass
    except Exception:
        return


def metrics_record(
    source_id: str,
    *,
    elapsed_ms: int,
    error: str | None,
    from_cache: bool = False,
    attempted_at: float | None = None,
) -> None:
    """Append one JSON line to ``metrics.jsonl`` if ``UXON_METRICS=1``.

    Rotates at ``_METRICS_ROTATE_BYTES`` (1 MiB by default) into ``.1``
    and ``.2`` files; cap is 3 files total. Telemetry path — never
    raises, never crashes the TUI.

    Fields:
      ts            ISO-8601 UTC timestamp (seconds precision)
      source_id     ``"main_ctx_rebuild"`` or ``"remote:<host>"``
      elapsed_ms    wall-time of the fetch attempt
      error         first-line error string, or ``null`` on success
      from_cache    True iff the result was served from on-disk cache
      attempted_at  optional epoch seconds (caller-supplied)
    """
    if not _metrics_enabled():
        return
    try:
        import datetime
        import json

        now = datetime.datetime.now(datetime.UTC)
        record: dict[str, Any] = {
            "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source_id": source_id,
            "elapsed_ms": int(elapsed_ms),
            "error": error,
            "from_cache": bool(from_cache),
        }
        if attempted_at is not None:
            record["attempted_at"] = float(attempted_at)

        log_dir = _log_dir()
        try:
            # 0o700 mirrors the SSH cache permission used elsewhere — the
            # metrics file may carry hostnames the operator considers
            # sensitive, so we keep it user-only.
            os.makedirs(log_dir, mode=0o700, exist_ok=True)
        except OSError:
            pass

        path = _metrics_path()
        # Pre-write rotation: check current size and shift if past
        # threshold. Append-only; we don't rotate mid-write.
        try:
            if os.path.exists(path) and os.path.getsize(path) >= _METRICS_ROTATE_BYTES:
                _rotate_metrics(path)
        except OSError:
            pass
        line = json.dumps(record, ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        # Telemetry — never crash the TUI.
        return


def _log_event(
    event: str,
    *,
    screen: Any = None,
    caller_user: str = "",
    launch_user: str = "",
    action: str = "",
    key: str = "",
    item_kind: str = "",
    outcome: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """Append one JSON line to today's uxon TUI log.

    Silent on failure — logging must NEVER break the TUI. A missing
    directory is created on the first call. Permission / write errors
    are swallowed.
    """
    try:
        import datetime
        import json

        now = datetime.datetime.now(datetime.UTC)
        record: dict[str, Any] = {
            "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "event": event,
        }
        if screen is not None:
            # screen may be a :class:`Screen` enum (legacy) or a plain str
            record["screen"] = getattr(screen, "value", screen)
        if caller_user:
            record["caller_user"] = caller_user
        if launch_user:
            record["launch_user"] = launch_user
        if action:
            record["action"] = action
        if key:
            record["key"] = key
        if item_kind:
            record["item_kind"] = item_kind
        if outcome:
            record["outcome"] = outcome
        if extra:
            record["extra"] = extra

        log_dir = _log_dir()
        try:
            os.makedirs(log_dir, mode=0o2775, exist_ok=True)
        except OSError:
            # Directory creation may fail (parent missing, no perm).
            # Try to write anyway — the open() below will raise and we
            # swallow that too.
            pass

        user = launch_user or caller_user or "unknown"
        date_str = now.strftime("%Y%m%d")
        path = os.path.join(log_dir, f"tui-{user}-{date_str}.log")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        # Any exception during logging is swallowed. Logging is telemetry,
        # not a correctness path.
        return

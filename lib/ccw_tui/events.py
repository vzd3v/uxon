"""Structured JSONL event log for the ccw TUI.

Every user-visible transition in the TUI writes one JSON line to
``/srv/work/logs/ccw/tui-{launch_user}-YYYYMMDD.log``. Format is
newline-delimited JSON; each line is self-describing. Log writes are
best-effort — a failure here must NEVER crash the TUI or propagate
into the fullscreen TUI context.

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

LOG_DIR = "/srv/work/logs/ccw"


def _log_dir() -> str:
    """Return the log directory, honouring an env-var override for tests."""
    return os.environ.get("CCW_LOG_DIR", LOG_DIR)


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
    """Append one JSON line to today's ccw TUI log.

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

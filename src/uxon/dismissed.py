"""Per-user persistence for dismissed detected-agent suggestions.

Why per-user (not repo config): uxon ships on shared multi-user VPS hosts;
persisting dismissals into the *shared* repo config silences the banner
for every user. The dismissed list is a per-user UI preference and lives
under ``${XDG_STATE_HOME:-$HOME/.local/state}/uxon/dismissed.json``.

Pure stdlib (json + os). No textual, no TUI imports.
"""

from __future__ import annotations

import json
from pathlib import Path

import platformdirs


def state_dir() -> Path:
    """Return the per-user state directory for uxon (created on demand).

    Honours ``XDG_STATE_HOME`` per the XDG Base Directory spec, falling
    back to ``~/.local/state``. Resolution is delegated to
    :mod:`platformdirs`, which honours the same env var on Linux.
    """
    return Path(platformdirs.user_state_dir("uxon", appauthor=False))


def dismissed_path() -> Path:
    """Path to the per-user dismissed-detected-agents JSON file."""
    return state_dir() / "dismissed.json"


def load_dismissed() -> list[str]:
    """Read the dismissed agent ids list. Missing / malformed file → []."""
    path = dismissed_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, OSError):
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    items = data.get("dismissed_detected_agents") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [str(x) for x in items if isinstance(x, str)]


def add_dismissed(agent_id: str) -> list[str]:
    """Add ``agent_id`` to the dismissed list, persist, and return the new list.

    Idempotent: re-dismissing an already-dismissed id is a no-op.
    """
    current = load_dismissed()
    if agent_id in current:
        return current
    new = [*current, agent_id]
    _write(new)
    return new


def _write(items: list[str]) -> None:
    """Atomically write the dismissed list, creating the state dir if needed."""
    path = dismissed_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"dismissed_detected_agents": items}, indent=2)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(payload + "\n", encoding="utf-8")
    tmp.replace(path)

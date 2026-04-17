"""Settings schema + repo-level config.toml read/write.

Single source of truth for which keys are user-editable through the TUI
superuser block, their type, and how to persist changes back to the
repo-level ``config/config.toml``.

Project-level ``.ccw.toml`` is never written from here — it is surfaced
read-only in the UI so operators can see where a given key's value came
from.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# ── Schema ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SettingSpec:
    key: str
    kind: str  # "string" | "bool" | "enum" | "array" | "table"
    description: str = ""
    choices: "tuple[str, ...] | None" = None  # for "enum"


SETTINGS_SPECS: tuple[SettingSpec, ...] = (
    SettingSpec("runtime_user", "string", "Launch user when default_launch_mode='fixed'."),
    SettingSpec(
        "default_launch_mode", "enum", "Who runs claude by default.", choices=("caller", "fixed")
    ),
    SettingSpec("enable_all_users_list", "bool", "Allow 'ccw list --all-users'."),
    SettingSpec(
        "launch_user_by_caller", "table", "Per-caller launch-user override (caller → launch_user)."
    ),
    SettingSpec(
        "session_users",
        "array",
        "Users scanned by 'list --all-users' and the TUI superuser block.",
    ),
    SettingSpec("allowed_roots", "array", "Directories ccw is allowed to run in."),
    SettingSpec("session_prefix", "string", "Tmux session name prefix."),
    SettingSpec("default_claude_args", "array", "Flags prepended to every claude invocation."),
    SettingSpec("new_project_root", "string", "Base directory for 'ccw new <name>'."),
    SettingSpec(
        "repeat_noninteractive_mode",
        "enum",
        "Non-TTY fallback when a compatible session already exists.",
        choices=("fail", "attach", "new"),
    ),
    SettingSpec(
        "tmux_socket_template",
        "string",
        "Per-user socket path. Placeholders: {user}, {uid}.",
    ),
)

TABLE_KEYS: tuple[str, ...] = tuple(spec.key for spec in SETTINGS_SPECS if spec.kind == "table")
SCHEMA_KEYS: tuple[str, ...] = tuple(spec.key for spec in SETTINGS_SPECS)


# ── Resolved entry (schema + current value + source) ─────────────────


@dataclass
class SettingEntry:
    spec: SettingSpec
    value: Any
    source: str  # "default" | "repo" | "project:<path>"
    editable: bool  # False when source is project-level — TUI never writes .ccw.toml


def resolve_setting_entries(
    repo_data: dict,
    project_data: dict,
    project_path: "Path | None",
    defaults: dict,
) -> list[SettingEntry]:
    """Merge the three layers and return one entry per schema key with source info."""
    out: list[SettingEntry] = []
    for spec in SETTINGS_SPECS:
        if spec.key in project_data:
            value = project_data[spec.key]
            source = f"project:{project_path}" if project_path else "project"
            editable = False
        elif spec.key in repo_data:
            value = repo_data[spec.key]
            source = "repo"
            editable = True
        else:
            value = defaults.get(spec.key)
            source = "default"
            editable = True
        out.append(SettingEntry(spec=spec, value=value, source=source, editable=editable))
    return out


# ── TOML rendering ───────────────────────────────────────────────────


def _escape_string(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _escape_key(k: str) -> str:
    if k and all(c.isalnum() or c in "_-" for c in k):
        return k
    return _escape_string(k)


def _format_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return _escape_string(v)
    if isinstance(v, list):
        parts = []
        for x in v:
            if isinstance(x, str):
                parts.append(_escape_string(x))
            else:
                parts.append(_format_value(x))
        return "[" + ", ".join(parts) + "]"
    raise ValueError(f"unsupported TOML value type: {type(v).__name__}")


def render_repo_config_toml(repo_data: dict) -> str:
    """Render a repo-level config.toml body from ``repo_data``.

    Keys are emitted in SETTINGS_SPECS order for stability. Comments in the
    original file are *not* preserved — callers must warn the user.
    The ``launch_user_by_caller`` table is always emitted at the end (even
    when empty) so operators see it when opening the file directly.
    """
    lines: list[str] = []

    for key in SCHEMA_KEYS:
        if key in TABLE_KEYS:
            continue
        if key in repo_data:
            lines.append(f"{key} = {_format_value(repo_data[key])}")

    for table_key in TABLE_KEYS:
        lines.append("")
        lines.append(f"[{table_key}]")
        table = repo_data.get(table_key) or {}
        if isinstance(table, dict):
            for sub_key in sorted(table):
                sub_val = table[sub_key]
                if not isinstance(sub_val, str):
                    raise ValueError(
                        f"{table_key}.{sub_key}: expected string value, got {type(sub_val).__name__}"
                    )
                lines.append(f"{_escape_key(sub_key)} = {_escape_string(sub_val)}")

    return "\n".join(lines) + "\n"


# ── Persistence ──────────────────────────────────────────────────────


def write_repo_config_toml(content: str, path: "Path | str") -> None:
    """Write ``content`` to ``path``. Tries a direct atomic write first; falls
    back to ``sudo tee`` when the destination is not writable by the current
    process (typical for a repo checkout owned by another service user).
    """
    path = Path(path)
    try:
        tmp = path.parent / (path.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
        return
    except (PermissionError, OSError):
        pass

    # Fall back to sudo tee (non-atomic but preserves ownership and mode).
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", delete=False, suffix=".toml"
    ) as f:
        f.write(content)
        tmp_path = f.name
    try:
        result = subprocess.run(
            ["sudo", "sh", "-c", f"cat {tmp_path!s} > {str(path)!s}"],
            capture_output=True,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"failed to write {path}: {stderr or 'unknown error'}")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ── Mutators ─────────────────────────────────────────────────────────


def apply_setting(repo_data: dict, key: str, new_value: Any) -> dict:
    """Return a new dict with repo_data[key] = new_value. Does not mutate input."""
    if key not in SCHEMA_KEYS:
        raise KeyError(f"unknown setting key: {key}")
    out = dict(repo_data)
    out[key] = new_value
    return out


def remove_setting(repo_data: dict, key: str) -> dict:
    """Return a new dict with repo_data[key] removed (reverting to default)."""
    if key not in SCHEMA_KEYS:
        raise KeyError(f"unknown setting key: {key}")
    out = dict(repo_data)
    out.pop(key, None)
    return out


def replace_mapping(repo_data: dict, key: str, new_mapping: dict) -> dict:
    """Return a new dict with repo_data[key] = new_mapping (for table kinds)."""
    spec_by_key = {spec.key: spec for spec in SETTINGS_SPECS}
    spec = spec_by_key.get(key)
    if spec is None or spec.kind != "table":
        raise KeyError(f"not a table setting: {key}")
    for k, v in new_mapping.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ValueError(f"table {key} requires string keys and values")
    out = dict(repo_data)
    out[key] = dict(new_mapping)
    return out

"""Settings schema + repo-level config.toml read/write.

Single source of truth for which keys are user-editable through the TUI
superuser block, their type, and how to persist changes back to the
repo-level ``config/config.toml``.

Project-level ``.ccw.toml`` is never written from here — it is surfaced
read-only in the UI so operators can see where a given key's value came
from.

Round-trip writes preserve comments: the existing TOML text is parsed
with ``tomlkit``, only the changed keys are mutated in the document tree,
and the document is re-serialized. If the file does not exist yet, a
minimal TOML is emitted from scratch.
"""

from __future__ import annotations

import subprocess
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
    SettingSpec("git_create_enabled", "bool", "Enable the git-remote-on-new-project flow."),
    SettingSpec(
        "default_git_remote_profile",
        "string",
        "Profile name used when --git-remote default is passed or picked as TUI default.",
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


# ── TOML rendering (minimal, for fresh files only) ───────────────────


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
    """Render a minimal repo-level config.toml body from scratch.

    Used only when there is no existing file to update (e.g. fresh
    install). No comments are emitted — an installer that wants a
    commented starter should ship a hand-written template.
    Keys are emitted in SETTINGS_SPECS order for stability. The
    ``launch_user_by_caller`` table is always emitted (even when empty)
    so operators see it when opening the file directly.
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


# ── Round-trip update (comment-preserving) ───────────────────────────


def update_repo_config_text(existing_text: str, updates: dict) -> str:
    """Apply ``updates`` to ``existing_text`` (a config.toml body) and
    return the new text with comments and formatting of untouched parts
    preserved byte-identical.

    ``updates`` maps schema keys to their new values. Table-kind keys
    (see :data:`TABLE_KEYS`) are replaced wholesale: the table body is
    rewritten but the table header line and any comments above it stay
    intact.

    Raises ``KeyError`` for unknown keys and ``ValueError`` for type
    mismatches (mirrors :func:`apply_setting`/:func:`replace_mapping`).
    """
    import tomlkit  # lazy: only the writer path pulls tomlkit in

    doc = tomlkit.parse(existing_text)
    for key, value in updates.items():
        if key not in SCHEMA_KEYS:
            raise KeyError(f"unknown setting key: {key}")
        if key in TABLE_KEYS:
            if not isinstance(value, dict):
                raise ValueError(f"{key} must be a mapping")
            tbl = tomlkit.table()
            for sub_k in sorted(value):
                sub_v = value[sub_k]
                if not isinstance(sub_k, str) or not isinstance(sub_v, str):
                    raise ValueError(f"table {key} requires string keys and values")
                tbl[sub_k] = sub_v
            doc[key] = tbl
        else:
            doc[key] = value
    return tomlkit.dumps(doc)


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

    # Fall back to ``sudo tee`` with content piped on stdin — avoids any
    # shell interpolation of the destination path (which is otherwise
    # attacker-influenced via repo checkout layout).
    result = subprocess.run(
        ["sudo", "tee", "--", str(path)],
        input=content.encode("utf-8"),
        capture_output=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"failed to write {path}: {stderr or 'unknown error'}")


def persist_repo_config_updates(path: "Path | str", updates: dict) -> None:
    """Read ``path`` (if it exists), apply ``updates`` via
    :func:`update_repo_config_text`, and write the result back.

    When the file is missing, a minimal starter is rendered: the updates
    alone are emitted with no accompanying comments.
    """
    path = Path(path)
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""
    new_text = update_repo_config_text(existing, updates)
    write_repo_config_toml(new_text, path)


# ── Mutators (in-memory dict helpers) ────────────────────────────────


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


def remove_repo_key(path: "Path | str", key: str) -> None:
    """Drop ``key`` from the repo-level config.toml. Preserves comments
    and formatting of untouched parts. No-op if file or key is missing.
    """
    import tomlkit

    if key not in SCHEMA_KEYS:
        raise KeyError(f"unknown setting key: {key}")
    path = Path(path)
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    doc = tomlkit.parse(existing)
    if key in doc:
        del doc[key]
        write_repo_config_toml(tomlkit.dumps(doc), path)

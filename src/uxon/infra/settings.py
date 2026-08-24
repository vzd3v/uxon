"""Settings schema + repo-level config.toml read/write.

Single source of truth for which keys are user-editable through the TUI
superuser block, their type, and how to persist changes back to the
repo-level ``config/config.toml``.

Round-trip writes preserve comments: the existing TOML text is parsed
with ``tomlkit``, only the changed keys are mutated in the document tree,
and the document is re-serialized. If the file does not exist yet, a
minimal TOML is emitted from scratch.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from uxon.infra import config_loader
from uxon.infra.run import run_query
from uxon.infra.settings_toml import set_dotted, update_repo_config_text

# ── Schema ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SettingSpec:
    key: str
    kind: str  # "string" | "number" | "bool" | "enum" | "array" | "table"
    description: str = ""
    choices: tuple[str, ...] | None = None  # for "enum"


SETTINGS_SPECS: tuple[SettingSpec, ...] = (
    SettingSpec("default_launch_user", "string", "Launch user when default_launch_mode='fixed'."),
    SettingSpec(
        "default_launch_mode", "enum", "Who runs claude by default.", choices=("caller", "fixed")
    ),
    SettingSpec("enable_all_users_list", "bool", "Allow 'uxon list --all-users'."),
    SettingSpec(
        "launch_user_by_caller", "table", "Per-caller launch-user override (caller → launch_user)."
    ),
    SettingSpec(
        "session_users",
        "array",
        "Users scanned by 'list --all-users' and the TUI superuser block.",
    ),
    SettingSpec("allowed_roots", "array", "Directories uxon is allowed to run in."),
    SettingSpec(
        "session_prefix", "string", "Tmux session name prefix used when creating new sessions."
    ),
    SettingSpec(
        "legacy_session_prefixes",
        "array",
        "Additional prefixes recognised for list/attach/kill (never used to create new sessions).",
    ),
    SettingSpec("new_project_root", "string", "Base directory for 'uxon new <name>'."),
    SettingSpec(
        "worktree_root",
        "string",
        "Base dir for uxon-managed worktrees. Empty = <repo>/.uxon/worktrees.",
    ),
    SettingSpec(
        "worktree_base",
        "enum",
        "Base ref for a new worktree branch: 'local' (no fetch) or 'remote' (git fetch first).",
        choices=("local", "remote"),
    ),
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
    SettingSpec(
        "tui_refresh_interval_seconds",
        "number",
        "Main TUI auto-refresh interval in seconds.",
    ),
    SettingSpec(
        "tui_ssh_refresh_interval_seconds",
        "number",
        "SSH link-health refresh interval in seconds.",
    ),
    SettingSpec(
        "tui.table.columns",
        "array",
        "Dashboard columns in display order. Empty == REGISTRY defaults.",
    ),
    SettingSpec("git_create_enabled", "bool", "Enable the git-remote-on-new-project flow."),
    SettingSpec(
        "tui.table.default_view",
        "enum",
        "Default dashboard view (by_host or flat).",
        choices=("by_host", "flat"),
    ),
    SettingSpec(
        "tui.search.fields",
        "array",
        "Fields the SearchBar substring-matches against. "
        "Allowed: name, user, host, path, cmd. Default ['name','user'].",
    ),
    SettingSpec(
        "tui.color_palette",
        "array",
        "Auto-cycle palette for remote-host blocks (Rich style names). "
        "Default ['cyan','blue']; no magenta, no red, no yellow.",
    ),
    SettingSpec(
        "local_host.color",
        "string",
        "Rich style spec painting the locals block. Default 'green'.",
    ),
    SettingSpec(
        "ssh_multiplex",
        "enum",
        "Reuse one SSH master connection across fetches.",
        choices=("auto", "off"),
    ),
    SettingSpec(
        "ssh_control_persist_seconds",
        "number",
        "ControlPersist for the multiplexed SSH master, integer seconds. "
        "Must be > 0; disable multiplexing via ssh_multiplex=off.",
    ),
    SettingSpec(
        "tmux.manage_options",
        "bool",
        "Apply uxon-managed tmux options to launched sessions (off by default). "
        "Turn on to apply the recommended set; edit the option lists in "
        "config.toml under [tmux.options]/[tmux.server_options]/"
        "[tmux.append_server_options].",
    ),
)

TABLE_KEYS: tuple[str, ...] = tuple(spec.key for spec in SETTINGS_SPECS if spec.kind == "table")
SCHEMA_KEYS: tuple[str, ...] = tuple(spec.key for spec in SETTINGS_SPECS)


# ── Dotted-key helper for nested dict/TOML lookup ────────────────────


def _get_dotted(doc: Any, dotted_key: str, default: Any = None) -> Any:
    """Walk nested dict/tomlkit tables, returning ``default`` if any key is missing."""
    node = doc
    for part in dotted_key.split("."):
        if not isinstance(node, dict) and not hasattr(node, "get"):
            return default
        if part not in node:
            return default
        node = node[part]
    return node


# ── Resolved entry (schema + current value + source) ─────────────────


@dataclass
class SettingEntry:
    spec: SettingSpec
    value: Any
    source: str  # "default" | "repo"
    editable: bool


def settings_specs_for(_agent_ids: tuple[str, ...]) -> tuple[SettingSpec, ...]:
    """Return the scalar settings schema.

    ``agent_ids`` is accepted for the old call surface; launch profiles and
    agent catalog tables are file-only and are not represented here.
    """
    return SETTINGS_SPECS


def resolve_setting_entries(
    repo_data: dict,
    project_data: dict,
    project_path: Path | None,
    defaults: dict,
    agent_ids: tuple[str, ...] = (),
) -> list[SettingEntry]:
    """Merge the three layers and return one entry per schema key with source info.

    ``project_data`` / ``project_path`` are accepted for the old call surface
    but ignored. Runtime policy is operator-owned and no project config layer
    is displayed.
    """
    del project_data, project_path
    out: list[SettingEntry] = []
    for spec in settings_specs_for(agent_ids):
        key = spec.key
        is_dotted = "." in key
        if is_dotted:
            repo_val = _get_dotted(repo_data, key, _MISSING)
            def_val = _get_dotted(defaults, key, None)
            if repo_val is not _MISSING:
                value = repo_val
                source = "repo"
                editable = True
            else:
                value = def_val
                source = "default"
                editable = True
        else:
            if key in repo_data:
                value = repo_data[key]
                source = "repo"
                editable = True
            else:
                value = defaults.get(key)
                source = "default"
                editable = True
        out.append(SettingEntry(spec=spec, value=value, source=source, editable=editable))
    return out


_MISSING = object()  # sentinel for dotted-key lookup


# ── Persistence ──────────────────────────────────────────────────────


def write_repo_config_toml(content: str, path: Path | str) -> None:
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
    result = run_query(
        ["sudo", "tee", "--", str(path)],
        input=content.encode("utf-8"),
        text=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"failed to write {path}: {stderr or 'unknown error'}")


def persist_repo_config_updates(path: Path | str, updates: dict) -> None:
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
    new_text = update_repo_config_text(
        existing, updates, schema_keys=SCHEMA_KEYS, table_keys=TABLE_KEYS
    )
    write_repo_config_toml(new_text, path)


# ── Mutators (in-memory dict helpers) ────────────────────────────────


def apply_setting(repo_data: dict, key: str, new_value: Any) -> dict:
    """Return a new dict with repo_data[key] = new_value. Does not mutate input."""
    if key not in SCHEMA_KEYS:
        raise KeyError(f"unknown setting key: {key}")
    import copy

    out = copy.deepcopy(repo_data)
    if "." in key:
        set_dotted(out, key, new_value)
    else:
        out[key] = new_value
    return out


def remove_setting(repo_data: dict, key: str) -> dict:
    """Return a new dict with repo_data[key] removed (reverting to default)."""
    if key not in SCHEMA_KEYS:
        raise KeyError(f"unknown setting key: {key}")
    import copy

    out = copy.deepcopy(repo_data)
    if "." in key:
        parts = key.split(".")
        node = out
        for part in parts[:-1]:
            if part not in node:
                return out
            node = node[part]
        node.pop(parts[-1], None)
    else:
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


def remove_repo_key(path: Path | str, key: str) -> None:
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
    doc: Any = tomlkit.parse(existing)
    if "." in key:
        parts = key.split(".")
        node: Any = doc
        for part in parts[:-1]:
            if part not in node:
                return
            node = node[part]
        if parts[-1] in node:
            del node[parts[-1]]
            write_repo_config_toml(tomlkit.dumps(doc), path)
    elif key in doc:
        del doc[key]
        write_repo_config_toml(tomlkit.dumps(doc), path)


def load_settings_sources(_cwd: str) -> tuple[dict, dict, Path | None]:
    """Load raw repo config data.

    Used by the TUI settings screen so it can show each value's origin and
    write back only to the repo-level file.
    """
    repo_cfg = config_loader.repo_config_path()
    repo_data = config_loader.load_toml(repo_cfg)
    return repo_data, {}, None

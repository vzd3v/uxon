"""Settings schema + host-wide operator config read/write.

Single source of truth for which keys are user-editable through the TUI
superuser block, their type, and how to persist changes to
``/etc/uxon/config.toml``.

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
from uxon.infra.settings_toml import set_dotted, update_operator_config_text

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
        "default_launch_mode", "enum", "Who runs agents by default.", choices=("caller", "fixed")
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
        "Per-user/backend socket path. Placeholders: {user}, {uid}, "
        "{execution_backend}, {execution_fingerprint}.",
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
    source: str  # "default" | "operator"
    editable: bool


def settings_specs() -> tuple[SettingSpec, ...]:
    """Return the scalar settings schema."""
    return SETTINGS_SPECS


def resolve_setting_entries(
    operator_data: dict,
    defaults: dict,
) -> list[SettingEntry]:
    """Resolve settings; only a root-owned Uxon process may edit them."""
    import os

    can_edit = os.geteuid() == 0
    out: list[SettingEntry] = []
    for spec in settings_specs():
        key = spec.key
        is_dotted = "." in key
        if is_dotted:
            operator_val = _get_dotted(operator_data, key, _MISSING)
            def_val = _get_dotted(defaults, key, None)
            if operator_val is not _MISSING:
                value = operator_val
                source = "operator"
                editable = can_edit
            else:
                value = def_val
                source = "default"
                editable = can_edit
        else:
            if key in operator_data:
                value = operator_data[key]
                source = "operator"
                editable = can_edit
            else:
                value = defaults.get(key)
                source = "default"
                editable = can_edit
        out.append(SettingEntry(spec=spec, value=value, source=source, editable=editable))
    return out


_MISSING = object()  # sentinel for dotted-key lookup


# ── Persistence ──────────────────────────────────────────────────────


def write_operator_config_toml(content: str, path: Path | str) -> None:
    """Atomically install root-owned operator config when running as root."""
    import os
    import secrets

    path = Path(path)
    if os.geteuid() == 0:
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        tmp = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fchown(stream.fileno(), 0, 0)
                os.fchmod(stream.fileno(), 0o644)
                os.fsync(stream.fileno())
            os.replace(tmp, path)
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
        return

    raise PermissionError(
        "operator settings are read-only for non-root processes; "
        "render with 'uxon config render' and install with sudo"
    )


def persist_operator_config_updates(path: Path | str, updates: dict) -> None:
    """Read ``path`` (if it exists), apply ``updates`` via
    :func:`update_operator_config_text`, and write the result back.

    When the file is missing, a minimal starter is rendered: the updates
    alone are emitted with no accompanying comments.
    """
    path = Path(path)
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""
    new_text = update_operator_config_text(
        existing, updates, schema_keys=SCHEMA_KEYS, table_keys=TABLE_KEYS
    )
    write_operator_config_toml(new_text, path)


# ── Mutators (in-memory dict helpers) ────────────────────────────────


def apply_setting(operator_data: dict, key: str, new_value: Any) -> dict:
    """Return operator data with one setting replaced, without mutation."""
    if key not in SCHEMA_KEYS:
        raise KeyError(f"unknown setting key: {key}")
    import copy

    out = copy.deepcopy(operator_data)
    if "." in key:
        set_dotted(out, key, new_value)
    else:
        out[key] = new_value
    return out


def remove_setting(operator_data: dict, key: str) -> dict:
    """Return operator data without one setting, reverting to default."""
    if key not in SCHEMA_KEYS:
        raise KeyError(f"unknown setting key: {key}")
    import copy

    out = copy.deepcopy(operator_data)
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


def replace_mapping(operator_data: dict, key: str, new_mapping: dict) -> dict:
    """Return operator data with a mapping setting replaced."""
    spec_by_key = {spec.key: spec for spec in SETTINGS_SPECS}
    spec = spec_by_key.get(key)
    if spec is None or spec.kind != "table":
        raise KeyError(f"not a table setting: {key}")
    for k, v in new_mapping.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ValueError(f"table {key} requires string keys and values")
    out = dict(operator_data)
    out[key] = dict(new_mapping)
    return out


def remove_operator_key(path: Path | str, key: str) -> None:
    """Drop ``key`` from the operator config. Preserves comments
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
            write_operator_config_toml(tomlkit.dumps(doc), path)
    elif key in doc:
        del doc[key]
        write_operator_config_toml(tomlkit.dumps(doc), path)


def load_settings_source() -> dict:
    """Load raw operator config for the TUI settings screen."""
    return config_loader.load_toml(config_loader.operator_config_path())

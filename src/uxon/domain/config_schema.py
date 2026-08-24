# SPDX-License-Identifier: MIT
"""Pure structural schema for the public v4 configuration surface."""

from __future__ import annotations

import math
from typing import Any

TOP_LEVEL_KEYS = frozenset(
    {
        "default_launch_user",
        "default_launch_mode",
        "enable_all_users_list",
        "launch_user_by_caller",
        "session_users",
        "allowed_roots",
        "session_prefix",
        "legacy_session_prefixes",
        "agents",
        "launch",
        "new_project_root",
        "repeat_noninteractive_mode",
        "worktree_root",
        "worktree_base",
        "tmux_socket_template",
        "execution",
        "tui_refresh_interval_seconds",
        "tui_ssh_refresh_interval_seconds",
        "tui",
        "local_host",
        "ssh_multiplex",
        "ssh_control_persist_seconds",
        "fetch_concurrency",
        "git_create_enabled",
        "git_remote_profiles",
        "remote_hosts",
        "audit",
        "tmux",
        "runtimes",
        "launch_record_dir",
    }
)

AGENT_KEYS = frozenset({"binary", "version_args", "install_hint", "default_args", "mode"})
AGENT_MODE_KEYS = frozenset({"id", "label", "flags", "dangerous"})
LAUNCH_KEYS = frozenset({"enabled_profiles", "default_profile", "profiles", "path_rules"})
LAUNCH_PROFILE_KEYS = frozenset(
    {
        "agent",
        "display_name",
        "launch_user",
        "runtime",
        "allowed_git_remote_profiles",
        "default_git_remote_profile",
    }
)
LAUNCH_PATH_RULE_KEYS = frozenset(
    {
        "path_prefix",
        "allowed_profiles",
        "default_profile",
        "allowed_git_remote_profiles",
        "default_git_remote_profile",
    }
)
EXECUTION_KEYS = frozenset({"default_backend", "backend_by_launch_user", "backends"})
EXECUTION_BACKEND_KEYS = frozenset({"kind", "command_prefix", "probe_timeout_seconds"})
RUNTIME_KEYS = frozenset(
    {
        "kind",
        "resource_scope",
        "resource_name_template",
        "exec_prefix",
        "readiness",
        "identity",
        "session",
        "timeouts",
        "telemetry",
        "path_map",
    }
)
RUNTIME_READINESS_KEYS = frozenset(
    {
        "ready_command",
        "exists_command",
        "start_command",
        "create_command",
        "on_missing",
        "approval",
    }
)
RUNTIME_IDENTITY_KEYS = frozenset({"resolve_command"})
RUNTIME_SESSION_KEYS = frozenset({"stop_command"})
RUNTIME_TIMEOUT_KEYS = frozenset({"probe_seconds", "prepare_seconds", "stop_seconds"})
TUI_KEYS = frozenset({"table", "search", "color_palette"})
TUI_TABLE_KEYS = frozenset({"columns", "default_view"})
TUI_SEARCH_KEYS = frozenset({"fields"})
LOCAL_HOST_KEYS = frozenset({"color"})
AUDIT_KEYS = frozenset({"enabled", "syslog_facility"})
TMUX_KEYS = frozenset({"manage_options", "options", "server_options", "append_server_options"})
GIT_REMOTE_KEYS = frozenset(
    {"name", "host", "owner", "auth", "creds_user", "token_file", "visibility"}
)
REMOTE_HOST_KEYS = frozenset(
    {
        "name",
        "ssh_alias",
        "description",
        "remote_uxon",
        "interval",
        "connect_timeout",
        "total_timeout",
        "extra_ssh_options",
        "command_template",
        "color",
    }
)


class ConfigSchemaError(ValueError):
    """A public config payload is structurally invalid."""


def require_table(value: Any, *, source: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigSchemaError(f"'{source}' must be an object/table")
    return value


def reject_unknown_keys(value: Any, allowed: frozenset[str], *, source: str) -> dict[str, Any]:
    table = require_table(value, source=source)
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigSchemaError(
            f"{source}: unknown key(s) {unknown!r}; expected one of {sorted(allowed)!r}"
        )
    return table


def _require_type(value: Any, expected: type, *, source: str) -> None:
    if not isinstance(value, expected) or (expected in {int, float} and isinstance(value, bool)):
        label = {str: "string", bool: "boolean", int: "integer"}.get(expected, expected.__name__)
        raise ConfigSchemaError(f"{source} must be a {label}")


def _optional_type(raw: dict[str, Any], key: str, expected: type, *, source: str) -> None:
    if key in raw:
        _require_type(raw[key], expected, source=f"{source}.{key}")


def _number(value: Any, *, source: str) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ConfigSchemaError(f"'{source}' must be a finite number")


def _string_list(value: Any, *, source: str) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigSchemaError(f"'{source}' must be a list of strings")


def _optional_string_list(raw: dict[str, Any], key: str, *, source: str) -> None:
    if key in raw:
        _string_list(raw[key], source=f"{source}.{key}")


def _string_map(value: Any, *, source: str) -> None:
    table = require_table(value, source=source)
    if not all(isinstance(key, str) and isinstance(item, str) for key, item in table.items()):
        raise ConfigSchemaError(f"'{source}' keys and values must be strings")


def _table_list(value: Any, *, source: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ConfigSchemaError(f"'{source}' must be a list of objects/tables")
    return value


def validate_public_config(payload: Any) -> dict[str, Any]:
    """Validate public keys and JSON/TOML scalar/container types without coercion."""
    raw = reject_unknown_keys(payload, TOP_LEVEL_KEYS, source="config")
    for key in (
        "default_launch_user",
        "default_launch_mode",
        "session_prefix",
        "new_project_root",
        "repeat_noninteractive_mode",
        "worktree_root",
        "worktree_base",
        "tmux_socket_template",
        "ssh_multiplex",
        "launch_record_dir",
    ):
        _optional_type(raw, key, str, source="config")
    for key in ("enable_all_users_list", "git_create_enabled"):
        _optional_type(raw, key, bool, source="config")
    for key in ("tui_refresh_interval_seconds", "tui_ssh_refresh_interval_seconds"):
        if key in raw:
            _number(raw[key], source=f"config.{key}")
    for key in ("ssh_control_persist_seconds", "fetch_concurrency"):
        _optional_type(raw, key, int, source="config")
    for key in ("session_users", "allowed_roots", "legacy_session_prefixes"):
        _optional_string_list(raw, key, source="config")
    if "launch_user_by_caller" in raw:
        _string_map(raw["launch_user_by_caller"], source="launch_user_by_caller")

    agents = require_table(raw.get("agents", {}), source="agents")
    for agent_id, value in agents.items():
        agent = reject_unknown_keys(value, AGENT_KEYS, source=f"agents.{agent_id}")
        for key in ("binary", "install_hint"):
            _optional_type(agent, key, str, source=f"agents.{agent_id}")
        for key in ("version_args", "default_args"):
            _optional_string_list(agent, key, source=f"agents.{agent_id}")
        if "mode" in agent:
            for index, value in enumerate(
                _table_list(agent["mode"], source=f"agents.{agent_id}.mode")
            ):
                mode = reject_unknown_keys(
                    value, AGENT_MODE_KEYS, source=f"agents.{agent_id}.mode[{index}]"
                )
                for key in ("id", "label"):
                    _optional_type(mode, key, str, source=f"agents.{agent_id}.mode[{index}]")
                _optional_string_list(mode, "flags", source=f"agents.{agent_id}.mode[{index}]")
                _optional_type(mode, "dangerous", bool, source=f"agents.{agent_id}.mode[{index}]")

    launch = reject_unknown_keys(raw.get("launch", {}), LAUNCH_KEYS, source="launch")
    _optional_string_list(launch, "enabled_profiles", source="launch")
    _optional_type(launch, "default_profile", str, source="launch")
    profiles = require_table(launch.get("profiles", {}), source="launch.profiles")
    for profile_id, value in profiles.items():
        profile = reject_unknown_keys(
            value, LAUNCH_PROFILE_KEYS, source=f"launch.profiles.{profile_id}"
        )
        for key in (
            "agent",
            "display_name",
            "launch_user",
            "runtime",
            "default_git_remote_profile",
        ):
            _optional_type(profile, key, str, source=f"launch.profiles.{profile_id}")
        _optional_string_list(
            profile, "allowed_git_remote_profiles", source=f"launch.profiles.{profile_id}"
        )
    if "path_rules" in launch:
        for index, value in enumerate(
            _table_list(launch["path_rules"], source="launch.path_rules")
        ):
            rule = reject_unknown_keys(
                value, LAUNCH_PATH_RULE_KEYS, source=f"launch.path_rules[{index}]"
            )
            for key in ("path_prefix", "default_profile", "default_git_remote_profile"):
                _optional_type(rule, key, str, source=f"launch.path_rules[{index}]")
            for key in ("allowed_profiles", "allowed_git_remote_profiles"):
                _optional_string_list(rule, key, source=f"launch.path_rules[{index}]")

    execution = reject_unknown_keys(raw.get("execution", {}), EXECUTION_KEYS, source="execution")
    _optional_type(execution, "default_backend", str, source="execution")
    if "backend_by_launch_user" in execution:
        _string_map(execution["backend_by_launch_user"], source="execution.backend_by_launch_user")
    backends = require_table(execution.get("backends", {}), source="execution.backends")
    for backend_id, value in backends.items():
        backend = reject_unknown_keys(
            value, EXECUTION_BACKEND_KEYS, source=f"execution.backends.{backend_id}"
        )
        _optional_type(backend, "kind", str, source=f"execution.backends.{backend_id}")
        _optional_string_list(backend, "command_prefix", source=f"execution.backends.{backend_id}")
        if "probe_timeout_seconds" in backend:
            _number(
                backend["probe_timeout_seconds"],
                source=f"execution.backends.{backend_id}.probe_timeout_seconds",
            )

    runtimes = require_table(raw.get("runtimes", {}), source="runtimes")
    for runtime_id, value in runtimes.items():
        runtime = reject_unknown_keys(value, RUNTIME_KEYS, source=f"runtimes.{runtime_id}")
        for key in ("kind", "resource_scope", "resource_name_template", "telemetry"):
            _optional_type(runtime, key, str, source=f"runtimes.{runtime_id}")
        _optional_string_list(runtime, "exec_prefix", source=f"runtimes.{runtime_id}")
        for child, allowed in (
            ("readiness", RUNTIME_READINESS_KEYS),
            ("identity", RUNTIME_IDENTITY_KEYS),
            ("session", RUNTIME_SESSION_KEYS),
            ("timeouts", RUNTIME_TIMEOUT_KEYS),
        ):
            child_table = reject_unknown_keys(
                runtime.get(child, {}), allowed, source=f"runtimes.{runtime_id}.{child}"
            )
            for key, item in child_table.items():
                if child == "timeouts":
                    _number(item, source=f"runtimes.{runtime_id}.{child}.{key}")
                elif key.endswith("command"):
                    _string_list(item, source=f"runtimes.{runtime_id}.{child}.{key}")
                else:
                    _require_type(item, str, source=f"runtimes.{runtime_id}.{child}.{key}")
        if "path_map" in runtime:
            _string_map(runtime["path_map"], source=f"runtimes.{runtime_id}.path_map")

    for source, keys in (
        ("tui", TUI_KEYS),
        ("local_host", LOCAL_HOST_KEYS),
        ("audit", AUDIT_KEYS),
        ("tmux", TMUX_KEYS),
    ):
        reject_unknown_keys(raw.get(source, {}), keys, source=source)
    tui = require_table(raw.get("tui", {}), source="tui")
    table = reject_unknown_keys(tui.get("table", {}), TUI_TABLE_KEYS, source="tui.table")
    search = reject_unknown_keys(tui.get("search", {}), TUI_SEARCH_KEYS, source="tui.search")
    _optional_string_list(table, "columns", source="tui.table")
    _optional_type(table, "default_view", str, source="tui.table")
    _optional_string_list(search, "fields", source="tui.search")
    _optional_string_list(tui, "color_palette", source="tui")
    local_host = require_table(raw.get("local_host", {}), source="local_host")
    _optional_type(local_host, "color", str, source="local_host")
    audit = require_table(raw.get("audit", {}), source="audit")
    _optional_type(audit, "enabled", bool, source="audit")
    _optional_type(audit, "syslog_facility", str, source="audit")
    tmux = require_table(raw.get("tmux", {}), source="tmux")
    _optional_type(tmux, "manage_options", bool, source="tmux")
    for key in ("options", "server_options", "append_server_options"):
        if key in tmux:
            option_table = require_table(tmux[key], source=f"tmux.{key}")
            for option, value in option_table.items():
                if not isinstance(option, str) or not isinstance(value, (str, int, bool)):
                    raise ConfigSchemaError(
                        f"'tmux.{key}' keys must be strings and values must be strings, "
                        "integers, or booleans"
                    )

    if "git_remote_profiles" in raw:
        for index, value in enumerate(
            _table_list(raw["git_remote_profiles"], source="git_remote_profiles")
        ):
            profile = reject_unknown_keys(
                value, GIT_REMOTE_KEYS, source=f"git_remote_profiles[{index}]"
            )
            for key in GIT_REMOTE_KEYS:
                _optional_type(profile, key, str, source=f"git_remote_profiles[{index}]")
    if "remote_hosts" in raw:
        for index, value in enumerate(_table_list(raw["remote_hosts"], source="remote_hosts")):
            host = reject_unknown_keys(value, REMOTE_HOST_KEYS, source=f"remote_hosts[{index}]")
            for key in (
                "name",
                "ssh_alias",
                "description",
                "remote_uxon",
                "color",
            ):
                _optional_type(host, key, str, source=f"remote_hosts[{index}]")
            for key in ("interval", "connect_timeout", "total_timeout"):
                if key in host:
                    _number(host[key], source=f"remote_hosts[{index}].{key}")
            for key in ("extra_ssh_options", "command_template"):
                _optional_string_list(host, key, source=f"remote_hosts[{index}]")
    return raw

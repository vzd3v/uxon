# SPDX-License-Identifier: MIT
"""Strict JSON-to-TOML renderer for the installed configuration helper."""

from __future__ import annotations

import json
from typing import Any

from uxon.domain.config_schema import validate_public_config


def _string(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "[" + ", ".join(_string(item) for item in value) + "]"
    raise ValueError(f"unsupported TOML value: {value!r}")


def _multiline_strings(values: list[str]) -> list[str]:
    return ["[", *[f"  {_string(value)}," for value in values], "]"]


def _render_table(path: str, table: Any, lines: list[str], *, array: bool = False) -> None:
    if not isinstance(table, dict):
        raise ValueError(f"'{path}' must be an object")
    lines.append(f"[[{path}]]" if array else f"[{path}]")
    children: list[tuple[str, Any]] = []
    for key, value in table.items():
        if isinstance(value, dict) or (
            isinstance(value, list) and value and isinstance(value[0], dict)
        ):
            children.append((key, value))
        else:
            lines.append(f"{_string(key)} = {_value(value)}")
    lines.append("")
    for key, value in children:
        # Dynamic ids and map keys are untrusted JSON strings. Quote every
        # nested path segment so dots/brackets cannot change TOML structure;
        # quoting static segments is semantically identical TOML.
        child_path = f"{path}.{_string(key)}"
        if isinstance(value, list):
            if not all(isinstance(item, dict) for item in value):
                raise ValueError(f"'{child_path}' must be a list of objects")
            for item in value:
                _render_table(child_path, item, lines, array=True)
        else:
            _render_table(child_path, value, lines)


def render_config(payload: dict[str, Any]) -> str:
    """Render a fully validated public config payload without coercion."""
    validate_public_config(payload)
    default_launch_user = payload.get("default_launch_user", "").strip()
    default_launch_mode = payload.get("default_launch_mode", "caller").strip()
    session_prefix = payload.get("session_prefix", "uxon-").strip() or "uxon-"
    repeat_mode = payload.get("repeat_noninteractive_mode", "fail").strip().lower()
    socket_template = payload.get(
        "tmux_socket_template", "/tmp/uxon-{user}-{execution_backend}.sock"
    ).strip()
    if default_launch_mode not in {"fixed", "caller"}:
        raise ValueError("default_launch_mode must be 'fixed' or 'caller'")
    if default_launch_mode == "fixed" and not default_launch_user:
        raise ValueError("default_launch_user is required when default_launch_mode is 'fixed'")
    if repeat_mode not in {"fail", "attach", "new"}:
        raise ValueError("repeat_noninteractive_mode must be 'fail', 'attach', or 'new'")
    if not socket_template:
        raise ValueError("tmux_socket_template must not be empty")

    lines: list[str] = []
    if default_launch_user:
        lines.append(f"default_launch_user = {_string(default_launch_user)}")
    lines.extend(
        [
            f"default_launch_mode = {_string(default_launch_mode)}",
            "enable_all_users_list = "
            + ("true" if payload.get("enable_all_users_list", False) else "false"),
            f"session_prefix = {_string(session_prefix)}",
        ]
    )
    for key in ("legacy_session_prefixes", "allowed_roots", "session_users"):
        rendered = _multiline_strings(payload.get(key, []))
        lines.append(f"{key} = {rendered[0]}")
        lines.extend(rendered[1:])
    for key in ("new_project_root", "launch_record_dir"):
        value = payload.get(key, "")
        if value:
            lines.append(f"{key} = {_string(value.strip())}")
    lines.extend(
        [
            f"repeat_noninteractive_mode = {_string(repeat_mode)}",
            f"tmux_socket_template = {_string(socket_template)}",
        ]
    )
    for key in (
        "worktree_root",
        "worktree_base",
        "tui_refresh_interval_seconds",
        "tui_ssh_refresh_interval_seconds",
        "ssh_multiplex",
        "ssh_control_persist_seconds",
        "fetch_concurrency",
        "git_create_enabled",
    ):
        if key in payload:
            lines.append(f"{key} = {_value(payload[key])}")
    lines.append("")

    for table_name in (
        "agents",
        "launch",
        "execution",
        "runtimes",
        "tui",
        "local_host",
        "audit",
        "tmux",
    ):
        table = payload.get(table_name, {})
        if table:
            _render_table(table_name, table, lines)
    for table_name in ("git_remote_profiles", "remote_hosts"):
        for entry in payload.get(table_name, []):
            _render_table(table_name, entry, lines, array=True)

    mapping = payload.get("launch_user_by_caller", {})
    lines.append("[launch_user_by_caller]")
    for caller in sorted(mapping):
        lines.append(f"{_string(caller)} = {_string(mapping[caller])}")
    lines.append("")
    return "\n".join(lines)

#!/usr/bin/env python3
"""Render repo-local uxon config.toml from a single JSON payload."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

TOP_LEVEL_KEYS = {
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
}


def reject_unknown_keys(value: Any, allowed: set[str], *, source: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"'{source}' must be an object")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(
            f"{source}: unknown key(s) {unknown!r}; expected one of {sorted(allowed)!r}"
        )
    return value


def validate_payload_schema(payload: dict[str, Any]) -> None:
    reject_unknown_keys(payload, TOP_LEVEL_KEYS, source="config")
    agents_value = payload.get("agents", {})
    if not isinstance(agents_value, dict):
        raise ValueError("'agents' must be an object")
    agents = agents_value
    for agent_id, raw in agents.items():
        agent = reject_unknown_keys(
            raw,
            {"binary", "version_args", "install_hint", "default_args", "mode"},
            source=f"agents.{agent_id}",
        )
        for index, mode in enumerate(agent.get("mode", [])):
            reject_unknown_keys(
                mode,
                {"id", "label", "flags", "dangerous"},
                source=f"agents.{agent_id}.mode[{index}]",
            )

    launch = reject_unknown_keys(
        payload.get("launch", {}),
        {"enabled_profiles", "default_profile", "profiles", "path_rules"},
        source="launch",
    )
    profiles = launch.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ValueError("'launch.profiles' must be an object")
    for profile_id, raw in profiles.items():
        reject_unknown_keys(
            raw,
            {
                "agent",
                "display_name",
                "launch_user",
                "runtime",
                "allowed_git_remote_profiles",
                "default_git_remote_profile",
            },
            source=f"launch.profiles.{profile_id}",
        )
    for index, raw in enumerate(launch.get("path_rules", [])):
        reject_unknown_keys(
            raw,
            {
                "path_prefix",
                "allowed_profiles",
                "default_profile",
                "allowed_git_remote_profiles",
                "default_git_remote_profile",
            },
            source=f"launch.path_rules[{index}]",
        )

    execution = reject_unknown_keys(
        payload.get("execution", {}),
        {"default_backend", "state_dir", "backend_by_launch_user", "backends"},
        source="execution",
    )
    backends = execution.get("backends", {})
    if not isinstance(backends, dict):
        raise ValueError("'execution.backends' must be an object")
    for backend_id, raw in backends.items():
        reject_unknown_keys(
            raw,
            {
                "kind",
                "command_prefix",
                "probe_timeout_seconds",
            },
            source=f"execution.backends.{backend_id}",
        )

    runtimes = payload.get("runtimes", {})
    if not isinstance(runtimes, dict):
        raise ValueError("'runtimes' must be an object")
    for runtime_id, raw_value in runtimes.items():
        raw = reject_unknown_keys(
            raw_value,
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
            },
            source=f"runtimes.{runtime_id}",
        )
        for child, allowed in (
            (
                "readiness",
                {
                    "ready_command",
                    "exists_command",
                    "start_command",
                    "create_command",
                    "on_missing",
                    "approval",
                },
            ),
            ("identity", {"resolve_command"}),
            ("session", {"stop_command"}),
            ("timeouts", {"probe_seconds", "prepare_seconds", "stop_seconds"}),
        ):
            reject_unknown_keys(
                raw.get(child, {}), allowed, source=f"runtimes.{runtime_id}.{child}"
            )

    for source, allowed in (
        ("tui", {"table", "search", "color_palette"}),
        ("local_host", {"color"}),
        ("audit", {"enabled", "syslog_facility"}),
        ("tmux", {"manage_options", "options", "server_options", "append_server_options"}),
    ):
        reject_unknown_keys(payload.get(source, {}), allowed, source=source)
    tui = payload.get("tui", {})
    reject_unknown_keys(tui.get("table", {}), {"columns", "default_view"}, source="tui.table")
    reject_unknown_keys(tui.get("search", {}), {"fields"}, source="tui.search")

    git_keys = {"name", "host", "owner", "auth", "creds_user", "token_file", "visibility"}
    for index, raw in enumerate(payload.get("git_remote_profiles", [])):
        reject_unknown_keys(raw, git_keys, source=f"git_remote_profiles[{index}]")
    remote_keys = {
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
    for index, raw in enumerate(payload.get("remote_hosts", [])):
        reject_unknown_keys(raw, remote_keys, source=f"remote_hosts[{index}]")


def fail(msg: str) -> int:
    print(f"render_uxon_config.py: {msg}", file=sys.stderr)
    return 2


def read_json(path: str) -> dict[str, Any]:
    if path == "-":
        data = json.load(sys.stdin)
    else:
        with Path(path).open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("config payload must be a JSON object")
    return data


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def toml_bool(value: bool) -> str:
    return "true" if value else "false"


def toml_string_list(values: list[str]) -> list[str]:
    lines = ["["]
    for value in values:
        lines.append(f"  {toml_string(str(value))},")
    lines.append("]")
    return lines


def normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("expected a list")
    return [str(item) for item in value]


def normalize_mapping(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    out: dict[str, str] = {}
    for key, item in value.items():
        out[str(key)] = str(item)
    return out


def normalize_repeat_mode(value: Any) -> str:
    mode = str(value if value is not None else "fail").strip().lower()
    if mode not in {"fail", "attach", "new"}:
        raise ValueError("repeat_noninteractive_mode must be 'fail', 'attach', or 'new'")
    return mode


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return toml_bool(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return toml_string(value)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "[" + ", ".join(toml_string(item) for item in value) + "]"
    raise ValueError(f"unsupported TOML value: {value!r}")


def render_table(path: str, table: Any, lines: list[str], *, array: bool = False) -> None:
    """Render a JSON object as a TOML table without shell-string coercion."""
    if not isinstance(table, dict):
        raise ValueError(f"'{path}' must be an object")
    lines.append(f"[[{path}]]" if array else f"[{path}]")
    children: list[tuple[str, Any]] = []
    for key, value in table.items():
        if isinstance(value, dict) or (
            isinstance(value, list) and value and isinstance(value[0], dict)
        ):
            children.append((str(key), value))
        else:
            lines.append(f"{toml_string(str(key))} = {toml_value(value)}")
    lines.append("")
    for key, value in children:
        child_path = f"{path}.{key}"
        if isinstance(value, list):
            if not all(isinstance(item, dict) for item in value):
                raise ValueError(f"'{child_path}' must be a list of objects")
            for item in value:
                render_table(child_path, item, lines, array=True)
        else:
            render_table(child_path, value, lines)


def render_config(payload: dict[str, Any]) -> str:
    validate_payload_schema(payload)
    default_launch_user = str(payload.get("default_launch_user", "")).strip()
    default_launch_mode = str(payload.get("default_launch_mode", "caller")).strip()
    enable_all_users_list = bool(payload.get("enable_all_users_list", False))
    session_prefix = str(payload.get("session_prefix", "uxon-")).strip() or "uxon-"
    legacy_session_prefixes = normalize_string_list(payload.get("legacy_session_prefixes", []))
    allowed_roots = normalize_string_list(payload.get("allowed_roots", []))
    session_users = normalize_string_list(payload.get("session_users", []))
    launch_user_by_caller = normalize_mapping(payload.get("launch_user_by_caller", {}))
    new_project_root = str(payload.get("new_project_root", "")).strip()
    repeat_noninteractive_mode = normalize_repeat_mode(
        payload.get("repeat_noninteractive_mode", "fail")
    )
    tmux_socket_template = str(
        payload.get("tmux_socket_template", "/tmp/uxon-{user}-{execution_backend}.sock")
    ).strip()

    agents_payload = payload.get("agents", {})
    if not isinstance(agents_payload, dict):
        raise ValueError("'agents' must be an object")
    launch_payload = payload.get("launch", {})
    execution_payload = payload.get("execution", {})
    runtimes_payload = payload.get("runtimes", {})
    for key, value in (
        ("launch", launch_payload),
        ("execution", execution_payload),
        ("runtimes", runtimes_payload),
    ):
        if not isinstance(value, dict):
            raise ValueError(f"'{key}' must be an object")

    if default_launch_mode not in {"fixed", "caller"}:
        raise ValueError("default_launch_mode must be 'fixed' or 'caller'")
    if default_launch_mode == "fixed" and not default_launch_user:
        raise ValueError("default_launch_user is required when default_launch_mode is 'fixed'")
    if not tmux_socket_template:
        raise ValueError("tmux_socket_template must not be empty")

    lines: list[str] = []
    if default_launch_user:
        lines.append(f"default_launch_user = {toml_string(default_launch_user)}")
    lines.append(f"default_launch_mode = {toml_string(default_launch_mode)}")
    lines.append(f"enable_all_users_list = {toml_bool(enable_all_users_list)}")
    lines.append(f"session_prefix = {toml_string(session_prefix)}")
    lines.append("legacy_session_prefixes = " + toml_string_list(legacy_session_prefixes)[0])
    lines.extend(toml_string_list(legacy_session_prefixes)[1:])
    lines.append("allowed_roots = " + toml_string_list(allowed_roots)[0])
    lines.extend(toml_string_list(allowed_roots)[1:])
    lines.append("session_users = " + toml_string_list(session_users)[0])
    lines.extend(toml_string_list(session_users)[1:])
    if new_project_root:
        lines.append(f"new_project_root = {toml_string(new_project_root)}")
    lines.append(f"repeat_noninteractive_mode = {toml_string(repeat_noninteractive_mode)}")
    lines.append(f"tmux_socket_template = {toml_string(tmux_socket_template)}")
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
            lines.append(f"{key} = {toml_value(payload[key])}")
    lines.append("")
    for table_name, table in (
        ("agents", agents_payload),
        ("launch", launch_payload),
        ("execution", execution_payload),
        ("runtimes", runtimes_payload),
        ("tui", payload.get("tui", {})),
        ("local_host", payload.get("local_host", {})),
        ("audit", payload.get("audit", {})),
        ("tmux", payload.get("tmux", {})),
    ):
        if table:
            render_table(table_name, table, lines)
    for table_name in ("git_remote_profiles", "remote_hosts"):
        entries = payload.get(table_name, [])
        if not isinstance(entries, list):
            raise ValueError(f"'{table_name}' must be a list of objects")
        for entry in entries:
            render_table(table_name, entry, lines, array=True)
    lines.append("[launch_user_by_caller]")
    for caller in sorted(launch_user_by_caller):
        lines.append(f"{toml_string(caller)} = {toml_string(launch_user_by_caller[caller])}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-json", required=True, help="JSON payload path or '-' for stdin")
    parser.add_argument("--output", default="-", help="Output path or '-' for stdout")
    args = parser.parse_args(argv)

    try:
        payload = read_json(args.config_json)
        rendered = render_config(payload)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return fail(str(exc))

    if args.output == "-":
        sys.stdout.write(rendered)
    else:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

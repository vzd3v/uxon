#!/usr/bin/env python3
"""Render repo-local uxon config.toml from a single JSON payload."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


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
    lines.append("")
    for table_name, table in (
        ("agents", agents_payload),
        ("launch", launch_payload),
        ("execution", execution_payload),
        ("runtimes", runtimes_payload),
    ):
        if table:
            render_table(table_name, table, lines)
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

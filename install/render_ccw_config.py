#!/usr/bin/env python3
"""Render repo-local ccw config.toml from a single JSON payload."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def fail(msg: str) -> int:
    print(f"render_ccw_config.py: {msg}", file=sys.stderr)
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


def render_config(payload: dict[str, Any]) -> str:
    runtime_user = str(payload.get("runtime_user", "")).strip()
    default_launch_mode = str(payload.get("default_launch_mode", "caller")).strip()
    enable_all_users_list = bool(payload.get("enable_all_users_list", False))
    session_prefix = str(payload.get("session_prefix", "cc-")).strip() or "cc-"
    allowed_roots = normalize_string_list(payload.get("allowed_roots", []))
    session_users = normalize_string_list(payload.get("session_users", []))
    default_claude_args = normalize_string_list(payload.get("default_claude_args", []))
    launch_user_by_caller = normalize_mapping(payload.get("launch_user_by_caller", {}))
    new_project_root = str(payload.get("new_project_root", "")).strip()

    if default_launch_mode not in {"fixed", "caller"}:
        raise ValueError("default_launch_mode must be 'fixed' or 'caller'")
    if default_launch_mode == "fixed" and not runtime_user:
        raise ValueError("runtime_user is required when default_launch_mode is 'fixed'")

    lines: list[str] = []
    if runtime_user:
        lines.append(f"runtime_user = {toml_string(runtime_user)}")
    lines.append(f"default_launch_mode = {toml_string(default_launch_mode)}")
    lines.append(f"enable_all_users_list = {toml_bool(enable_all_users_list)}")
    lines.append(f"session_prefix = {toml_string(session_prefix)}")
    lines.append("allowed_roots = " + toml_string_list(allowed_roots)[0])
    lines.extend(toml_string_list(allowed_roots)[1:])
    lines.append("session_users = " + toml_string_list(session_users)[0])
    lines.extend(toml_string_list(session_users)[1:])
    if new_project_root:
        lines.append(f"new_project_root = {toml_string(new_project_root)}")
    lines.append("")
    lines.append("default_claude_args = " + toml_string_list(default_claude_args)[0])
    lines.extend(toml_string_list(default_claude_args)[1:])
    lines.append("")
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

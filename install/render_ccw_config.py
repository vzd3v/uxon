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


def normalize_repeat_mode(value: Any) -> str:
    mode = str(value if value is not None else "fail").strip().lower()
    if mode not in {"fail", "attach", "new"}:
        raise ValueError("repeat_noninteractive_mode must be 'fail', 'attach', or 'new'")
    return mode


def normalize_agent_args(value: Any) -> list[str]:
    """Accept a list of strings (agent default_args); ignore missing keys."""
    if value is None:
        return []
    return normalize_string_list(value)


VALID_AGENT_IDS = ("claude", "codex", "cursor")


def render_config(payload: dict[str, Any]) -> str:
    runtime_user = str(payload.get("runtime_user", "")).strip()
    default_launch_mode = str(payload.get("default_launch_mode", "caller")).strip()
    enable_all_users_list = bool(payload.get("enable_all_users_list", False))
    session_prefix = str(payload.get("session_prefix", "ccw-")).strip() or "ccw-"
    allowed_roots = normalize_string_list(payload.get("allowed_roots", []))
    session_users = normalize_string_list(payload.get("session_users", []))
    launch_user_by_caller = normalize_mapping(payload.get("launch_user_by_caller", {}))
    new_project_root = str(payload.get("new_project_root", "")).strip()
    repeat_noninteractive_mode = normalize_repeat_mode(payload.get("repeat_noninteractive_mode", "fail"))
    tmux_socket_template = str(payload.get("tmux_socket_template", "/tmp/ccw-{user}.sock")).strip()

    # Agent configuration (replaces legacy default_claude_args).
    agents_payload = payload.get("agents", {})
    if not isinstance(agents_payload, dict):
        raise ValueError("'agents' must be an object")
    agents_enabled: list[str] = normalize_string_list(
        agents_payload.get("enabled", ["claude"])
    )
    if not agents_enabled:
        raise ValueError("agents.enabled must not be empty")
    for aid in agents_enabled:
        if aid not in VALID_AGENT_IDS:
            raise ValueError(f"unknown agent id in agents.enabled: {aid!r}")
    agents_default: str = str(agents_payload.get("default", agents_enabled[0])).strip()
    if agents_default not in agents_enabled:
        raise ValueError(f"agents.default={agents_default!r} is not in agents.enabled={agents_enabled}")
    per_agent_args: dict[str, list[str]] = {}
    for aid in VALID_AGENT_IDS:
        sub = agents_payload.get(aid, {})
        if not isinstance(sub, dict):
            raise ValueError(f"'agents.{aid}' must be an object")
        per_agent_args[aid] = normalize_agent_args(sub.get("default_args"))

    if default_launch_mode not in {"fixed", "caller"}:
        raise ValueError("default_launch_mode must be 'fixed' or 'caller'")
    if default_launch_mode == "fixed" and not runtime_user:
        raise ValueError("runtime_user is required when default_launch_mode is 'fixed'")
    if not tmux_socket_template:
        raise ValueError("tmux_socket_template must not be empty")

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
    lines.append(f"repeat_noninteractive_mode = {toml_string(repeat_noninteractive_mode)}")
    lines.append(f"tmux_socket_template = {toml_string(tmux_socket_template)}")
    lines.append("")
    # Nested [agents] tables.
    lines.append("[agents]")
    lines.append("enabled = " + toml_string_list(agents_enabled)[0])
    lines.extend(toml_string_list(agents_enabled)[1:])
    lines.append(f"default = {toml_string(agents_default)}")
    lines.append("")
    for aid in VALID_AGENT_IDS:
        lines.append(f"[agents.{aid}]")
        lines.append("default_args = " + toml_string_list(per_agent_args[aid])[0])
        lines.extend(toml_string_list(per_agent_args[aid])[1:])
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

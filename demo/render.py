#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Render a uxon demo scenario (YAML) into wire-envelope JSON files.

One ``<host>.json`` per host in the scenario. Each file is a
schema-1 :class:`uxon.wire_schema.Envelope` of ``kind="list"`` —
the same shape the SSH collector parses in production.

YAML schema (see demo/scenarios/*.yaml for working examples):

    schema_version: 1
    defaults:                       # optional, merged into every session
      windows: 1
      cpu_pct: 0.0
      rss_mib: 220
      agent: claude
    local:                          # optional; sessions on the machine running uxon.
      - user: wes-agent           # surfaced as host=local in the TUI.
        slot: crm-billing
        agent: claude
        attached: true
    hosts:                          # remote peers fetched over SSH in production.
      - name: dev-wes             # ASCII; becomes the filename and HOST column.
        color: green                # optional Rich color for that host's block.
        host_stats:                 # optional; renders the status bar with metrics.
          cpu_pct: 18.0
          mem_used_kib: 8200000
          mem_total_kib: 16000000
          loadavg_1m: 1.2
          uptime_s: 124000
          kernel: "6.8.0-90-generic"
        sessions:
          - user: wes-agent        # launch user — the low-priv agent account, not the human.
            slot: crm-billing        # stem; session name becomes "uxon-crm-billing@claude"
            agent: claude            # claude | codex | cursor — rendered in its own AGENT column
            attached: true
            windows: 3
            cpu_pct: 12.4
            rss_mib: 540
            cmd: claude              # active-pane command name; defaults to agent
            path: /home/wes-agent/work/crm-billing
            age_min: 25              # session created N minutes ago
            attached_min_ago: 0      # last_attached N min ago; omit / null for "never"
            pid: 100123              # optional; otherwise a stable synthetic PID

Usage:  render.py <scenario.yaml> <output_dir>

The output directory is overwritten — caller is expected to nuke it
first (the demo entry-point does this).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

WIRE_SCHEMA_VERSION = "1"  # mirrors uxon.wire_schema; demo stays decoupled
SESSION_PREFIX = "uxon-"

VALID_AGENTS = {"claude", "codex", "cursor"}


def _now() -> datetime:
    # Frozen-clock-friendly seam: tests / reviewers can monkeypatch this.
    return datetime.now(tz=UTC)


def _iso(dt: datetime) -> str:
    # ISO 8601 with timezone; matches uxon.cli ISO emission.
    return dt.isoformat(timespec="seconds")


def _stable_pid(host_name: str, user: str, slot: str) -> int:
    """Deterministic synthetic PID so rendered files diff cleanly.

    Built-in ``hash()`` is randomised per-process by default
    (``PYTHONHASHSEED``); using it here would make every render produce
    a different PID and pollute the git diff. ``sha256`` is overkill
    cryptographically but cheap and reproducible.
    """
    digest = hashlib.sha256(f"{host_name}\x00{user}\x00{slot}".encode()).digest()
    h = int.from_bytes(digest[:4], "big")
    return 10000 + (h % 89999)


def _format_rss_kib(mib: float) -> int:
    return int(mib * 1024)


def _build_session_record(
    host_name: str,
    user: str,
    spec: dict[str, Any],
    defaults: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    merged: dict[str, Any] = {**defaults, **spec}

    slot = merged.get("slot")
    if not slot:
        raise ValueError(f"session for user={user!r} on host={host_name!r} missing 'slot'")
    agent = str(merged.get("agent", "claude"))
    if agent not in VALID_AGENTS:
        raise ValueError(
            f"unknown agent {agent!r} for user={user!r} slot={slot!r}; "
            f"expected one of {sorted(VALID_AGENTS)}"
        )

    # Real uxon session names follow `<prefix><stem>@<agent>[-N]` —
    # see :func:`uxon.cli.parse_session_name`. The dashboard's NAME
    # column strips the `@<agent>` suffix (the AGENT column carries
    # it). Anything else here makes the NAME column display garbage.
    short_id = f"{slot}@{agent}"
    name = f"{SESSION_PREFIX}{short_id}"

    age_min = float(merged.get("age_min", 30))
    created_dt = now - timedelta(minutes=age_min)

    attached = bool(merged.get("attached", False))
    raw_attached_min = merged.get("attached_min_ago")
    if attached:
        # An attached session's last_attached is by definition "now"
        # (the consumer treats them interchangeably).
        last_attached_dt: datetime | None = now
    elif raw_attached_min is None:
        last_attached_dt = None
    else:
        last_attached_dt = now - timedelta(minutes=float(raw_attached_min))

    pid = int(merged.get("pid") or _stable_pid(host_name, user, slot))
    cpu_pct = float(merged.get("cpu_pct", 0.0))
    rss_kib = _format_rss_kib(float(merged.get("rss_mib", 200)))
    windows = str(int(merged.get("windows", 1)))
    cmd = str(merged.get("cmd") or agent)
    path = str(merged.get("path", f"/home/{user}"))

    return {
        "user": user,
        "name": name,
        "short_id": short_id,
        "agent": agent,
        "attached": attached,
        "windows": windows,
        "created": _iso(created_dt),
        "last_attached": _iso(last_attached_dt) if last_attached_dt is not None else "",
        "pane_pids": [pid],
        "active_pid": pid,
        "active_cmd": cmd,
        "active_path": path,
        "cpu_pct": cpu_pct,
        "rss_kib": rss_kib,
        "legacy": False,
    }


def _build_local_envelope(
    sessions_in: list[Any], defaults: dict[str, Any], now: datetime
) -> dict[str, Any]:
    # ``_local.json`` mirrors a remote envelope but is consumed by
    # ``uxon._demo.load_demo_local_sessions`` instead of the SSH path —
    # it backs the "local" rows in the TUI, the machine running uxon
    # itself.
    records: list[dict[str, Any]] = []
    users_seen: set[str] = set()
    for spec in sessions_in:
        if not isinstance(spec, dict):
            raise ValueError(f"local session entry must be a mapping, got {spec!r}")
        user = spec.get("user")
        if not user or not isinstance(user, str):
            raise ValueError(f"local session missing 'user': {spec!r}")
        users_seen.add(user)
        records.append(_build_session_record("local", user, spec, defaults, now))
    return {
        "schema_version": WIRE_SCHEMA_VERSION,
        "uxon_version": "demo",
        "kind": "list",
        "data": {
            "sessions": records,
            "scope_users": sorted(users_seen),
            "scope_skipped": [],
        },
    }


def _build_envelope(
    host: dict[str, Any], defaults: dict[str, Any], now: datetime, order: int
) -> dict[str, Any]:
    host_name = host.get("name")
    if not host_name or not isinstance(host_name, str):
        raise ValueError(f"host missing 'name': {host!r}")

    sessions_in = host.get("sessions", []) or []
    if not isinstance(sessions_in, list):
        raise ValueError(f"host {host_name!r}: 'sessions' must be a list")

    records: list[dict[str, Any]] = []
    users_seen: set[str] = set()
    for spec in sessions_in:
        if not isinstance(spec, dict):
            raise ValueError(f"host {host_name!r}: session entry must be a mapping, got {spec!r}")
        user = spec.get("user")
        if not user or not isinstance(user, str):
            raise ValueError(f"host {host_name!r}: session missing 'user': {spec!r}")
        users_seen.add(user)
        records.append(_build_session_record(host_name, user, spec, defaults, now))

    envelope: dict[str, Any] = {
        "schema_version": WIRE_SCHEMA_VERSION,
        "uxon_version": "demo",
        "kind": "list",
        "data": {
            "sessions": records,
            "scope_users": sorted(users_seen),
            "scope_skipped": list(host.get("scope_skipped", []) or []),
        },
    }

    host_stats = host.get("host_stats")
    if isinstance(host_stats, dict) and host_stats:
        envelope["host_stats"] = dict(host_stats)

    color = host.get("color")
    if isinstance(color, str) and color:
        # Additive demo-only field consumed by uxon._demo.synthesize_remote_hosts.
        envelope["demo_color"] = color

    # Position in the YAML ``hosts:`` list — preserves authoring order in the
    # TUI instead of letting alphabetic file sort dictate presentation.
    envelope["demo_order"] = order

    return envelope


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("scenario", type=Path, help="Path to a scenario YAML file")
    parser.add_argument("output_dir", type=Path, help="Directory to write <host>.json files into")
    args = parser.parse_args(argv)

    scenario_text = args.scenario.read_text(encoding="utf-8")
    scenario = yaml.safe_load(scenario_text)
    if not isinstance(scenario, dict):
        raise SystemExit(f"{args.scenario}: top-level must be a mapping")

    schema = scenario.get("schema_version", 1)
    if schema != 1:
        raise SystemExit(f"{args.scenario}: unsupported scenario schema_version={schema!r}")

    defaults = scenario.get("defaults", {}) or {}
    if not isinstance(defaults, dict):
        raise SystemExit(f"{args.scenario}: 'defaults' must be a mapping")

    hosts = scenario.get("hosts", []) or []
    if not isinstance(hosts, list):
        raise SystemExit(f"{args.scenario}: 'hosts' must be a list (may be empty)")

    local_sessions = scenario.get("local", []) or []
    if not isinstance(local_sessions, list):
        raise SystemExit(f"{args.scenario}: 'local' must be a list of session specs")

    if not hosts and not local_sessions:
        raise SystemExit(f"{args.scenario}: need at least one of 'hosts' or 'local'")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    now = _now()

    written: list[str] = []
    for idx, host in enumerate(hosts):
        if not isinstance(host, dict):
            raise SystemExit(f"{args.scenario}: each host must be a mapping, got {host!r}")
        env = _build_envelope(host, defaults, now, order=idx)
        path = args.output_dir / f"{host['name']}.json"
        path.write_text(json.dumps(env, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        written.append(str(path))

    if local_sessions:
        env = _build_local_envelope(local_sessions, defaults, now)
        path = args.output_dir / "_local.json"
        path.write_text(json.dumps(env, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        written.append(str(path))

    for p in written:
        print(p, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

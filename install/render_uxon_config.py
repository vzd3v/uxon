#!/usr/bin/env python3
"""Render the host-wide Uxon operator config from one strict JSON payload."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from uxon.infra.config_renderer import render_config


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-json", required=True, help="JSON payload path or '-' for stdin")
    parser.add_argument("--output", default="-", help="Output path or '-' for stdout")
    args = parser.parse_args(argv)
    try:
        if args.config_json == "-":
            payload = json.load(sys.stdin)
        else:
            with Path(args.config_json).open("r", encoding="utf-8") as stream:
                payload = json.load(stream)
        if not isinstance(payload, dict):
            raise ValueError("config payload must be a JSON object")
        rendered = render_config(payload)
        if args.output == "-":
            sys.stdout.write(rendered)
        else:
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(rendered, encoding="utf-8")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"render_uxon_config.py: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

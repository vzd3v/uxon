#!/usr/bin/env python3
"""Render the host-wide Uxon operator config from one strict JSON payload."""

from __future__ import annotations

import sys

from uxon.cli.main import main as uxon_main


def main(argv: list[str]) -> int:
    return uxon_main(["config", "render", *argv])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

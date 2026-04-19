#!/usr/bin/env python3
"""T0c prototype: confirm textual CSS variable names we reference in plan."""
from __future__ import annotations

import sys


def main() -> int:
    try:
        from textual.design import ColorSystem
    except Exception as exc:
        print(f"FAIL import textual.design.ColorSystem: {exc}")
        return 1

    # ColorSystem.generate() returns a dict of token_name -> Color (or str)
    cs = ColorSystem("#004578", "#0178D4")
    variables = cs.generate()
    names = sorted(variables.keys())
    print(f"Total variables: {len(names)}")
    # variables we reference in plan:
    wanted = ["error", "warning", "text-muted", "primary", "secondary", "accent",
              "success", "surface", "background", "boost", "panel"]
    for name in wanted:
        status = "OK" if name in variables else "MISSING"
        print(f"  {name}: {status}")

    # Confirm the exact subset used in plan's $ccw-* tokens
    needed = {"error", "warning", "text-muted"}
    missing = needed - set(variables.keys())
    if missing:
        print(f"FAIL missing variables: {missing}")
        return 1
    print("OK proto_css_vars: all required textual CSS variables present")
    print("Full name list (first 40):", names[:40])
    return 0


if __name__ == "__main__":
    sys.exit(main())

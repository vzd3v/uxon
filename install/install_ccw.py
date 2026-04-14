#!/usr/bin/env python3
"""Install the ccw entrypoint as a symlink from the canonical checkout."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def fail(msg: str) -> int:
    print(f"install_ccw.py: {msg}", file=sys.stderr)
    return 2


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-dir", required=True)
    parser.add_argument("--install-path", required=True)
    args = parser.parse_args(argv)

    repo_dir = Path(args.repo_dir).resolve()
    source = repo_dir / "bin" / "ccw"
    install_path = Path(args.install_path)

    if not source.exists():
        return fail(f"missing source executable: {source}")

    install_path.parent.mkdir(parents=True, exist_ok=True)
    if install_path.is_symlink() or install_path.exists():
        install_path.unlink()
    os.symlink(source, install_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

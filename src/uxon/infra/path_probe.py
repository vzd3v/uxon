# SPDX-License-Identifier: MIT
"""Canonicalize one launch path inside an execution backend."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def inspect(path: str) -> dict[str, object]:
    """Return target-filesystem facts without following a shell protocol."""
    target = _absolute(path)
    exists = target.exists()
    directory = target.is_dir()
    cursor = target
    while not cursor.exists():
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    nearest = str(cursor.resolve(strict=True)) if cursor.exists() else ""
    canonical = canonical_existing(path) if exists else canonical_intended(path)
    return {
        "ok": True,
        "path": canonical,
        "exists": exists,
        "directory": directory,
        "writable": directory and os.access(target, os.W_OK | os.X_OK),
        "nearest_existing_ancestor": nearest,
        "error": "",
    }


def list_directories(path: str) -> dict[str, object]:
    """List direct child directories without following child symlinks."""
    target = _absolute(path)
    if not target.exists():
        return {"ok": True, "entries": [], "error": ""}
    if not target.is_dir():
        raise ValueError(f"not a directory: {target}")
    entries: list[dict[str, object]] = []
    with os.scandir(target) as iterator:
        for entry in iterator:
            if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
                continue
            entries.append(
                {
                    "name": entry.name,
                    "mtime": int(entry.stat(follow_symlinks=False).st_mtime),
                }
            )
    entries.sort(key=lambda item: str(item["name"]))
    return {"ok": True, "entries": entries, "error": ""}


def _absolute(path: str) -> Path:
    target = Path(path)
    if not target.is_absolute() or any(part in {"", ".", ".."} for part in target.parts):
        raise ValueError("launch path must be absolute and normalized")
    if "\0" in path:
        raise ValueError("launch path contains a NUL byte")
    return target


def canonical_existing(path: str) -> str:
    target = _absolute(path)
    return str(target.resolve(strict=False))


def canonical_intended(path: str) -> str:
    target = _absolute(path)
    cursor = Path(target.anchor)
    for part in target.parts[1:]:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError(f"refusing to create launch target through symlink: {cursor}")
        if not os.path.lexists(cursor):
            break
    if target.exists():
        return str(target.resolve(strict=True))
    missing: list[str] = []
    cursor = target
    while not cursor.exists():
        if cursor.is_symlink():
            raise ValueError(f"refusing to create launch target through symlink: {cursor}")
        missing.append(cursor.name)
        parent = cursor.parent
        if parent == cursor:
            raise ValueError(f"no existing parent for launch target: {target}")
        cursor = parent
    base = cursor.resolve(strict=True)
    return str(base.joinpath(*reversed(missing)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="uxon path-probe")
    parser.add_argument(
        "--mode", choices=("existing", "intended", "inspect", "list-directories"), required=True
    )
    parser.add_argument("--path", required=True)
    ns = parser.parse_args(argv)
    try:
        if ns.mode == "inspect":
            payload = inspect(ns.path)
        elif ns.mode == "list-directories":
            payload = list_directories(ns.path)
        else:
            canonical = (
                canonical_existing(ns.path)
                if ns.mode == "existing"
                else canonical_intended(ns.path)
            )
            payload = {"ok": True, "path": canonical, "error": ""}
    except (OSError, ValueError) as exc:
        if ns.mode == "inspect":
            payload = {
                "ok": False,
                "path": "",
                "exists": False,
                "directory": False,
                "writable": False,
                "nearest_existing_ancestor": "",
                "error": str(exc),
            }
        elif ns.mode == "list-directories":
            payload = {"ok": False, "entries": [], "error": str(exc)}
        else:
            payload = {"ok": False, "path": "", "error": str(exc)}
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

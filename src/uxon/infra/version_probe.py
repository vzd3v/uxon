# SPDX-License-Identifier: MIT
"""Impure version readers: package version + git commit/dirty state.

These shell out to ``git`` and read the filesystem to gather the raw
inputs for the version string. The pure string builder lives in
:func:`uxon.domain.version.format_version`; ``cli`` / ``app`` compose
these readers with it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from uxon.domain.version import format_version as _format_version_str


def repo_root() -> Path:
    """Best-effort path to the repo root for in-tree dev runs.

    For pipx / `uv tool` / wheel installs this points into site-packages
    and the resulting paths (``config/config.toml`` etc.) won't exist —
    callers must tolerate missing files.
    """
    return Path(__file__).resolve().parents[3]


def read_repo_version() -> str:
    # Single source of truth: ``__version__`` in ``src/uxon/__init__.py``.
    # Hatch reads the same string at build time, so wheels and dev
    # checkouts always agree.
    try:
        from uxon import __version__ as pkg_version
    except ImportError:
        pkg_version = ""
    return pkg_version or "0.0.0+unknown"


def read_git_commit_short() -> str | None:
    root = str(repo_root())
    cp = subprocess.run(
        ["git", "-c", f"safe.directory={root}", "-C", root, "rev-parse", "--short", "HEAD"],
        text=True,
        capture_output=True,
    )
    if cp.returncode != 0:
        return None
    commit = (cp.stdout or "").strip()
    return commit or None


def repo_is_dirty() -> bool:
    root = str(repo_root())
    refresh = subprocess.run(
        ["git", "-c", f"safe.directory={root}", "-C", root, "update-index", "-q", "--refresh"],
        text=True,
        capture_output=True,
    )
    if refresh.returncode != 0:
        return False
    cp = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={root}",
            "-C",
            root,
            "status",
            "--porcelain",
            "--untracked-files=no",
        ],
        text=True,
        capture_output=True,
    )
    if cp.returncode != 0:
        return False
    return bool((cp.stdout or "").strip())


def format_version() -> str:
    """Compose the impure version readers with the pure string builder.

    The display-string construction is owned by
    :func:`uxon.domain.version.format_version`; this gathers ``version`` /
    ``commit`` / ``dirty`` from the impure git/FS readers above. Callers
    in ``cli`` / ``app`` delegate here instead of re-composing.
    """
    version = read_repo_version()
    commit = read_git_commit_short()
    dirty = repo_is_dirty() if commit else False
    return _format_version_str(version, commit, dirty)


def _version_data() -> dict[str, Any]:
    """Build the ``data`` body for ``uxon version --json``.

    Mirrors the version display string: the package version, the short
    git commit (when running from a checkout), and the dirty bit.
    Fields use ``null`` rather than ``"-"`` so consumers see a clear
    "not available" signal instead of a placeholder string.
    """
    commit = read_git_commit_short()
    return {
        "uxon_version": read_repo_version(),
        "commit": commit,
        "commit_dirty": repo_is_dirty() if commit else False,
    }

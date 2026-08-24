# SPDX-License-Identifier: MIT
"""Impure version readers: package version + git commit/dirty state.

These shell out to ``git`` and read the filesystem to gather the raw
inputs for the version string. The pure string builder lives in
:func:`uxon.domain.version.format_version`; ``cli`` / ``app`` compose
these readers with it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from uxon.domain.version import format_version as _format_version_str
from uxon.infra.run import run_query


def repo_root() -> Path:
    """Best-effort path to the repo root for in-tree dev runs.

    For pipx / `uv tool` / wheel installs this points into site-packages,
    which is normally not a git checkout. Callers must tolerate unavailable
    commit and dirty-state metadata.
    """
    return Path(__file__).resolve().parents[3]


def source_checkout_root() -> Path | None:
    """Return the Uxon checkout containing this imported module, if any.

    A wheel may be installed below an unrelated Git worktree. Merely asking
    Git for the nearest repository would then report the consumer project's
    commit. The checkout is trusted only when its source-tree module is the
    exact file Python imported.
    """
    root = repo_root()
    expected_module = root / "src" / "uxon" / "infra" / "version_probe.py"
    try:
        if not expected_module.samefile(Path(__file__)):
            return None
    except OSError:
        return None
    return root


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
    checkout = source_checkout_root()
    if checkout is None:
        return None
    root = str(checkout)
    cp = run_query(
        ["git", "-c", f"safe.directory={root}", "-C", root, "rev-parse", "--short", "HEAD"],
    )
    if cp.returncode != 0:
        return None
    commit = (cp.stdout or "").strip()
    return commit or None


def repo_is_dirty() -> bool:
    checkout = source_checkout_root()
    if checkout is None:
        return False
    root = str(checkout)
    refresh = run_query(
        ["git", "-c", f"safe.directory={root}", "-C", root, "update-index", "-q", "--refresh"],
    )
    if refresh.returncode != 0:
        return False
    cp = run_query(
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

"""Pure, Textual-free git-worktree helpers.

This module imports no UI framework and no ``subprocess`` machinery — it
holds the deterministic computations (path layout, slug, porcelain
parsing) so they are unit-testable without spinning up a git repo or the
TUI. The side-effecting git calls live in ``uxon.cli`` and pass their
captured stdout into :func:`parse_worktree_porcelain`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass


def _slugify(name: str) -> str:
    # Byte-identical to ``uxon.cli.slugify`` (cli.py:796-798). Duplicated —
    # NOT imported — because ``uxon.cli`` imports THIS module, so importing
    # ``slugify`` back from cli would create a circular import. CRITICAL:
    # this rule and ``cli.slugify`` must stay identical, or the worktree
    # PATH slug (here) and the session STEM slug (``session_stem_for_worktree``
    # → ``cli.slugify``) diverge and session↔worktree identity (§2.5) breaks
    # silently. The parity is locked by a test (Task 1 SlugParityTests).
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    return slug or "workspace"


def compute_worktree_path(*, repo_root: str, branch: str, worktree_root: str) -> str:
    """Return the absolute directory a worktree for ``branch`` should occupy.

    Default (empty ``worktree_root``): ``<repo_root>/.uxon/worktrees/<slug>``.
    Override: ``<worktree_root>/<repo-slug>/<slug>``. The caller gates the
    result through ``is_worktree_target_allowed`` (the not-yet-exists
    predicate — the dir does not exist yet) — this function never touches
    the filesystem.
    """
    branch_slug = _slugify(branch)
    if worktree_root:
        repo_slug = _slugify(os.path.basename(repo_root.rstrip("/")))
        return os.path.join(worktree_root, repo_slug, branch_slug)
    return os.path.join(repo_root, ".uxon", "worktrees", branch_slug)


@dataclass(frozen=True)
class Workspace:
    """One git working tree as surfaced to the TUI WORKSPACE column.

    ``label`` is the user-facing row text (branch name, or short SHA for a
    detached worktree, with ``(primary)`` appended by the renderer — not
    here). ``branch`` is the porcelain branch short-name or the short SHA
    for a detached HEAD. ``path`` is the absolute worktree directory.
    ``is_primary`` marks the main working tree (first porcelain entry /
    path == repo root).
    """

    label: str
    branch: str
    path: str
    is_primary: bool


def parse_worktree_porcelain(text: str, *, repo_root: str) -> list[Workspace]:
    raise NotImplementedError  # implemented in Task 2

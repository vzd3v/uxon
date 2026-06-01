"""Pure, Textual-free git-worktree helpers.

This module imports no UI framework and no ``subprocess`` machinery — it
holds the deterministic computations (path layout, slug, porcelain
parsing) so they are unit-testable without spinning up a git repo or the
TUI. The side-effecting git calls live in ``uxon.cli`` and pass their
captured stdout into :func:`parse_worktree_porcelain`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from uxon.domain.session import slugify


def compute_worktree_path(*, repo_root: str, branch: str, worktree_root: str) -> str:
    """Return the absolute directory a worktree for ``branch`` should occupy.

    Default (empty ``worktree_root``): ``<repo_root>/.uxon/worktrees/<slug>``.
    Override: ``<worktree_root>/<repo-slug>/<slug>``. The caller gates the
    result through ``is_worktree_target_allowed`` (the not-yet-exists
    predicate — the dir does not exist yet) — this function never touches
    the filesystem.
    """
    branch_slug = slugify(branch)
    if worktree_root:
        repo_slug = slugify(os.path.basename(repo_root.rstrip("/")))
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
    """Parse ``git worktree list --porcelain`` into :class:`Workspace` rows.

    Records are separated by blank lines. Each starts with ``worktree
    <path>``; the working tree's ref is ``branch refs/heads/<name>`` (a
    real branch), ``detached`` (no branch — labelled by short SHA), or
    ``bare`` (no working tree — dropped). The primary working tree is the
    one whose path equals ``repo_root`` (it is also conventionally first;
    we detect by path so a future reorder can't fool us).
    """
    repo_root = repo_root.rstrip("/")
    rows: list[Workspace] = []
    path = ""
    head = ""
    branch = ""
    is_bare = False
    is_detached = False

    def flush() -> None:
        nonlocal path, head, branch, is_bare, is_detached
        if path and not is_bare:
            if branch:
                label = branch
            else:  # detached HEAD — short SHA stands in for the branch.
                label = head[:7]
                branch = head[:7]
            rows.append(
                Workspace(
                    label=label,
                    branch=branch,
                    path=path,
                    is_primary=(path.rstrip("/") == repo_root),
                )
            )
        path = ""
        head = ""
        branch = ""
        is_bare = False
        is_detached = False

    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if not line.strip():
            flush()
            continue
        if line.startswith("worktree "):
            flush()
            path = line[len("worktree ") :].strip()
        elif line.startswith("HEAD "):
            head = line[len("HEAD ") :].strip()
        elif line.startswith("branch "):
            ref = line[len("branch ") :].strip()
            branch = ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref
        elif line.strip() == "detached":
            is_detached = True
        elif line.strip() == "bare":
            is_bare = True
    flush()
    return rows

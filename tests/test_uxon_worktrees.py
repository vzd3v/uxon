"""Pure unit tests for uxon.worktrees (no Textual, no subprocess)."""

from __future__ import annotations

import unittest

from uxon.worktrees import compute_worktree_path


class ComputeWorktreePathTests(unittest.TestCase):
    def test_default_layout_inside_repo(self) -> None:
        path = compute_worktree_path(
            repo_root="/srv/work/myapp", branch="feature/auth", worktree_root=""
        )
        self.assertEqual(path, "/srv/work/myapp/.uxon/worktrees/feature-auth")

    def test_worktree_root_override_adds_repo_slug(self) -> None:
        path = compute_worktree_path(
            repo_root="/srv/work/myapp", branch="bugfix-1", worktree_root="/data/wt"
        )
        self.assertEqual(path, "/data/wt/myapp/bugfix-1")

    def test_distinct_branches_can_slugify_to_same_path(self) -> None:
        # §8 slug collision precondition: both reduce to "feature-auth".
        a = compute_worktree_path(repo_root="/r/app", branch="feature/auth", worktree_root="")
        b = compute_worktree_path(repo_root="/r/app", branch="feature-auth", worktree_root="")
        self.assertEqual(a, b)


class SlugParityTests(unittest.TestCase):
    """C5: the worktree-path slug and the session-stem slug MUST match.

    ``compute_worktree_path`` uses ``worktrees._slugify``; the session stem
    uses ``cli.slugify`` (via ``session_stem_for_worktree``). If they ever
    diverge, the created session name and the probe-derived name disagree
    and identity breaks silently. Lock them together here.
    """

    def test_slugify_matches_cli_slugify(self) -> None:
        import uxon.cli as cli
        from uxon.worktrees import _slugify

        for branch in [
            "feature/auth",
            "feature-auth",
            "BugFix_123",
            "weird//name!!",
            "",
            "déjà-vu",
            "a/b/c",
        ]:
            self.assertEqual(_slugify(branch), cli.slugify(branch), f"slug mismatch for {branch!r}")

"""Pure unit tests for uxon.worktrees (no Textual, no subprocess)."""

from __future__ import annotations

import unittest

from uxon.worktrees import compute_worktree_path, parse_worktree_porcelain


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


class ParsePorcelainTests(unittest.TestCase):
    def _sample(self) -> str:
        # Real ``git worktree list --porcelain`` shape: blank-line separated
        # records, "worktree <path>" first, then "HEAD"/"branch" or
        # "detached" or "bare".
        return (
            "worktree /srv/work/myapp\n"
            "HEAD 1111111111111111111111111111111111111111\n"
            "branch refs/heads/main\n"
            "\n"
            "worktree /srv/work/myapp/.uxon/worktrees/feature-auth\n"
            "HEAD 2222222222222222222222222222222222222222\n"
            "branch refs/heads/feature/auth\n"
            "\n"
            "worktree /srv/work/myapp/.uxon/worktrees/detached-one\n"
            "HEAD 3333333333333333333333333333333333333333\n"
            "detached\n"
        )

    def test_primary_is_first_and_repo_root(self) -> None:
        rows = parse_worktree_porcelain(self._sample(), repo_root="/srv/work/myapp")
        self.assertTrue(rows[0].is_primary)
        self.assertEqual(rows[0].branch, "main")
        self.assertEqual(rows[0].path, "/srv/work/myapp")

    def test_linked_worktree_branch_short_name(self) -> None:
        rows = parse_worktree_porcelain(self._sample(), repo_root="/srv/work/myapp")
        self.assertEqual(rows[1].branch, "feature/auth")
        self.assertFalse(rows[1].is_primary)
        self.assertEqual(rows[1].path, "/srv/work/myapp/.uxon/worktrees/feature-auth")

    def test_detached_uses_short_sha_as_branch(self) -> None:
        rows = parse_worktree_porcelain(self._sample(), repo_root="/srv/work/myapp")
        self.assertEqual(rows[2].branch, "3333333")  # 7-char short sha
        self.assertEqual(rows[2].label, "3333333")

    def test_bare_repo_entry_skipped(self) -> None:
        text = "worktree /srv/work/bare.git\nbare\n"
        rows = parse_worktree_porcelain(text, repo_root="/srv/work/bare.git")
        # A bare repo has no working tree to launch into — it is dropped.
        self.assertEqual(rows, [])

    def test_primary_detected_by_path_match_not_only_order(self) -> None:
        # If git ever reorders, path==repo_root still flags the primary.
        text = (
            "worktree /srv/work/myapp/.uxon/worktrees/x\n"
            "HEAD 4444444444444444444444444444444444444444\n"
            "branch refs/heads/x\n"
            "\n"
            "worktree /srv/work/myapp\n"
            "HEAD 5555555555555555555555555555555555555555\n"
            "branch refs/heads/main\n"
        )
        rows = parse_worktree_porcelain(text, repo_root="/srv/work/myapp")
        primary = [w for w in rows if w.is_primary]
        self.assertEqual(len(primary), 1)
        self.assertEqual(primary[0].path, "/srv/work/myapp")

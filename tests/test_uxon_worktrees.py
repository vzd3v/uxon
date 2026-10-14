"""Mixed worktree porcelain is decoded once, including reordered and bare entries."""

from uxon.infra.worktrees import Workspace, parse_worktree_porcelain


def test_mixed_porcelain_preserves_every_workspace_field_without_final_separator():
    transcript = (
        "worktree /srv/work/myapp/.uxon/worktrees/feature-auth\n"
        "HEAD 2222222222222222222222222222222222222222\n"
        "branch refs/heads/feature/auth\n\n"
        "worktree /srv/work/bare.git\nbare\n\n"
        "worktree /srv/work/myapp\n"
        "HEAD 1111111111111111111111111111111111111111\n"
        "branch refs/heads/main\n\n"
        "worktree /srv/work/myapp/.uxon/worktrees/detached\n"
        "HEAD 3333333333333333333333333333333333333333\ndetached\n"
    )
    assert parse_worktree_porcelain(transcript, repo_root="/srv/work/myapp/") == [
        Workspace(
            "feature/auth", "feature/auth", "/srv/work/myapp/.uxon/worktrees/feature-auth", False
        ),
        Workspace("main", "main", "/srv/work/myapp", True),
        Workspace("3333333", "3333333", "/srv/work/myapp/.uxon/worktrees/detached", False),
    ]

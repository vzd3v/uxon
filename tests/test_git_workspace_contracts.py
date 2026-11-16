"""Real Git and filesystem regressions for target-user workspace operations."""

from __future__ import annotations

import errno
import os
import shutil
import struct
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from helpers import make_config

from uxon.infra import git
from uxon.infra.identity import process_user
from uxon.infra.worktrees import WorktreeProbeError

pytestmark = pytest.mark.usefixtures("real_execution")


def command(root: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-C",
            str(root),
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout


def repository(path: Path, *, metadata: Path | None = None) -> Path:
    path.mkdir()
    command(
        path,
        "init",
        "-q",
        "-b",
        "main",
        *(["--separate-git-dir", str(metadata)] if metadata else []),
    )
    command(path, "commit", "--allow-empty", "-qm", "initial")
    return path


# Real helper startup keeps the production 0.5s deadline; run in the serial integration lane.
@pytest.mark.slow
def test_real_discovery_preserves_git_layout_paths_and_errors(tmp_path):
    cfg, user = make_config(), process_user()
    plain = tmp_path / "plain"
    plain.mkdir()
    assert git.discover_workspaces(cfg, str(plain), user) is None
    for separate in (False, True):
        repo = repository(
            tmp_path / f"repo-{separate}", metadata=tmp_path / "metadata" if separate else None
        )
        linked = tmp_path / f"linked-{separate}\nwith newline"
        command(repo, "worktree", "add", "-b", "feature", str(linked))
        main = git.discover_workspaces(cfg, str(repo), user)
        assert main.current_root == main.primary_root == main.source_anchor == str(repo)
        assert any(row.path == str(repo) and row.is_primary for row in main.workspaces)
        child = linked / "nested"
        child.mkdir()
        result = git.discover_workspaces(cfg, str(child), user)
        assert result.target_path == str(child) and result.current_root == str(linked)
        assert any(row.path == str(linked) for row in result.workspaces)
        assert all(row.path != str(tmp_path / "metadata") for row in result.workspaces)
        if not separate:
            assert result.primary_root == str(repo)
        metadata = Path(result.common_dir)
        config = metadata / "config"
        original = config.read_bytes()
        config.write_bytes(b"[broken")
        with pytest.raises(WorktreeProbeError, match="config"):
            git.discover_workspaces(cfg, str(repo), user)
        config.write_bytes(original)


@pytest.mark.parametrize(
    "conflict", ["tracked", "tracked_symlink", "destination_parent", "existing", "source_symlink"]
)
def test_include_conflicts_never_overwrite_or_escape_worktree(tmp_path, conflict):
    cfg, user = make_config(), process_user()
    repo = repository(tmp_path / "repo")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "secret"
    sentinel.write_bytes(b"outside-original")
    relative = "nested/secret" if conflict == "destination_parent" else ".env"
    command(repo, "checkout", "-qb", "feature")
    if conflict in {"tracked", "tracked_symlink"}:
        if conflict == "tracked":
            (repo / relative).write_bytes(b"tracked-original")
        else:
            (repo / relative).symlink_to(sentinel)
        command(repo, "add", relative)
        command(repo, "commit", "-qm", "destination content")
    command(repo, "checkout", "-q", "main")
    (repo / ".gitignore").write_text(relative + "\n")
    (repo / ".worktreeinclude").write_text(relative + "\n")
    source = repo / relative
    source.parent.mkdir(exist_ok=True)
    if conflict == "source_symlink":
        source.symlink_to(sentinel)
    else:
        source.write_bytes(b"source-private")
    destination = tmp_path / "linked"
    command(repo, "worktree", "add", str(destination), "feature")
    if conflict == "existing":
        (destination / relative).write_bytes(b"untracked-original")
    elif conflict == "destination_parent":
        (destination / "nested").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SystemExit):
        git.copy_worktreeinclude_matches(cfg, str(repo), str(destination), user)
    assert sentinel.read_bytes() == b"outside-original"
    if conflict in {"tracked", "existing"}:
        assert (destination / relative).read_bytes() == (
            b"tracked-original" if conflict == "tracked" else b"untracked-original"
        )
    if conflict == "tracked_symlink":
        assert (destination / relative).is_symlink()
    assert not list(destination.rglob(".uxon-copy-*"))


def access_acl(path):
    try:
        return os.getxattr(path, "system.posix_acl_access")
    except OSError as exc:
        if exc.errno == errno.ENODATA:
            return None
        raise


def default_acl(path):
    # Pick an unrelated numeric identity; no installed account is required by POSIX ACLs.
    reader = os.getuid() + 100000
    entries = [
        (1, 7, 0xFFFFFFFF),
        (2, 4, reader),
        (4, 5, 0xFFFFFFFF),
        (16, 5, 0xFFFFFFFF),
        (32, 0, 0xFFFFFFFF),
    ]
    value = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *row) for row in entries)
    try:
        os.setxattr(path, "system.posix_acl_default", value)
    except OSError as exc:
        if exc.errno in {errno.ENOTSUP, errno.EOPNOTSUPP}:
            pytest.skip("filesystem does not support POSIX ACLs")
        raise
    return value


def test_include_preserves_source_acl_group_and_complete_nested_contents(tmp_path):
    cfg, user = make_config(), process_user()
    repo = repository(tmp_path / "repo")
    (repo / ".gitignore").write_text("nested/\n")
    (repo / ".worktreeinclude").write_text("nested/\n")
    (repo / "nested").mkdir()
    source = repo / "nested" / "private\nname"
    source.write_bytes(b"private\x00" * 180000)
    source.chmod(0o640)
    destination = tmp_path / "linked"
    command(repo, "worktree", "add", "-b", "feature", str(destination))
    default_acl(destination)
    groups = [gid for gid in os.getgroups() if gid != source.stat().st_gid]
    if groups:
        os.chown(destination, -1, groups[0])
        destination.chmod(0o2770)
    git.copy_worktreeinclude_matches(cfg, str(repo), str(destination), user)
    copied = destination / "nested" / source.name
    assert copied.read_bytes() == source.read_bytes()
    assert copied.stat().st_mode & 0o777 == 0o640
    assert copied.stat().st_gid == source.stat().st_gid
    assert access_acl(copied) == access_acl(source) is None
    assert not list(destination.rglob(".uxon-copy-*"))
    # Preserve an explicit source ACL too, not just the absence of one.
    acl_source = repo / "nested" / "with-acl"
    acl_source.write_bytes(b"acl-data")
    default_acl(repo / "nested")
    acl_source.unlink()
    acl_source.write_bytes(b"acl-data")
    source.unlink()  # Already copied paths intentionally conflict on a second run.
    git.copy_worktreeinclude_matches(cfg, str(repo), str(destination), user)
    assert access_acl(destination / "nested" / "with-acl") == access_acl(acl_source)


def test_exclude_preserves_inode_permissions_acl_and_serializes_writers(tmp_path):
    cfg, user = make_config(), process_user()
    repo = repository(tmp_path / "repo", metadata=tmp_path / "metadata")
    exclude = tmp_path / "metadata" / "info" / "exclude"
    exclude.write_bytes(b"existing-without-newline")
    exclude.chmod(0o640)
    before = exclude.stat()
    default_acl(exclude.parent)
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: git.write_uxon_exclude_entry(cfg, str(repo), user), range(3)))
    after = exclude.stat()
    assert exclude.read_bytes() == b"existing-without-newline\n.uxon/\n"
    assert (after.st_ino, after.st_uid, after.st_gid, after.st_mode) == (
        before.st_ino,
        before.st_uid,
        before.st_gid,
        before.st_mode,
    )
    assert access_acl(exclude) is None
    exclude.unlink()
    git.write_uxon_exclude_entry(cfg, str(repo), user)
    assert access_acl(exclude) is not None  # A new file retains the directory's policy.
    shutil.rmtree(exclude.parent)
    git.write_uxon_exclude_entry(cfg, str(repo), user)
    assert exclude.read_bytes() == b".uxon/\n"


def test_group_writable_exclude_owned_by_other_uid_can_be_updated(tmp_path):
    if os.getuid() == 0:
        pytest.skip("requires an unprivileged writer")
    cfg, user = make_config(), process_user()
    repo = repository(tmp_path / "repo")
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_bytes(b"shared\n")
    exclude.chmod(0o660)
    result = subprocess.run(
        ["sudo", "-n", "chown", f"0:{os.getgid()}", str(exclude)], capture_output=True, timeout=3
    )
    if result.returncode:
        pytest.skip("requires permission to assign a different owner to this temporary file")
    before = exclude.stat()
    git.write_uxon_exclude_entry(cfg, str(repo), user)
    assert exclude.read_bytes() == b"shared\n.uxon/\n"
    assert (exclude.stat().st_uid, exclude.stat().st_ino) == (before.st_uid, before.st_ino)

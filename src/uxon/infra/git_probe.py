# SPDX-License-Identifier: MIT
"""Git discovery and worktree file operations inside one execution boundary.

The adapter invokes this module as the target user. Git owns repository and
ignore semantics; filesystem writes use directory descriptors, never shell paths.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import secrets
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from uxon.infra.run import run_query
from uxon.infra.worktrees import GitDiscovery, parse_worktree_porcelain


class GitRead:
    """One bounded batch of Git queries, not a timeout per subprocess."""

    def __init__(self, timeout: float) -> None:
        self.deadline = time.monotonic() + timeout

    def run(self, path: str, *args: str, check: bool = True):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Git query timed out")
        cp = run_query(["git", "-C", path, *args], text=False, timeout=remaining)
        if check and cp.returncode:
            raise ValueError(
                os.fsdecode(cp.stderr).strip() or f"git {args[0]} failed ({cp.returncode})"
            )
        return cp


def _git_path(data: bytes) -> str:
    # Remove Git's record terminator, not whitespace that belongs to the path.
    return os.fsdecode(data.removesuffix(b"\n"))


def _has_git_metadata(path: str) -> bool:
    """Conservatively distinguish absent repositories from failed Git discovery.

    Git uses exit 128 for both. Filesystem inspection and a separate config
    query avoid recognizing translated diagnostic sentences. Unreadable
    ancestors, malformed gitfiles and unsafe ownership remain errors.
    """
    cursor = Path(path)
    device = cursor.stat().st_dev
    ceilings = {
        Path(value).resolve()
        for value in os.environ.get("GIT_CEILING_DIRECTORIES", "").split(os.pathsep)
        if os.path.isabs(value)
    }
    across = os.environ.get("GIT_DISCOVERY_ACROSS_FILESYSTEM", "false").lower()
    if across not in {"", "0", "false", "no", "off", "1", "true", "yes", "on"}:
        raise ValueError("invalid GIT_DISCOVERY_ACROSS_FILESYSTEM boolean")
    cross_filesystems = across in {"1", "true", "yes", "on"}
    while True:
        with os.scandir(cursor) as entries:
            names = {entry.name for entry in entries}
        if ".git" in names or {"HEAD", "objects", "refs"} <= names:
            return True
        if cursor.parent == cursor:
            return False
        if cursor.parent in ceilings:
            return False
        if not cross_filesystems and cursor.parent.stat().st_dev != device:
            return False
        cursor = cursor.parent


def discover(path: str, timeout: float) -> GitDiscovery | None:
    target = str(Path(path).resolve(strict=True))
    if not Path(target).is_dir():
        raise ValueError(f"not a directory: {target}")
    git = GitRead(timeout)
    cp = git.run(target, "worktree", "list", "--porcelain", "-z", check=False)
    if cp.returncode:
        if (
            cp.returncode == 128
            and not any(key in os.environ for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"))
            and not _has_git_metadata(target)
        ):
            # This also verifies that Git itself and its configuration work.
            git.run(target, "config", "--null", "--list")
            return None
        raise ValueError(
            os.fsdecode(cp.stderr).strip() or f"git worktree list failed ({cp.returncode})"
        )
    records = os.fsdecode(cp.stdout)
    first = records.split("\0\0", 1)[0].split("\0")
    if not first or not first[0].startswith("worktree "):
        raise ValueError("git worktree list returned no repository record")
    first_path = first[0][len("worktree ") :]
    primary = None if "bare" in first else first_path
    bare = git.run(target, "rev-parse", "--is-bare-repository").stdout == b"true\n"
    current = None if bare else _git_path(git.run(target, "rev-parse", "--show-toplevel").stdout)
    common = _git_path(
        git.run(target, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout
    )
    if primary == common:
        # With --separate-git-dir Git can report the metadata directory as
        # its main worktree. Only the main checkout itself establishes the
        # actual path; a linked checkout may have no backlink to that path.
        git_dir = _git_path(git.run(target, "rev-parse", "--absolute-git-dir").stdout)
        primary = current if git_dir == common else None
        if primary is not None:
            records = "worktree " + primary + records[len("worktree " + first_path) :]
        else:
            records = records.partition("\0\0")[2]
    return GitDiscovery(
        target_path=target,
        current_root=current,
        primary_root=primary,
        source_anchor=primary or current or first_path,
        common_dir=common,
        workspaces=tuple(parse_worktree_porcelain(records, repo_root=primary or "")),
    )


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _parts(relative: str) -> list[str]:
    parts = relative.split("/")
    if not relative or any(part in {"", ".", "..", ".git"} for part in parts):
        raise ValueError(f"unsafe worktree include path: {relative!r}")
    return parts


@contextmanager
def _directory(path: str, *, create: bool = False):
    if not os.path.isabs(path):
        raise ValueError(f"expected absolute directory: {path}")
    fd = os.open("/", _DIRECTORY_FLAGS)
    try:
        for part in Path(path).parts[1:]:
            if create:
                try:
                    os.mkdir(part, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _parent(root_fd: int, parts: list[str], *, create: bool = False):
    fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _regular(parent_fd: int, name: str):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"worktree include source is not a regular file: {name!r}")
        yield fd
    finally:
        os.close(fd)


def _assert_absent(parent_fd: int, name: str) -> None:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise ValueError(f"refusing to overwrite worktree destination: {name!r}")


def _temporary(parent_fd: int, mode: int) -> tuple[str, int]:
    name = f".uxon-copy-{secrets.token_hex(16)}"
    return name, os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode, dir_fd=parent_fd)


def _access_acl(fd: int) -> bytes | None:
    try:
        return os.getxattr(fd, "system.posix_acl_access")
    except OSError as exc:
        if exc.errno not in {errno.ENODATA, errno.ENOTSUP}:
            raise
        return None


def _copy_permissions(source_fd: int, destination_fd: int, metadata: os.stat_result) -> None:
    if os.fstat(destination_fd).st_gid != metadata.st_gid:
        try:
            os.fchown(destination_fd, -1, metadata.st_gid)
        except OSError as exc:
            raise ValueError(f"cannot preserve source file group {metadata.st_gid}: {exc}") from exc
    acl = _access_acl(source_fd)
    if acl is not None:
        os.setxattr(destination_fd, "system.posix_acl_access", acl)
    else:
        # chmod changes an ACL's mask; it does not remove inherited named
        # readers. Preserve the absence of a source access ACL explicitly.
        try:
            os.removexattr(destination_fd, "system.posix_acl_access")
        except OSError as exc:
            if exc.errno not in {errno.ENODATA, errno.ENOTSUP}:
                raise
    # Keep the temporary file's restrictive mask until inherited named-reader
    # ACL entries have been removed or replaced with the source's access ACL.
    os.fchmod(destination_fd, stat.S_IMODE(metadata.st_mode) & 0o777)


def _copy_one(source_fd: int, destination_fd: int, relative: str) -> None:
    parts = _parts(relative)
    with _parent(source_fd, parts) as src_parent, _regular(src_parent, parts[-1]) as src:
        metadata = os.fstat(src)
        with _parent(destination_fd, parts, create=True) as dst_parent:
            _assert_absent(dst_parent, parts[-1])
            name, fd = _temporary(dst_parent, 0o600)
            try:
                with os.fdopen(fd, "wb") as output, os.fdopen(os.dup(src), "rb") as source:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
                    output.flush()
                    _copy_permissions(src, output.fileno(), metadata)
                    os.utime(output.fileno(), ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
                # link is an atomic no-replace publication of the complete file.
                os.link(
                    name,
                    parts[-1],
                    src_dir_fd=dst_parent,
                    dst_dir_fd=dst_parent,
                    follow_symlinks=False,
                )
            finally:
                os.unlink(name, dir_fd=dst_parent)


def copy_includes(source: str, destination: str, timeout: float) -> None:
    with _directory(source) as source_fd:
        try:
            with _regular(source_fd, ".worktreeinclude") as include_fd:
                with os.fdopen(os.dup(include_fd), "rb") as stream:
                    patterns = stream.read()
        except FileNotFoundError:
            return
        git = GitRead(timeout)
        ignored = set(
            git.run(source, "ls-files", "-z", "-o", "-i", "--exclude-standard").stdout.split(b"\0")
        )
        # Give Git an immutable snapshot of the safely opened pattern file.
        with tempfile.NamedTemporaryFile() as patterns_file:
            patterns_file.write(patterns)
            patterns_file.flush()
            included = set(
                git.run(
                    source, "ls-files", "-z", "-o", "-i", f"--exclude-from={patterns_file.name}"
                ).stdout.split(b"\0")
            )
        candidates = sorted(os.fsdecode(name) for name in ignored & included if name)
        if not candidates:
            return
        tracked = {
            os.fsdecode(name)
            for name in git.run(destination, "ls-files", "-z", "--cached", "--", ".").stdout.split(
                b"\0"
            )
            if name
        }
        with _directory(destination) as destination_fd:
            # Finish every predictable conflict check before making directories/files.
            for relative in candidates:
                parts = _parts(relative)
                ancestors = {"/".join(parts[:index]) for index in range(1, len(parts) + 1)}
                if ancestors & tracked or any(name.startswith(relative + "/") for name in tracked):
                    raise ValueError(
                        f"worktree include conflicts with tracked destination: {relative!r}"
                    )
                with _parent(source_fd, parts) as parent, _regular(parent, parts[-1]):
                    pass
                try:
                    with _parent(destination_fd, parts) as parent:
                        _assert_absent(parent, parts[-1])
                except FileNotFoundError:
                    pass
            for relative in candidates:
                try:
                    _copy_one(source_fd, destination_fd, relative)
                except (OSError, ValueError) as exc:
                    raise ValueError(f"copying worktree include {relative!r}: {exc}") from exc


def update_exclude(path: str, timeout: float) -> None:
    git = GitRead(timeout)
    exclude = _git_path(
        git.run(path, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude").stdout
    )
    parent, filename = os.path.split(exclude)
    with _directory(parent, create=True) as parent_fd:
        # Lock the directory inode: no persistent lock artifact, no unlink race.
        fcntl.flock(parent_fd, fcntl.LOCK_EX)
        metadata = None
        try:
            with _regular(parent_fd, filename) as old_fd:
                metadata = os.fstat(old_fd)
                with os.fdopen(os.dup(old_fd), "rb") as stream:
                    current = stream.read()
        except FileNotFoundError:
            current = b""
        if b".uxon/" in (line.strip() for line in current.splitlines()):
            return
        suffix = (b"\n" if current and not current.endswith(b"\n") else b"") + b".uxon/\n"
        flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        if metadata is None:
            flags |= os.O_CREAT | os.O_EXCL
        fd = os.open(filename, flags, 0o666, dir_fd=parent_fd)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or (
                metadata is not None
                and (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            ):
                raise ValueError(f"exclude file changed before append: {exclude!r}")
            # One append preserves the inode's owner, group, mode and ACL,
            # including a shared file this user cannot chown. Other Uxon
            # writers hold the same directory lock; external editors may not.
            written = os.write(fd, suffix)
            if written != len(suffix):
                raise OSError(
                    f"partial exclude append at {exclude!r} ({written}/{len(suffix)} bytes); "
                    "existing data retained, inspect the final line before retrying"
                )
        finally:
            os.close(fd)


def main(argv: list[str] | None = None) -> int:
    from dataclasses import asdict
    from subprocess import TimeoutExpired

    parser = argparse.ArgumentParser(prog="uxon git-probe")
    parser.add_argument("--mode", choices=("discover", "copy", "exclude"), required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--destination")
    parser.add_argument("--timeout", type=float, required=True)
    args = parser.parse_args(argv)
    payload: dict[str, object] = {"ok": True, "error": ""}
    try:
        if args.mode == "discover":
            result = discover(args.path, args.timeout)
            payload["discovery"] = None if result is None else asdict(result)
        elif args.mode == "copy":
            if not args.destination:
                raise ValueError("copy requires a destination")
            copy_includes(args.path, args.destination, args.timeout)
        else:
            update_exclude(args.path, args.timeout)
    except (OSError, ValueError, TimeoutExpired) as exc:
        payload = {"ok": False, "error": str(exc)}
    print(json.dumps(payload, ensure_ascii=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

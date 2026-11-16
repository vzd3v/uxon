# SPDX-License-Identifier: MIT
"""Git adapter: repo-root resolution, base-ref discovery, and the
``.git/info/exclude`` + ``.worktreeinclude`` file operations.

Every function here shells out to ``git`` (most under the launch user's
sudo prefix). Pure git-naming helpers live in :mod:`uxon.domain.session`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from uxon.domain.config import Config
from uxon.domain.execution import LOCAL_PROBE_TIMEOUT_SECONDS
from uxon.errors import fail
from uxon.infra.execution import command_prefix
from uxon.infra.process import run_cmd
from uxon.infra.run import run_query
from uxon.infra.worktrees import GitDiscovery, Workspace, WorktreeProbeError


def git_repo_root(cwd: str) -> str | None:
    from uxon.infra.git_probe import discover

    result = discover(cwd, LOCAL_PROBE_TIMEOUT_SECONDS)
    return None if result is None else result.current_root or result.source_anchor


def git_repo_root_as_user(cfg: Config, cwd: str, target_user: str) -> str | None:
    try:
        result = discover_workspaces(cfg, cwd, target_user)
    except WorktreeProbeError as exc:
        fail(str(exc))
    return None if result is None else result.current_root or result.source_anchor


def git_repo_root_nonint_as_user(cfg: Config, cwd: str, target_user: str) -> str | None:
    """Non-interactive variant of :func:`git_repo_root_as_user`.

    Uses a non-interactive execution prefix (``sudo -n`` locally) so a missing
    NOPASSWD grant fails fast instead of blocking on a hidden password
    prompt — required for the fullscreen TUI's worktree probe.
    """
    return git_repo_root_as_user(cfg, cwd, target_user)


def git_common_dir_root_as_user(cfg: Config, cwd: str, target_user: str) -> str | None:
    """Resolve Git's primary working tree or a bare repository's source anchor."""
    try:
        result = discover_workspaces(cfg, cwd, target_user)
    except WorktreeProbeError as exc:
        fail(str(exc))
    return None if result is None else result.primary_root or result.source_anchor


def _worktree_helper(
    cfg: Config, user: str, mode: str, path: str, *, destination: str | None = None
) -> dict:
    backend = cfg.execution.backend_for_user(user)
    argv = command_prefix(cfg, user, interactive=False) + [
        sys.executable,
        "-m",
        "uxon.infra.git_probe",
        "--mode",
        mode,
        "--path",
        path,
        "--timeout",
        str(backend.probe_timeout_seconds),
    ]
    if destination is not None:
        argv += ["--destination", destination]
    try:
        # Filesystem mutation keeps the existing unbounded copy policy. Its
        # Git reads have one deadline; killing a writer could strand a temp.
        cp = run_query(argv, timeout=backend.probe_timeout_seconds if mode == "discover" else None)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorktreeProbeError(f"Git {mode} for {path!r} as {user!r}: {exc}") from exc
    try:
        payload = json.loads(cp.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        detail = (cp.stderr or "").strip()
        raise WorktreeProbeError(
            f"Git {mode} returned invalid helper output: {detail or exc}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True or cp.returncode:
        detail = payload.get("error") if isinstance(payload, dict) else None
        raise WorktreeProbeError(
            f"Git {mode} for {path!r}: {detail or cp.stderr or 'helper failed'}"
        )
    return payload


def discover_workspaces(cfg: Config, target_path: str, user: str) -> GitDiscovery | None:
    """Read target-user Git state, without profile/agent/tmux authorization.

    Only a confirmed non-repository returns None. Backend failures, unreadable
    metadata, invalid configuration and Git errors raise WorktreeProbeError.
    The caller may share this read across profiles with the same execution key.
    """
    payload = _worktree_helper(cfg, user, "discover", target_path)
    data = payload.get("discovery")
    if data is None and "discovery" in payload:
        return None
    try:
        if not isinstance(data, dict):
            raise ValueError("missing discovery object")
        for key in ("target_path", "source_anchor", "common_dir"):
            if not isinstance(data.get(key), str) or not os.path.isabs(data[key]):
                raise ValueError(f"invalid {key}")
        for key in ("current_root", "primary_root"):
            if data.get(key) is not None and (
                not isinstance(data[key], str) or not os.path.isabs(data[key])
            ):
                raise ValueError(f"invalid {key}")
        rows = data["workspaces"]
        if not isinstance(rows, list):
            raise ValueError("invalid workspace list")
        workspaces = []
        for row in rows:
            if (
                not isinstance(row, dict)
                or any(not isinstance(row.get(key), str) for key in ("path", "label", "branch"))
                or not os.path.isabs(row["path"])
                or not isinstance(row.get("is_primary"), bool)
            ):
                raise ValueError("invalid workspace record")
            workspaces.append(Workspace(**row))
        return GitDiscovery(
            data["target_path"],
            data["current_root"],
            data["primary_root"],
            data["source_anchor"],
            data["common_dir"],
            tuple(workspaces),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise WorktreeProbeError(f"Git discovery returned invalid data: {exc}") from exc


def write_uxon_exclude_entry(cfg: Config, repo_root: str, launch_user: str) -> None:
    """Append to Git's exclude inode, preserving owner, mode and access ACL.

    A directory lock serializes Uxon writers; external editors need not honor it.
    """
    try:
        _worktree_helper(cfg, launch_user, "exclude", repo_root)
    except WorktreeProbeError as exc:
        fail(str(exc))


def copy_worktreeinclude_matches(cfg: Config, repo_root: str, dest: str, launch_user: str) -> None:
    """Copy gitignored files matching ``.worktreeinclude`` into ``dest``.

    Copy set = ``A ∩ B`` where A = ``git ls-files -o -i --exclude-standard``
    (gitignored + untracked) and B = ``git ls-files -o -i
    --exclude-from=<.worktreeinclude>`` (untracked matching the include
    patterns). Both queries are ``--others`` so tracked files are excluded
    by construction; git is the sole authority for ignore + match.
    No-op when ``.worktreeinclude`` is absent.
    """
    try:
        _worktree_helper(cfg, launch_user, "copy", repo_root, destination=dest)
    except WorktreeProbeError as exc:
        fail(str(exc))


def _branch_exists_as_user(cfg: Config, repo_root: str, branch: str, launch_user: str) -> bool:
    cp = run_query(
        command_prefix(cfg, launch_user, interactive=False)
        + ["git", "-C", repo_root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
    )
    return cp.returncode == 0


def _local_base_ref_as_user(cfg: Config, repo_root: str, launch_user: str) -> str:
    """Local base ref for a new branch: local origin/HEAD if present, else HEAD.

    No network — origin/HEAD is consulted only if a local remote-tracking
    symref exists.
    """
    cp = run_query(
        command_prefix(cfg, launch_user, interactive=False)
        + ["git", "-C", repo_root, "rev-parse", "--verify", "--quiet", "origin/HEAD"],
    )
    return "origin/HEAD" if cp.returncode == 0 else "HEAD"


def _remote_base_ref_as_user(cfg: Config, repo_root: str, launch_user: str) -> str:
    """Base ref after a ``worktree_base = "remote"`` fetch.

    ``git fetch origin`` does NOT create the local ``origin/HEAD`` symref
    (only clone / ``git remote set-head`` do), so we cannot assume it
    exists. Establish it explicitly via ``git remote set-head origin -a``
    (a local, network-free operation that points ``origin/HEAD`` at the
    remote's default branch using the already-fetched refs); then use
    ``origin/HEAD``. If that still fails (no default detectable), fall
    back to the verified local resolver so the add never gets a
    non-existent ref.
    """
    prefix = command_prefix(cfg, launch_user, interactive=True)
    run_cmd(prefix + ["git", "-C", repo_root, "remote", "set-head", "origin", "-a"], check=False)
    cp = run_query(
        command_prefix(cfg, launch_user, interactive=False)
        + ["git", "-C", repo_root, "rev-parse", "--verify", "--quiet", "origin/HEAD"],
    )
    if cp.returncode == 0:
        return "origin/HEAD"
    return _local_base_ref_as_user(cfg, repo_root, launch_user)

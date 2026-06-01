# SPDX-License-Identifier: MIT
"""Git adapter: repo-root resolution, base-ref discovery, and the
``.git/info/exclude`` + ``.worktreeinclude`` file operations.

Every function here shells out to ``git`` (most under the launch user's
sudo prefix). Pure git-naming helpers live in :mod:`uxon.domain.session`.
"""

from __future__ import annotations

import os
import shlex
import subprocess

from uxon.domain.authz import canonical
from uxon.errors import fail
from uxon.infra.identity import command_prefix_for_user, nonint_command_prefix_for_user
from uxon.infra.process import run_cmd


def git_repo_root(cwd: str) -> str | None:
    cp = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
        text=True,
        capture_output=True,
    )
    if cp.returncode != 0:
        return None
    out = (cp.stdout or "").strip()
    if not out:
        return None
    return canonical(out)


def git_repo_root_as_user(cwd: str, target_user: str) -> str | None:
    cp = subprocess.run(
        command_prefix_for_user(target_user) + ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
        text=True,
        capture_output=True,
    )
    if cp.returncode != 0:
        return None
    out = (cp.stdout or "").strip()
    if not out:
        return None
    return canonical(out)


def git_repo_root_nonint_as_user(cwd: str, target_user: str) -> str | None:
    """Non-interactive variant of :func:`git_repo_root_as_user`.

    Uses :func:`nonint_command_prefix_for_user` (``sudo -n``) so a missing
    NOPASSWD grant fails fast instead of blocking on a hidden password
    prompt — required for the fullscreen TUI's worktree probe (§4.2).
    """
    cp = subprocess.run(
        nonint_command_prefix_for_user(target_user)
        + ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
        text=True,
        capture_output=True,
    )
    if cp.returncode != 0:
        return None
    out = (cp.stdout or "").strip()
    if not out:
        return None
    return canonical(out)


def git_common_dir_root_as_user(cwd: str, target_user: str) -> str | None:
    """Resolve the *primary* working tree of the repo containing ``cwd``.

    Uses ``git rev-parse --git-common-dir``: on a linked worktree this
    returns the primary repo's ``.git`` (whereas ``--show-toplevel``
    returns the *linked* worktree root). The primary root is that dir's
    parent. This anchors new worktrees to the primary repo even when
    launched from inside another worktree (§8 worktree-from-worktree).
    Non-interactive prefix, same rationale as the resolver above.
    """
    cp = subprocess.run(
        nonint_command_prefix_for_user(target_user)
        + ["git", "-C", cwd, "rev-parse", "--git-common-dir"],
        text=True,
        capture_output=True,
    )
    if cp.returncode != 0:
        return None
    common = (cp.stdout or "").strip()
    if not common:
        return None
    common_abs = common if os.path.isabs(common) else os.path.join(cwd, common)
    # ``<root>/.git`` → ``<root>``.
    return canonical(os.path.dirname(common_abs))


_UXON_EXCLUDE_LINE = ".uxon/"


def write_uxon_exclude_entry(repo_root: str, launch_user: str) -> None:
    """Idempotently append ``.uxon/`` to ``.git/info/exclude`` as launch_user.

    Local-only (never committed) and concurrency-safe: read-modify-write
    via a temp file + atomic rename so two simultaneous ``launch_user``
    creates can't double-append or clobber each other (§2.3). Skipped by
    the caller when ``worktree_root`` is set (out-of-repo worktree).
    """
    prefix = command_prefix_for_user(launch_user)
    exclude_path = os.path.join(repo_root, ".git", "info", "exclude")
    # Read current contents (tolerate absent file).
    cp = subprocess.run(
        prefix + ["sh", "-c", f"cat {shlex.quote(exclude_path)} 2>/dev/null || true"],
        text=True,
        capture_output=True,
    )
    current = cp.stdout or ""
    if any(line.strip() == _UXON_EXCLUDE_LINE for line in current.splitlines()):
        return  # already present — idempotent
    new_contents = current
    if new_contents and not new_contents.endswith("\n"):
        new_contents += "\n"
    new_contents += _UXON_EXCLUDE_LINE + "\n"
    # Atomic temp-file-then-rename under the info/ dir (same filesystem),
    # serialising the read-modify-write against concurrent writers.
    info_dir = os.path.join(repo_root, ".git", "info")
    script = (
        f"mkdir -p {shlex.quote(info_dir)} && "
        f"tmp=$(mktemp {shlex.quote(info_dir)}/exclude.XXXXXX) && "
        f'cat > "$tmp" && mv -f "$tmp" {shlex.quote(exclude_path)}'
    )
    # run_cmd() does not forward stdin, so feed the new contents directly
    # via subprocess.run (same capture/text conventions as run_cmd) and
    # fail() with the captured stderr on a non-zero exit.
    cp = subprocess.run(
        prefix + ["sh", "-c", script],
        text=True,
        input=new_contents,
        capture_output=True,
    )
    if cp.returncode != 0:
        fail((cp.stderr or "").strip() or "failed to write .git/info/exclude")


def copy_worktreeinclude_matches(repo_root: str, dest: str, launch_user: str) -> None:
    """Copy gitignored files matching ``.worktreeinclude`` into ``dest``.

    Copy set = ``A ∩ B`` where A = ``git ls-files -o -i --exclude-standard``
    (gitignored + untracked) and B = ``git ls-files -o -i
    --exclude-from=<.worktreeinclude>`` (untracked matching the include
    patterns). Both queries are ``--others`` so tracked files are excluded
    by construction; git is the sole authority for ignore + match (§2.4).
    No-op when ``.worktreeinclude`` is absent.
    """
    prefix = command_prefix_for_user(launch_user)
    include_file = os.path.join(repo_root, ".worktreeinclude")
    if not os.path.exists(include_file):
        return

    def _ls(extra: list[str]) -> set[str]:
        cp = subprocess.run(
            prefix + ["git", "-C", repo_root, "ls-files", "-o", "-i"] + extra,
            text=True,
            capture_output=True,
        )
        if cp.returncode != 0:
            return set()
        return {ln for ln in (cp.stdout or "").splitlines() if ln.strip()}

    set_a = _ls(["--exclude-standard"])
    set_b = _ls([f"--exclude-from={include_file}"])
    for rel in sorted(set_a & set_b):
        src = os.path.join(repo_root, rel)
        dst = os.path.join(dest, rel)
        run_cmd(prefix + ["mkdir", "-p", os.path.dirname(dst)], check=True)
        run_cmd(prefix + ["cp", "-p", src, dst], check=True)


def _branch_exists_as_user(repo_root: str, branch: str, launch_user: str) -> bool:
    cp = subprocess.run(
        nonint_command_prefix_for_user(launch_user)
        + ["git", "-C", repo_root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        text=True,
        capture_output=True,
    )
    return cp.returncode == 0


def _local_base_ref_as_user(repo_root: str, launch_user: str) -> str:
    """Local base ref for a new branch: local origin/HEAD if present, else HEAD.

    No network — origin/HEAD is consulted only if a local remote-tracking
    symref exists (``worktree_base = "local"`` contract, §4.5).
    """
    cp = subprocess.run(
        nonint_command_prefix_for_user(launch_user)
        + ["git", "-C", repo_root, "rev-parse", "--verify", "--quiet", "origin/HEAD"],
        text=True,
        capture_output=True,
    )
    return "origin/HEAD" if cp.returncode == 0 else "HEAD"


def _remote_base_ref_as_user(repo_root: str, launch_user: str) -> str:
    """Base ref after a ``worktree_base = "remote"`` fetch (§4.5, C4).

    ``git fetch origin`` does NOT create the local ``origin/HEAD`` symref
    (only clone / ``git remote set-head`` do), so we cannot assume it
    exists. Establish it explicitly via ``git remote set-head origin -a``
    (a local, network-free operation that points ``origin/HEAD`` at the
    remote's default branch using the already-fetched refs); then use
    ``origin/HEAD``. If that still fails (no default detectable), fall
    back to the verified local resolver so the add never gets a
    non-existent ref.
    """
    prefix = command_prefix_for_user(launch_user)
    run_cmd(prefix + ["git", "-C", repo_root, "remote", "set-head", "origin", "-a"], check=False)
    cp = subprocess.run(
        nonint_command_prefix_for_user(launch_user)
        + ["git", "-C", repo_root, "rev-parse", "--verify", "--quiet", "origin/HEAD"],
        text=True,
        capture_output=True,
    )
    if cp.returncode == 0:
        return "origin/HEAD"
    return _local_base_ref_as_user(repo_root, launch_user)

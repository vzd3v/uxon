"""gh-CLI backend for git-remote-on-new-project.

Runs ``gh`` under ``creds_user`` so the OAuth token stored in that user's
``~/.config/gh/hosts.yml`` is the one that authenticates — not the
caller's token. Local git operations (``git init``, ``git push``) are
driven by the orchestrator under launch_user; this module only handles
the *remote creation* API call.

All subprocess calls go through a small ``run`` seam so tests can stub
execution without monkey-patching ``subprocess`` globally.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from ccw_git_profiles import GitRemoteProfile


class BackendError(RuntimeError):
    """Raised on any preflight/creation failure. ``stage`` hints which
    step failed so callers can give precise diagnostics.
    """

    def __init__(self, message: str, *, stage: str = "") -> None:
        super().__init__(message)
        self.stage = stage


@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str


def default_run(cmd: list[str], *, timeout: int = 20) -> RunResult:
    """Real subprocess runner. Tests override via the ``run`` parameter."""
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    return RunResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def sudo_prefix(creds_user: str, current_user: str) -> list[str]:
    """Return ``sudo -n -u <creds_user> --`` or empty when already that
    user. ``-n`` makes the call fail fast instead of prompting.
    """
    if not creds_user or creds_user == current_user:
        return []
    return ["sudo", "-n", "-u", creds_user, "--"]


def preflight(
    profile: GitRemoteProfile,
    repo_name: str,
    effective_creds_user: str,
    current_user: str,
    *,
    run=default_run,
) -> None:
    """Verify that ``gh`` under ``effective_creds_user`` can reach
    ``profile.host`` and that ``owner/repo_name`` is not already taken.
    """
    prefix = sudo_prefix(effective_creds_user, current_user)

    which = run(prefix + ["sh", "-c", "command -v gh"])
    if which.returncode != 0 or not which.stdout.strip():
        raise BackendError(
            f"gh CLI not found for user {effective_creds_user or current_user!r}",
            stage="preflight",
        )

    status = run(prefix + ["gh", "auth", "status", "--hostname", profile.host])
    if status.returncode != 0:
        detail = status.stderr.strip() or status.stdout.strip() or "unknown error"
        raise BackendError(
            f"gh is not logged in to {profile.host} under "
            f"{effective_creds_user or current_user!r}: {detail}",
            stage="preflight",
        )

    view = run(
        prefix
        + [
            "gh",
            "repo",
            "view",
            f"{profile.owner}/{repo_name}",
            "--json",
            "name",
        ]
    )
    if view.returncode == 0:
        raise BackendError(
            f"repository {profile.owner}/{repo_name} already exists on {profile.host}",
            stage="preflight",
        )


def create_remote(
    profile: GitRemoteProfile,
    repo_name: str,
    project_dir: str,  # noqa: ARG001 — unused (local push happens under launch_user)
    effective_creds_user: str,
    current_user: str,
    *,
    dry_run: bool = False,
    run=default_run,
) -> str:
    """Create the remote via ``gh repo create`` and return the SSH clone
    URL. ``gh`` is intentionally **not** given ``--source``/``--push``:
    the local ``.git`` is owned by launch_user and may be unreadable by
    creds_user. The orchestrator pushes under launch_user instead.
    """
    prefix = sudo_prefix(effective_creds_user, current_user)
    cmd = prefix + [
        "gh",
        "repo",
        "create",
        f"{profile.owner}/{repo_name}",
        f"--{profile.visibility}",
    ]
    if dry_run:
        return profile.ssh_remote_url(repo_name)
    result = run(cmd, timeout=60)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise BackendError(
            f"gh repo create failed for {profile.owner}/{repo_name}: {detail}",
            stage="remote_create",
        )
    return profile.ssh_remote_url(repo_name)


def describe_command(
    profile: GitRemoteProfile,
    repo_name: str,
    project_dir: str,  # noqa: ARG001 — kept for signature symmetry
    effective_creds_user: str,
    current_user: str,
) -> list[str]:
    """Return the command that :func:`create_remote` would run — used by
    orchestrator's ``--dry-run`` output.
    """
    prefix = sudo_prefix(effective_creds_user, current_user)
    return prefix + [
        "gh",
        "repo",
        "create",
        f"{profile.owner}/{repo_name}",
        f"--{profile.visibility}",
    ]

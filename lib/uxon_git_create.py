"""Git-remote-on-new-project: orchestrator.

Wires the two responsibilities together for a single ``uxon new`` call:

1. **Under launch_user**: ``git init -b main`` + empty initial commit in
   ``project_dir``. If that directory already has a ``.git``, the whole
   operation is skipped (the caller is re-running ``uxon new`` on a
   project that was already initialized).
2. **Under creds_user** (falls back to launch_user if empty): delegate
   remote creation to the matching backend (``gh`` or ``token``).
3. **Under launch_user**: ``git remote add origin <url>`` and
   ``git push -u origin main``.

The two backends (:mod:`uxon_git_backend_gh` and
:mod:`uxon_git_backend_token`) share the ``BackendError`` type so the
orchestrator can translate failures into a single
:class:`CreationError` with a ``stage`` field for caller diagnostics.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field

from uxon_git_backend_gh import BackendError, RunResult, default_run
from uxon_git_backend_gh import create_remote as gh_create_remote
from uxon_git_backend_gh import describe_command as gh_describe
from uxon_git_backend_gh import preflight as gh_preflight
from uxon_git_backend_token import create_remote as token_create_remote
from uxon_git_backend_token import describe_command as token_describe
from uxon_git_backend_token import preflight as token_preflight
from uxon_git_profiles import GitRemoteProfile

STAGES = ("preflight", "local_init", "remote_create", "remote_config", "push")


class CreationError(RuntimeError):
    """Single failure type raised by :func:`create_project_remote`.

    ``stage`` ∈ :data:`STAGES` tells callers how much was done before
    the failure, so they can give the operator a clear next step
    (e.g. "local .git is initialized; fix credentials and rerun").
    """

    def __init__(self, message: str, *, stage: str) -> None:
        super().__init__(message)
        self.stage = stage


@dataclass
class CreationResult:
    profile_name: str
    ssh_url: str
    commands: list[str] = field(default_factory=list)  # populated in --dry-run


# ── Local git ops (under launch_user) ────────────────────────────────


def _launch_prefix(launch_user: str, current_user: str) -> list[str]:
    """Shell prefix for running ``git`` as launch_user. We use ``sudo
    -iu`` here (not ``-n -u``) to match the rest of uxon's launch flow.
    """
    if not launch_user or launch_user == current_user:
        return []
    return ["sudo", "-iu", launch_user, "--"]


def _git_cmd(launch_user: str, current_user: str, project_dir: str, *args: str) -> list[str]:
    return _launch_prefix(launch_user, current_user) + [
        "git",
        "-C",
        project_dir,
        *args,
    ]


def _has_git_dir(
    project_dir: str,
    launch_user: str,
    current_user: str,
    *,
    run=default_run,
) -> bool:
    res = run(_launch_prefix(launch_user, current_user) + ["test", "-d", f"{project_dir}/.git"])
    return res.returncode == 0


def _run_or_raise(cmd: list[str], stage: str, *, run=default_run, timeout: int = 60) -> RunResult:
    res = run(cmd, timeout=timeout)
    if res.returncode != 0:
        detail = res.stderr.strip() or res.stdout.strip() or "unknown error"
        raise CreationError(
            f"{stage} failed: {' '.join(shlex.quote(x) for x in cmd)}\n{detail}",
            stage=stage,
        )
    return res


def _local_init(
    project_dir: str,
    launch_user: str,
    current_user: str,
    *,
    run=default_run,
) -> list[str]:
    """``git init -b main`` + empty initial commit. Returns the list of
    commands executed (for --dry-run reporting).
    """
    commands = [
        _git_cmd(launch_user, current_user, project_dir, "init", "-b", "main"),
        _git_cmd(
            launch_user,
            current_user,
            project_dir,
            "commit",
            "--allow-empty",
            "-m",
            "init",
        ),
    ]
    for cmd in commands:
        _run_or_raise(cmd, "local_init", run=run)
    return [shlex.join(cmd) for cmd in commands]


def _wire_remote_and_push(
    project_dir: str,
    launch_user: str,
    current_user: str,
    ssh_url: str,
    *,
    run=default_run,
) -> list[str]:
    """``git remote add origin <url>`` + ``git push -u origin main``,
    both under launch_user. Raises :class:`CreationError` with a
    distinct stage for each step so operators see which one failed.
    """
    remote_add = _git_cmd(
        launch_user, current_user, project_dir, "remote", "add", "origin", ssh_url
    )
    _run_or_raise(remote_add, "remote_config", run=run)
    push = _git_cmd(launch_user, current_user, project_dir, "push", "-u", "origin", "main")
    _run_or_raise(push, "push", run=run)
    return [shlex.join(remote_add), shlex.join(push)]


# ── Public entry point ───────────────────────────────────────────────


def resolve_creds_user(profile: GitRemoteProfile, launch_user: str) -> str:
    """Which OS user runs the remote-creation step. Empty
    ``profile.creds_user`` falls back to launch_user.
    """
    return profile.creds_user or launch_user


def create_project_remote(
    profile: GitRemoteProfile,
    repo_name: str,
    project_dir: str,
    launch_user: str,
    current_user: str,
    *,
    dry_run: bool = False,
    run=default_run,
    http=None,
) -> CreationResult:
    """Full pipeline. On success returns a :class:`CreationResult` with
    the chosen SSH URL. On any failure raises :class:`CreationError`
    with a stage tag. The local ``.git`` is not rolled back — the
    operator can inspect or rerun manually.

    ``http`` is forwarded to the token backend for injection in tests;
    ignored by the ``gh`` backend. Defaults to the real HTTP client.
    """
    effective_creds_user = resolve_creds_user(profile, launch_user)
    commands_trace: list[str] = []

    if _has_git_dir(project_dir, launch_user, current_user, run=run):
        raise CreationError(
            f"{project_dir}/.git already exists; refusing to re-initialize",
            stage="local_init",
        )

    # 1. Preflight (backend-specific)
    try:
        if profile.auth == "gh":
            gh_preflight(profile, repo_name, effective_creds_user, current_user, run=run)
        elif profile.auth == "token":
            if http is None:
                from uxon_git_backend_token import default_http as _default_http

                http_impl = _default_http
            else:
                http_impl = http
            token_preflight(
                profile,
                repo_name,
                effective_creds_user,
                current_user,
                run=run,
                http=http_impl,
            )
        else:  # defensive; load_profiles rejects this already
            raise CreationError(f"unsupported auth mode: {profile.auth!r}", stage="preflight")
    except BackendError as exc:
        raise CreationError(str(exc), stage=exc.stage or "preflight") from exc

    if dry_run:
        # Report what we *would* do.
        trace = [
            shlex.join(_git_cmd(launch_user, current_user, project_dir, "init", "-b", "main")),
            shlex.join(
                _git_cmd(
                    launch_user,
                    current_user,
                    project_dir,
                    "commit",
                    "--allow-empty",
                    "-m",
                    "init",
                )
            ),
        ]
        if profile.auth == "gh":
            trace.append(
                shlex.join(
                    gh_describe(profile, repo_name, project_dir, effective_creds_user, current_user)
                )
            )
        else:
            trace.append(token_describe(profile, repo_name))
        trace.append(
            shlex.join(
                _git_cmd(
                    launch_user,
                    current_user,
                    project_dir,
                    "remote",
                    "add",
                    "origin",
                    profile.ssh_remote_url(repo_name),
                )
            )
        )
        trace.append(
            shlex.join(
                _git_cmd(
                    launch_user,
                    current_user,
                    project_dir,
                    "push",
                    "-u",
                    "origin",
                    "main",
                )
            )
        )
        return CreationResult(
            profile_name=profile.name,
            ssh_url=profile.ssh_remote_url(repo_name),
            commands=trace,
        )

    # 2. Local init
    try:
        commands_trace.extend(_local_init(project_dir, launch_user, current_user, run=run))
    except CreationError:
        raise
    except Exception as exc:  # pragma: no cover — very defensive
        raise CreationError(f"local_init crashed: {exc}", stage="local_init") from exc

    # 3. Remote create (under creds_user; both backends return ssh_url
    #    without touching the local .git — launch_user pushes next).
    try:
        if profile.auth == "gh":
            ssh_url = gh_create_remote(
                profile,
                repo_name,
                project_dir,
                effective_creds_user,
                current_user,
                run=run,
            )
        else:  # token
            from uxon_git_backend_token import default_http as _default_http

            http_impl = http or _default_http
            ssh_url = token_create_remote(
                profile,
                repo_name,
                project_dir,
                effective_creds_user,
                current_user,
                run=run,
                http=http_impl,
            )
    except BackendError as exc:
        raise CreationError(str(exc), stage=exc.stage or "remote_create") from exc

    # 4. Wire remote URL and push (always under launch_user).
    commands_trace.extend(
        _wire_remote_and_push(project_dir, launch_user, current_user, ssh_url, run=run)
    )

    return CreationResult(profile_name=profile.name, ssh_url=ssh_url, commands=commands_trace)

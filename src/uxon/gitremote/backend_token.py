"""Token backend for git-remote-on-new-project.

Reads a fine-grained Personal Access Token (PAT) from ``profile.token_file``
and calls the provider's REST API inside the selected execution backend. The
token value is held for the operation and is never written to stdout, stderr, logs,
or exception messages — if it appears, we redact to ``***``.

This backend assumes GitHub-compatible REST semantics:
    - personal repos: ``POST /user/repos``
    - org repos:      ``POST /orgs/<owner>/repos``
    - existence check: ``GET /repos/<owner>/<repo>``
    - identity check:  ``GET /user``
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from uxon.domain.config import Config
from uxon.domain.git_profiles import GitRemoteProfile
from uxon.gitremote.backend_gh import BackendError, default_run, execution_prefix
from uxon.infra.execution import wrap_command
from uxon.infra.run import run_query

# ── Token file I/O (under creds_user) ────────────────────────────────


def read_token(
    cfg: Config,
    token_file: str,
    effective_creds_user: str,
    current_user: str,
    *,
    run=default_run,
) -> str:
    """Read and return the token. ``sudo -n -u <creds_user> cat -- <path>``
    so the call fails cleanly if the file isn't readable under that user
    or passwordless sudo isn't set up.

    Raises :class:`BackendError` if reading fails. The returned string is
    stripped of trailing whitespace.
    """
    prefix = execution_prefix(cfg, effective_creds_user)
    res = run(prefix + ["cat", "--", token_file])
    if res.returncode != 0:
        stderr = res.stderr.strip() or "unknown error"
        # Don't echo the path content — only the path itself is non-sensitive.
        raise BackendError(
            f"cannot read token_file={token_file!r} as "
            f"{effective_creds_user or current_user!r}: {stderr}",
            stage="preflight",
        )
    token = res.stdout.strip()
    if not token:
        raise BackendError(
            f"token_file={token_file!r} is empty under {effective_creds_user or current_user!r}",
            stage="preflight",
        )
    return token


# ── HTTP helpers (token in header, never in URL/body logs) ───────────


@dataclass
class HttpResponse:
    status: int
    body: bytes

    def json(self):
        if not self.body:
            return None
        return json.loads(self.body.decode("utf-8"))


def default_http(
    method: str,
    url: str,
    token: str,
    *,
    body: dict | None = None,
    timeout: int = 15,
) -> HttpResponse:
    """Minimal ``urllib`` wrapper. The token goes into ``Authorization:
    Bearer ...`` only; it's never logged here.
    """
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "uxon-git-remote",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return HttpResponse(status=resp.status, body=resp.read())
    except urllib.error.HTTPError as exc:
        return HttpResponse(status=exc.code, body=exc.read() or b"")
    except urllib.error.URLError as exc:
        # Network-level failure (DNS, connection refused, timeout). Convert
        # to a BackendError so the caller's "except BackendError" catches it
        # and the token never ends up in an uncaught traceback.
        host = url.split("://", 1)[-1].split("/", 1)[0]
        detail = str(exc.reason).replace(token, "***")
        raise BackendError(f"network error reaching {host}: {detail}", stage="") from None


def _api_error_message(resp: HttpResponse, fallback: str) -> str:
    try:
        payload = resp.json()
    except (ValueError, UnicodeDecodeError):
        payload = None
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str) and message:
            return f"{resp.status}: {message}"
    return f"{resp.status}: {fallback}"


@contextmanager
def _token_errors(token: str, stage: str) -> Iterator[None]:
    """Keep provider diagnostics useful without exposing an echoed credential."""
    try:
        yield
    except Exception as exc:
        detail = str(exc).replace(token, "***")
        raise BackendError(detail, stage=stage) from None


def _authenticated_login(profile: GitRemoteProfile, token: str, *, stage: str, http) -> str:
    me = http("GET", f"{profile.api_base()}/user", token)
    if me.status != 200:
        raise BackendError(
            f"token rejected by {profile.host}: " + _api_error_message(me, "GET /user failed"),
            stage=stage,
        )
    try:
        data = me.json()
    except (ValueError, UnicodeError):
        data = None
    login = data.get("login") if isinstance(data, dict) else None
    if not isinstance(login, str) or not login:
        raise BackendError(f"unexpected response from {profile.host} GET /user", stage=stage)
    return login


# ── Backend entry points ─────────────────────────────────────────────


def preflight(
    cfg: Config,
    profile: GitRemoteProfile,
    repo_name: str,
    effective_creds_user: str,
    current_user: str,
    *,
    run=default_run,
    http=default_http,
) -> None:
    """Verify the token's identity and that the target repository is absent.

    Creation permission is determined by the create endpoint: organization
    membership listings cannot establish a fine-grained token's permissions.
    """
    if cfg.execution.backend_for_user(effective_creds_user).kind == "command":
        _worker_request(cfg, profile, repo_name, effective_creds_user, operation="preflight")
        return
    token = read_token(cfg, profile.token_file, effective_creds_user, current_user, run=run)
    try:
        with _token_errors(token, "preflight"):
            _preflight_with_token(profile, repo_name, token, http=http)
    finally:
        # Drop the local reference ASAP.
        del token


def _preflight_with_token(
    profile: GitRemoteProfile,
    repo_name: str,
    token: str,
    *,
    http,
) -> None:
    base = profile.api_base()

    _authenticated_login(profile, token, stage="preflight", http=http)

    # Existence check — we don't want to half-init locally then fail remotely.
    exists = http("GET", f"{base}/repos/{profile.owner}/{repo_name}", token)
    if exists.status == 200:
        raise BackendError(
            f"repository {profile.owner}/{repo_name} already exists on {profile.host}",
            stage="preflight",
        )
    if exists.status != 404:
        raise BackendError(
            f"unexpected status from {profile.host} checking repo existence: "
            + _api_error_message(exists, "GET /repos failed"),
            stage="preflight",
        )


def create_remote(
    cfg: Config,
    profile: GitRemoteProfile,
    repo_name: str,
    project_dir: str,  # noqa: ARG001 — unused here; kept for interface symmetry with gh
    effective_creds_user: str,
    current_user: str,
    *,
    dry_run: bool = False,
    run=default_run,
    http=default_http,
) -> str:
    """Create the remote via REST. Local git push is the orchestrator's
    job — this backend just returns the SSH URL to use.
    """
    if dry_run:
        return profile.ssh_remote_url(repo_name)

    if cfg.execution.backend_for_user(effective_creds_user).kind == "command":
        return _worker_request(cfg, profile, repo_name, effective_creds_user, operation="create")

    token = read_token(cfg, profile.token_file, effective_creds_user, current_user, run=run)
    try:
        with _token_errors(token, "remote_create"):
            return _create_with_token(profile, repo_name, token, http=http)
    finally:
        del token


def _worker_request(
    cfg: Config,
    profile: GitRemoteProfile,
    repo_name: str,
    effective_creds_user: str,
    *,
    operation: str,
) -> str:
    request = {
        "operation": operation,
        "profile": {
            "name": profile.name,
            "host": profile.host,
            "owner": profile.owner,
            "auth": profile.auth,
            "creds_user": profile.creds_user,
            "token_file": profile.token_file,
            "visibility": profile.visibility,
        },
        "repo_name": repo_name,
    }
    argv = wrap_command(
        cfg,
        effective_creds_user,
        [sys.executable, "-m", "uxon.gitremote.token_worker"],
        interactive=False,
    )
    try:
        cp = run_query(
            argv,
            input=json.dumps(request, sort_keys=True, separators=(",", ":")),
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise BackendError("token worker timed out", stage=operation) from None
    except OSError as exc:
        raise BackendError(f"token worker failed: {exc}", stage=operation) from None
    try:
        response = json.loads(cp.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise BackendError(f"token worker returned invalid JSON: {exc}", stage=operation) from None
    if cp.returncode == 0 and isinstance(response, dict) and response.get("ok") is not True:
        raise BackendError("token worker returned failure with a zero exit status", stage=operation)
    if cp.returncode != 0 and isinstance(response, dict) and response.get("ok") is True:
        raise BackendError(
            "token worker returned success with a non-zero exit status", stage=operation
        )
    if not isinstance(response, dict) or response.get("ok") is not True:
        error = (
            response.get("error", "token worker failed")
            if isinstance(response, dict)
            else "token worker failed"
        )
        stage = response.get("stage", operation) if isinstance(response, dict) else operation
        raise BackendError(str(error), stage=str(stage))
    if set(response) != {"ok", "ssh_url"} or not isinstance(response.get("ssh_url"), str):
        raise BackendError("token worker returned an invalid result", stage=operation)
    return str(response["ssh_url"])


def _create_with_token(profile: GitRemoteProfile, repo_name: str, token: str, *, http) -> str:
    base = profile.api_base()
    # Determine endpoint: user's own repo vs org repo. Cheap re-query so
    # callers don't have to pass login alongside the profile.
    login = _authenticated_login(profile, token, stage="remote_create", http=http)

    payload = {
        "name": repo_name,
        "private": profile.visibility == "private",
        "auto_init": False,
    }
    if isinstance(login, str) and login.casefold() == profile.owner.casefold():
        url = f"{base}/user/repos"
    else:
        url = f"{base}/orgs/{profile.owner}/repos"

    resp = http("POST", url, token, body=payload)
    if resp.status not in (200, 201):
        raise BackendError(
            f"failed to create {profile.owner}/{repo_name} on {profile.host}: "
            + _api_error_message(resp, "POST repos failed"),
            stage="remote_create",
        )

    data = resp.json()
    if not isinstance(data, dict):
        raise BackendError(
            f"unexpected response from {profile.host} creating repository", stage="remote_create"
        )
    ssh_url = data.get("ssh_url")
    if isinstance(ssh_url, str) and ssh_url:
        return ssh_url
    # Fallback — compose ourselves rather than fail when server omits it.
    return profile.ssh_remote_url(repo_name)


def describe_command(profile: GitRemoteProfile, repo_name: str) -> str:
    """Return a one-line, token-free description for ``--dry-run`` output."""
    return (
        f"POST {profile.api_base()}/user/repos "
        f"(or /orgs/{profile.owner}/repos)  "
        f"name={repo_name} private={profile.visibility == 'private'}  "
        f"Authorization: Bearer ***"
    )

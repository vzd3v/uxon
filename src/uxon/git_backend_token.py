"""Token backend for git-remote-on-new-project.

Reads a fine-grained Personal Access Token (PAT) from ``profile.token_file``
under ``creds_user`` (via ``sudo -n -u <user> cat --``) and calls the
provider's REST API directly. The token value is held only for the
duration of each API call and is never written to stdout, stderr, logs,
or exception messages — if it appears, we redact to ``***``.

This backend assumes GitHub-compatible REST semantics:
    - personal repos: ``POST /user/repos``
    - org repos:      ``POST /orgs/<owner>/repos``
    - existence check: ``GET /repos/<owner>/<repo>``
    - identity check:  ``GET /user``, ``GET /user/orgs``
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from uxon.git_backend_gh import BackendError, default_run, sudo_prefix
from uxon.git_profiles import GitRemoteProfile

# ── Token file I/O (under creds_user) ────────────────────────────────


def read_token(
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
    prefix = sudo_prefix(effective_creds_user, current_user)
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
        raise BackendError(f"network error reaching {host}: {exc.reason}", stage="") from None


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


# ── Backend entry points ─────────────────────────────────────────────


def preflight(
    profile: GitRemoteProfile,
    repo_name: str,
    effective_creds_user: str,
    current_user: str,
    *,
    run=default_run,
    http=default_http,
) -> None:
    """Verify: token readable; token valid; owner is either the token
    owner or an org visible to it; target repo doesn't exist yet.
    """
    token = read_token(profile.token_file, effective_creds_user, current_user, run=run)
    try:
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

    me = http("GET", f"{base}/user", token)
    if me.status != 200:
        raise BackendError(
            f"token rejected by {profile.host}: " + _api_error_message(me, "GET /user failed"),
            stage="preflight",
        )
    login = (me.json() or {}).get("login", "") if me.body else ""
    if not isinstance(login, str) or not login:
        raise BackendError(
            f"unexpected response from {profile.host} GET /user",
            stage="preflight",
        )

    if login != profile.owner:
        orgs = http("GET", f"{base}/user/orgs", token)
        if orgs.status != 200:
            raise BackendError(
                f"token cannot list orgs on {profile.host}: "
                + _api_error_message(orgs, "GET /user/orgs failed"),
                stage="preflight",
            )
        org_logins = {o.get("login", "") for o in (orgs.json() or []) if isinstance(o, dict)}
        if profile.owner not in org_logins:
            raise BackendError(
                f"token for {login!r} cannot create repos under owner "
                f"{profile.owner!r}: not token user and {profile.owner!r} "
                f"is not visible among the token's {len(org_logins)} org(s)",
                stage="preflight",
            )

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

    token = read_token(profile.token_file, effective_creds_user, current_user, run=run)
    try:
        return _create_with_token(profile, repo_name, token, http=http)
    finally:
        del token


def _create_with_token(profile: GitRemoteProfile, repo_name: str, token: str, *, http) -> str:
    base = profile.api_base()
    # Determine endpoint: user's own repo vs org repo. Cheap re-query so
    # callers don't have to pass login alongside the profile.
    me = http("GET", f"{base}/user", token)
    if me.status != 200:
        raise BackendError(
            f"token rejected by {profile.host}: " + _api_error_message(me, "GET /user failed"),
            stage="remote_create",
        )
    login = (me.json() or {}).get("login", "")

    payload = {
        "name": repo_name,
        "private": profile.visibility == "private",
        "auto_init": False,
    }
    if login == profile.owner:
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

    data = resp.json() or {}
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

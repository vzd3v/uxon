"""Git-remote-on-new-project: profile schema.

A *profile* names one explicit target where ccw is allowed to create a
new remote repository. ccw will never create a repo outside of the
configured whitelist.

Profiles come from ``config/config.toml`` under ``[[git_remote_profiles]]``
and are parsed into immutable :class:`GitRemoteProfile` instances by
:func:`load_profiles`. Validation errors are reported with a one-shot
:class:`ProfileError` so the caller can decide how to surface them (CLI
``fail(...)`` or a structured TUI message).

This module is pure data: no subprocess, no filesystem, no network.
"""

from __future__ import annotations

from dataclasses import dataclass


AUTH_CHOICES: tuple[str, ...] = ("gh", "token")
VISIBILITY_CHOICES: tuple[str, ...] = ("private", "public")


class ProfileError(ValueError):
    """Raised when a profile definition is invalid."""


@dataclass(frozen=True)
class GitRemoteProfile:
    """One entry from ``[[git_remote_profiles]]`` after validation.

    ``creds_user`` is the OS user on this server whose credentials are
    used for the *remote creation* step (``gh`` CLI under ``auth="gh"``
    or ``token_file`` under ``auth="token"``). Empty string means
    "use the launch_user" — resolved at call time, not here.
    """

    name: str
    host: str
    owner: str
    auth: str  # one of AUTH_CHOICES
    creds_user: str  # empty → launch_user
    token_file: str  # non-empty iff auth == "token"
    visibility: str  # one of VISIBILITY_CHOICES

    def ssh_remote_url(self, repo_name: str) -> str:
        """SSH clone URL used when pushing from launch_user (GitHub-only
        for now; other hosts follow the same scheme).
        """
        return f"git@{self.host}:{self.owner}/{repo_name}.git"

    def https_remote_url(self, repo_name: str) -> str:
        return f"https://{self.host}/{self.owner}/{repo_name}.git"

    def api_base(self) -> str:
        """REST API base URL used by the ``token`` backend."""
        if self.host == "github.com":
            return "https://api.github.com"
        # Enterprise GitHub: api.<host>/api/v3 (not supported yet; flag
        # at call time so the error is close to the attempt).
        return f"https://{self.host}/api/v3"


def _validate_profile(raw: dict, index: int, seen_names: set[str]) -> GitRemoteProfile:
    def _req(field: str) -> str:
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ProfileError(
                f"git_remote_profiles[{index}]: missing or empty '{field}'"
            )
        return value.strip()

    name = _req("name")
    if name in seen_names:
        raise ProfileError(
            f"git_remote_profiles: duplicate name {name!r}"
        )
    host = _req("host")
    owner = _req("owner")
    auth = _req("auth")
    if auth not in AUTH_CHOICES:
        raise ProfileError(
            f"git_remote_profiles[{name}]: auth must be one of "
            f"{AUTH_CHOICES}, got {auth!r}"
        )

    creds_user = raw.get("creds_user", "")
    if not isinstance(creds_user, str):
        raise ProfileError(
            f"git_remote_profiles[{name}]: creds_user must be a string"
        )
    creds_user = creds_user.strip()

    token_file = raw.get("token_file", "")
    if not isinstance(token_file, str):
        raise ProfileError(
            f"git_remote_profiles[{name}]: token_file must be a string"
        )
    token_file = token_file.strip()

    if auth == "token" and not token_file:
        raise ProfileError(
            f"git_remote_profiles[{name}]: token_file is required for auth=\"token\""
        )
    if auth != "token" and token_file:
        raise ProfileError(
            f"git_remote_profiles[{name}]: token_file only applies to auth=\"token\""
        )

    visibility = raw.get("visibility", "private")
    if not isinstance(visibility, str):
        raise ProfileError(
            f"git_remote_profiles[{name}]: visibility must be a string"
        )
    visibility = visibility.strip() or "private"
    if visibility not in VISIBILITY_CHOICES:
        raise ProfileError(
            f"git_remote_profiles[{name}]: visibility must be one of "
            f"{VISIBILITY_CHOICES}, got {visibility!r}"
        )

    return GitRemoteProfile(
        name=name,
        host=host,
        owner=owner,
        auth=auth,
        creds_user=creds_user,
        token_file=token_file,
        visibility=visibility,
    )


def load_profiles(raw_list: object) -> list[GitRemoteProfile]:
    """Parse and validate ``[[git_remote_profiles]]``. ``raw_list`` is
    the raw value read from TOML — a list of dicts, or ``None`` / missing.

    Raises :class:`ProfileError` on the first invalid entry.
    """
    if raw_list is None:
        return []
    if not isinstance(raw_list, list):
        raise ProfileError("git_remote_profiles must be an array of tables")
    profiles: list[GitRemoteProfile] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_list):
        if not isinstance(raw, dict):
            raise ProfileError(f"git_remote_profiles[{i}] must be a table")
        profile = _validate_profile(raw, i, seen)
        seen.add(profile.name)
        profiles.append(profile)
    return profiles


def find_profile(
    profiles: list[GitRemoteProfile], name: str
) -> GitRemoteProfile | None:
    """Return the profile with ``name`` or ``None``."""
    for p in profiles:
        if p.name == name:
            return p
    return None


def resolve_profile_selector(
    profiles: list[GitRemoteProfile],
    selector: str,
    default_name: str,
) -> GitRemoteProfile:
    """Resolve a user-provided selector: an explicit profile name or the
    literal string ``"default"``. Fails with :class:`ProfileError` on
    unknown names or when ``"default"`` is asked for but none is set.
    """
    if selector == "default":
        if not default_name:
            raise ProfileError(
                "no default_git_remote_profile configured; "
                "pass --git-remote <name> instead"
            )
        found = find_profile(profiles, default_name)
        if found is None:
            raise ProfileError(
                f"default_git_remote_profile={default_name!r} does not exist "
                f"in git_remote_profiles"
            )
        return found
    found = find_profile(profiles, selector)
    if found is None:
        names = ", ".join(p.name for p in profiles) or "<none>"
        raise ProfileError(
            f"git remote profile {selector!r} not found; available: {names}"
        )
    return found

"""Typed remote profiles and explicit selection policy."""

import pytest

from uxon.domain import git_profiles as gp


def gh(**changes):
    return dict({"name": "work", "host": "github.com", "owner": "alice", "auth": "gh"}, **changes)


def token(**changes):
    return dict(
        {
            "name": "org",
            "host": "ghe.example.com",
            "owner": "acme",
            "auth": "token",
            "creds_user": "erin",
            "token_file": "/credentials/token",
            "visibility": "public",
        },
        **changes,
    )


def test_profiles_preserve_whitelisted_identity_and_safe_omitted_defaults():
    assert gp.load_profiles(None) == []
    assert gp.load_profiles([]) == []
    assert gp.load_profiles([gh(), token()]) == [
        gp.GitRemoteProfile("work", "github.com", "alice", "gh", "", "", "private"),
        gp.GitRemoteProfile(
            "org", "ghe.example.com", "acme", "token", "erin", "/credentials/token", "public"
        ),
    ]


def test_invalid_profiles_report_independent_field_and_combination_errors(subtests):
    cases = [
        ("shape", {}, "array of tables"),
        ("entry", ["bad"], "must be a table"),
        ("name", [gh(name="")], "missing or empty 'name'"),
        ("owner", [gh(owner="  ")], "missing or empty 'owner'"),
        ("auth", [gh(auth="ssh")], "auth must be one of"),
        ("duplicate", [gh(), gh(owner="bob")], "duplicate name"),
        ("token required", [token(token_file="")], "token_file is required"),
        ("token forbidden", [gh(token_file="/credentials/token")], "token_file only applies"),
        ("visibility", [gh(visibility="secret")], "visibility must be one of"),
    ]
    for name, payload, diagnostic in cases:
        with subtests.test(fault=name), pytest.raises(gp.ProfileError, match=diagnostic):
            gp.load_profiles(payload)


def test_selector_failure_lists_explicit_or_default_policy(subtests):
    profiles = gp.load_profiles([gh(), token()])
    for selector, default, diagnostic in [
        ("default", "", "no default_git_remote_profile"),
        ("default", "missing", "does not exist"),
        ("unknown", "", "available: work, org"),
    ]:
        with (
            subtests.test(selector=selector, default=default),
            pytest.raises(gp.ProfileError, match=diagnostic),
        ):
            gp.resolve_profile_selector(profiles, selector, default)

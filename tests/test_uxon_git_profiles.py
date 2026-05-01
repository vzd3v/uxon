import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import uxon_git_profiles as gp


def _gh(**overrides):
    base = {
        "name": "p",
        "host": "github.com",
        "owner": "vzd3v",
        "auth": "gh",
        "creds_user": "remdepl",
        "visibility": "private",
    }
    base.update(overrides)
    return base


def _tok(**overrides):
    base = {
        "name": "p",
        "host": "github.com",
        "owner": "acme",
        "auth": "token",
        "creds_user": "remdepl",
        "token_file": "/tmp/t",
        "visibility": "private",
    }
    base.update(overrides)
    return base


class LoadProfilesTests(unittest.TestCase):
    def test_empty_returns_empty_list(self) -> None:
        self.assertEqual(gp.load_profiles(None), [])
        self.assertEqual(gp.load_profiles([]), [])

    def test_not_a_list_fails(self) -> None:
        with self.assertRaises(gp.ProfileError):
            gp.load_profiles({"not": "a list"})

    def test_item_not_a_table_fails(self) -> None:
        with self.assertRaises(gp.ProfileError):
            gp.load_profiles(["bad"])

    def test_valid_gh_profile(self) -> None:
        profiles = gp.load_profiles([_gh()])
        self.assertEqual(len(profiles), 1)
        p = profiles[0]
        self.assertEqual(p.name, "p")
        self.assertEqual(p.auth, "gh")
        self.assertEqual(p.token_file, "")
        self.assertEqual(p.visibility, "private")

    def test_valid_token_profile(self) -> None:
        profiles = gp.load_profiles([_tok()])
        self.assertEqual(profiles[0].token_file, "/tmp/t")

    def test_missing_name(self) -> None:
        with self.assertRaisesRegex(gp.ProfileError, "missing or empty 'name'"):
            gp.load_profiles([_gh(name="")])

    def test_missing_owner(self) -> None:
        with self.assertRaisesRegex(gp.ProfileError, "missing or empty 'owner'"):
            gp.load_profiles([_gh(owner="   ")])

    def test_invalid_auth(self) -> None:
        with self.assertRaisesRegex(gp.ProfileError, "auth must be one of"):
            gp.load_profiles([_gh(auth="ssh")])

    def test_duplicate_name(self) -> None:
        with self.assertRaisesRegex(gp.ProfileError, "duplicate name"):
            gp.load_profiles([_gh(name="x"), _gh(name="x", owner="other")])

    def test_token_without_token_file(self) -> None:
        with self.assertRaisesRegex(gp.ProfileError, "token_file is required"):
            gp.load_profiles([_tok(token_file="")])

    def test_token_file_only_for_token_auth(self) -> None:
        with self.assertRaisesRegex(gp.ProfileError, "token_file only applies"):
            gp.load_profiles([_gh(token_file="/tmp/t")])

    def test_invalid_visibility(self) -> None:
        with self.assertRaisesRegex(gp.ProfileError, "visibility must be one of"):
            gp.load_profiles([_gh(visibility="secret")])

    def test_creds_user_default_empty(self) -> None:
        profiles = gp.load_profiles([_gh(creds_user="")])
        self.assertEqual(profiles[0].creds_user, "")

    def test_visibility_default(self) -> None:
        raw = _gh()
        raw.pop("visibility")
        profiles = gp.load_profiles([raw])
        self.assertEqual(profiles[0].visibility, "private")


class UrlsTests(unittest.TestCase):
    def test_ssh_url(self) -> None:
        p = gp.load_profiles([_gh()])[0]
        self.assertEqual(p.ssh_remote_url("foo"), "git@github.com:vzd3v/foo.git")

    def test_https_url(self) -> None:
        p = gp.load_profiles([_gh()])[0]
        self.assertEqual(p.https_remote_url("foo"), "https://github.com/vzd3v/foo.git")

    def test_api_base_github(self) -> None:
        p = gp.load_profiles([_gh()])[0]
        self.assertEqual(p.api_base(), "https://api.github.com")

    def test_api_base_enterprise_fallback(self) -> None:
        p = gp.load_profiles([_gh(host="ghe.example.com")])[0]
        self.assertEqual(p.api_base(), "https://ghe.example.com/api/v3")


class SelectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profiles = gp.load_profiles([_gh(name="a"), _gh(name="b", owner="other")])

    def test_by_name(self) -> None:
        self.assertEqual(gp.resolve_profile_selector(self.profiles, "a", "b").name, "a")

    def test_default_ok(self) -> None:
        self.assertEqual(gp.resolve_profile_selector(self.profiles, "default", "b").name, "b")

    def test_default_not_set(self) -> None:
        with self.assertRaisesRegex(gp.ProfileError, "no default_git_remote_profile"):
            gp.resolve_profile_selector(self.profiles, "default", "")

    def test_default_points_to_missing(self) -> None:
        with self.assertRaisesRegex(gp.ProfileError, "does not exist"):
            gp.resolve_profile_selector(self.profiles, "default", "missing")

    def test_unknown_name(self) -> None:
        with self.assertRaisesRegex(gp.ProfileError, "not found"):
            gp.resolve_profile_selector(self.profiles, "zzz", "")


if __name__ == "__main__":
    unittest.main()

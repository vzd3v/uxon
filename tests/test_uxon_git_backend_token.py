import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import uxon_git_backend_gh as gh
import uxon_git_backend_token as tok
import uxon_git_profiles as gp

SECRET = "ghp_SENSITIVE_DO_NOT_LEAK"


def _profile(**over):
    raw = {
        "name": "p",
        "host": "github.com",
        "owner": "vzd3v",
        "auth": "token",
        "creds_user": "remdepl",
        "token_file": "/tmp/tok",
        "visibility": "private",
    }
    raw.update(over)
    return gp.load_profiles([raw])[0]


class FakeRunner:
    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    def __call__(self, cmd, *, timeout=20):
        self.calls.append(cmd)
        return self._results.pop(0)


class FakeHttp:
    """Tracks (method, url, body) and replays responses. Asserts every
    call carries a non-empty token — raises AssertionError otherwise.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.tokens_seen = []

    def __call__(self, method, url, token, *, body=None, timeout=15):
        if not token:
            raise AssertionError("http called without token")
        self.tokens_seen.append(token)
        self.calls.append((method, url, body))
        return self._responses.pop(0)


def _resp(status=200, payload=None):
    import json

    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    return tok.HttpResponse(status=status, body=body)


def _ok_cat(content: str):
    return gh.RunResult(returncode=0, stdout=content, stderr="")


def _fail_cat(stderr="Permission denied"):
    return gh.RunResult(returncode=1, stdout="", stderr=stderr)


class ReadTokenTests(unittest.TestCase):
    def test_reads_and_strips(self) -> None:
        runner = FakeRunner([_ok_cat(SECRET + "\n")])
        got = tok.read_token("/tmp/t", "remdepl", "devagent", run=runner)
        self.assertEqual(got, SECRET)
        self.assertEqual(
            runner.calls[0],
            ["sudo", "-n", "-u", "remdepl", "--", "cat", "--", "/tmp/t"],
        )

    def test_empty_file_fails(self) -> None:
        runner = FakeRunner([_ok_cat("")])
        with self.assertRaisesRegex(gh.BackendError, "is empty"):
            tok.read_token("/tmp/t", "remdepl", "devagent", run=runner)

    def test_unreadable_file_fails_without_leaking_path(self) -> None:
        runner = FakeRunner([_fail_cat()])
        try:
            tok.read_token("/tmp/t", "remdepl", "devagent", run=runner)
        except gh.BackendError as exc:
            self.assertIn("cannot read", str(exc))
            self.assertIn("/tmp/t", str(exc))  # path itself is fine

    def test_no_sudo_when_same_user(self) -> None:
        runner = FakeRunner([_ok_cat(SECRET)])
        tok.read_token("/tmp/t", "alice", "alice", run=runner)
        self.assertEqual(runner.calls[0], ["cat", "--", "/tmp/t"])


class PreflightTokenTests(unittest.TestCase):
    def test_user_owned_happy_path(self) -> None:
        runner = FakeRunner([_ok_cat(SECRET)])
        http = FakeHttp(
            [
                _resp(200, {"login": "vzd3v"}),  # GET /user
                _resp(404),  # GET /repos/vzd3v/new-repo
            ]
        )
        tok.preflight(_profile(), "new-repo", "remdepl", "devagent", run=runner, http=http)
        self.assertEqual(len(http.calls), 2)
        self.assertEqual(http.tokens_seen, [SECRET, SECRET])

    def test_org_owned_requires_org_in_list(self) -> None:
        runner = FakeRunner([_ok_cat(SECRET)])
        http = FakeHttp(
            [
                _resp(200, {"login": "vzd3v"}),  # not the owner
                _resp(200, [{"login": "acme"}]),  # orgs includes acme
                _resp(404),  # repo doesn't exist
            ]
        )
        tok.preflight(_profile(owner="acme"), "r", "remdepl", "devagent", run=runner, http=http)

    def test_owner_not_in_orgs_fails(self) -> None:
        runner = FakeRunner([_ok_cat(SECRET)])
        http = FakeHttp(
            [
                _resp(200, {"login": "vzd3v"}),
                _resp(200, [{"login": "other-org"}]),
            ]
        )
        with self.assertRaisesRegex(gh.BackendError, "cannot create repos under owner"):
            tok.preflight(_profile(owner="acme"), "r", "remdepl", "devagent", run=runner, http=http)

    def test_existing_repo_fails(self) -> None:
        runner = FakeRunner([_ok_cat(SECRET)])
        http = FakeHttp(
            [
                _resp(200, {"login": "vzd3v"}),
                _resp(200, {"name": "r"}),
            ]
        )
        with self.assertRaisesRegex(gh.BackendError, "already exists"):
            tok.preflight(_profile(), "r", "remdepl", "devagent", run=runner, http=http)

    def test_bad_token_rejected(self) -> None:
        runner = FakeRunner([_ok_cat("deadbeef")])
        http = FakeHttp(
            [
                _resp(401, {"message": "Bad credentials"}),
            ]
        )
        try:
            tok.preflight(_profile(), "r", "remdepl", "devagent", run=runner, http=http)
        except gh.BackendError as exc:
            self.assertIn("token rejected", str(exc))
            self.assertIn("Bad credentials", str(exc))
            self.assertNotIn("deadbeef", str(exc))
            self.assertNotIn("deadbeef", repr(exc))


class CreateRemoteTokenTests(unittest.TestCase):
    def test_user_repo_endpoint(self) -> None:
        runner = FakeRunner([_ok_cat(SECRET)])
        http = FakeHttp(
            [
                _resp(200, {"login": "vzd3v"}),
                _resp(
                    201,
                    {"ssh_url": "git@github.com:vzd3v/r.git"},
                ),
            ]
        )
        url = tok.create_remote(
            _profile(), "r", "/tmp/r", "remdepl", "devagent", run=runner, http=http
        )
        self.assertEqual(url, "git@github.com:vzd3v/r.git")
        _, post_url, body = http.calls[1]
        self.assertEqual(post_url, "https://api.github.com/user/repos")
        self.assertEqual(body["private"], True)
        self.assertEqual(body["name"], "r")

    def test_org_repo_endpoint(self) -> None:
        runner = FakeRunner([_ok_cat(SECRET)])
        http = FakeHttp(
            [
                _resp(200, {"login": "vzd3v"}),
                _resp(
                    201,
                    {"ssh_url": "git@github.com:acme/r.git"},
                ),
            ]
        )
        tok.create_remote(
            _profile(owner="acme"),
            "r",
            "/tmp/r",
            "remdepl",
            "devagent",
            run=runner,
            http=http,
        )
        _, post_url, _ = http.calls[1]
        self.assertEqual(post_url, "https://api.github.com/orgs/acme/repos")

    def test_public_visibility_goes_private_false(self) -> None:
        runner = FakeRunner([_ok_cat(SECRET)])
        http = FakeHttp(
            [
                _resp(200, {"login": "vzd3v"}),
                _resp(201, {"ssh_url": "git@github.com:vzd3v/r.git"}),
            ]
        )
        tok.create_remote(
            _profile(visibility="public"),
            "r",
            "/tmp/r",
            "remdepl",
            "devagent",
            run=runner,
            http=http,
        )
        _, _, body = http.calls[1]
        self.assertFalse(body["private"])

    def test_create_fail_includes_message(self) -> None:
        runner = FakeRunner([_ok_cat(SECRET)])
        http = FakeHttp(
            [
                _resp(200, {"login": "vzd3v"}),
                _resp(422, {"message": "name already exists on this account"}),
            ]
        )
        with self.assertRaisesRegex(gh.BackendError, "failed to create"):
            tok.create_remote(
                _profile(), "r", "/tmp/r", "remdepl", "devagent", run=runner, http=http
            )

    def test_dry_run_reads_no_token(self) -> None:
        runner = FakeRunner([])  # would blow up if called
        http = FakeHttp([])
        url = tok.create_remote(
            _profile(),
            "r",
            "/tmp/r",
            "remdepl",
            "devagent",
            dry_run=True,
            run=runner,
            http=http,
        )
        self.assertEqual(url, "git@github.com:vzd3v/r.git")
        self.assertEqual(runner.calls, [])
        self.assertEqual(http.calls, [])


class NetworkErrorTests(unittest.TestCase):
    def test_url_error_in_default_http_becomes_backend_error(self) -> None:
        import unittest.mock
        import urllib.error

        with unittest.mock.patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("Network is unreachable"),
        ):
            try:
                tok.default_http("GET", "https://api.github.com/user", SECRET)
            except gh.BackendError as exc:
                self.assertNotIn(SECRET, str(exc))
                self.assertNotIn(SECRET, repr(exc))
                self.assertIn("network error", str(exc).lower())
            else:
                self.fail("expected BackendError")


class DescribeCommandTests(unittest.TestCase):
    def test_no_token_in_output(self) -> None:
        s = tok.describe_command(_profile(), "r")
        self.assertIn("***", s)
        self.assertNotIn(SECRET, s)
        self.assertIn("name=r", s)
        self.assertIn("vzd3v", s)


if __name__ == "__main__":
    unittest.main()

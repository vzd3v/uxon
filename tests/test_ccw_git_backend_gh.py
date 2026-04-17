import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import ccw_git_backend_gh as gh
import ccw_git_profiles as gp


def _profile(**over):
    raw = {
        "name": "p",
        "host": "github.com",
        "owner": "vzd3v",
        "auth": "gh",
        "creds_user": "remdepl",
        "visibility": "private",
    }
    raw.update(over)
    return gp.load_profiles([raw])[0]


class FakeRunner:
    """Replays a scripted sequence of run() results, recording calls."""

    def __init__(self, results):
        self._results = list(results)
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *, timeout=20):
        self.calls.append(cmd)
        if not self._results:
            raise AssertionError(f"unexpected extra run call: {cmd}")
        return self._results.pop(0)


def _ok(stdout="") -> gh.RunResult:
    return gh.RunResult(returncode=0, stdout=stdout, stderr="")


def _fail(stderr="boom", rc=1) -> gh.RunResult:
    return gh.RunResult(returncode=rc, stdout="", stderr=stderr)


class SudoPrefixTests(unittest.TestCase):
    def test_no_prefix_when_same_user(self) -> None:
        self.assertEqual(gh.sudo_prefix("a", "a"), [])

    def test_no_prefix_when_empty_creds_user(self) -> None:
        self.assertEqual(gh.sudo_prefix("", "a"), [])

    def test_prefix_when_different(self) -> None:
        self.assertEqual(
            gh.sudo_prefix("remdepl", "devagent"),
            ["sudo", "-n", "-u", "remdepl", "--"],
        )


class PreflightTests(unittest.TestCase):
    def test_happy_path(self) -> None:
        # which gh OK; gh auth status OK; gh repo view fails (repo absent)
        runner = FakeRunner([_ok("/usr/bin/gh\n"), _ok(), _fail()])
        gh.preflight(_profile(), "new-repo", "remdepl", "devagent", run=runner)
        self.assertEqual(len(runner.calls), 3)
        # First call checks via sh -c because `command -v` is a builtin.
        self.assertIn("command -v gh", " ".join(runner.calls[0]))

    def test_gh_missing_fails(self) -> None:
        runner = FakeRunner([_ok("")])  # which returns empty stdout
        with self.assertRaisesRegex(gh.BackendError, "gh CLI not found"):
            gh.preflight(_profile(), "r", "remdepl", "devagent", run=runner)

    def test_auth_status_failure_bubbles_host(self) -> None:
        runner = FakeRunner([_ok("/usr/bin/gh"), _fail("not logged in")])
        with self.assertRaisesRegex(gh.BackendError, "not logged in to github.com"):
            gh.preflight(_profile(), "r", "remdepl", "devagent", run=runner)

    def test_existing_repo_fails(self) -> None:
        runner = FakeRunner([_ok("/usr/bin/gh"), _ok(), _ok('{"name":"r"}')])
        with self.assertRaisesRegex(gh.BackendError, "already exists"):
            gh.preflight(_profile(), "r", "remdepl", "devagent", run=runner)


class CreateRemoteTests(unittest.TestCase):
    def test_dry_run_emits_no_calls(self) -> None:
        runner = FakeRunner([])
        url = gh.create_remote(
            _profile(), "r", "/tmp/r", "remdepl", "devagent", dry_run=True, run=runner
        )
        self.assertEqual(runner.calls, [])
        self.assertEqual(url, "git@github.com:vzd3v/r.git")

    def test_success_returns_ssh_url(self) -> None:
        runner = FakeRunner([_ok("Created")])
        url = gh.create_remote(
            _profile(), "r", "/tmp/r", "remdepl", "devagent", run=runner
        )
        self.assertEqual(url, "git@github.com:vzd3v/r.git")
        cmd = runner.calls[0]
        self.assertIn("--private", cmd)
        self.assertNotIn("--source", cmd)
        self.assertNotIn("--push", cmd)
        self.assertIn("vzd3v/r", cmd)
        self.assertEqual(cmd[:5], ["sudo", "-n", "-u", "remdepl", "--"])

    def test_public_visibility(self) -> None:
        runner = FakeRunner([_ok()])
        gh.create_remote(
            _profile(visibility="public"),
            "r",
            "/tmp/r",
            "remdepl",
            "devagent",
            run=runner,
        )
        self.assertIn("--public", runner.calls[0])

    def test_failure_includes_stderr(self) -> None:
        runner = FakeRunner([_fail("422 name already taken")])
        with self.assertRaisesRegex(gh.BackendError, "gh repo create failed"):
            gh.create_remote(
                _profile(), "r", "/tmp/r", "remdepl", "devagent", run=runner
            )


class DescribeCommandTests(unittest.TestCase):
    def test_matches_actual_invocation(self) -> None:
        desc = gh.describe_command(_profile(), "r", "/tmp/r", "remdepl", "devagent")
        self.assertEqual(desc[:5], ["sudo", "-n", "-u", "remdepl", "--"])
        self.assertIn("gh", desc)
        self.assertIn("vzd3v/r", desc)


if __name__ == "__main__":
    unittest.main()

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import ccw_git_backend_gh as gh
import ccw_git_backend_token as tok
import ccw_git_create as orch
import ccw_git_profiles as gp

SECRET = "ghp_SENSITIVE"


def _gh_profile(**over):
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


def _tok_profile(**over):
    raw = {
        "name": "p",
        "host": "github.com",
        "owner": "vzd3v",
        "auth": "token",
        "creds_user": "remdepl",
        "token_file": "/tmp/t",
        "visibility": "private",
    }
    raw.update(over)
    return gp.load_profiles([raw])[0]


class ScriptedRunner:
    """Returns results from a mapping of command-keyword→result. Raises
    for unknown commands to catch drift between orchestrator and tests.
    """

    def __init__(self, mapping):
        self.mapping = mapping  # list of (predicate, result) in order
        self.calls = []

    def __call__(self, cmd, *, timeout=20):
        self.calls.append(cmd)
        joined = " ".join(cmd)
        for key, value in self.mapping:
            if key in joined:
                return value() if callable(value) else value
        return gh.RunResult(returncode=0, stdout="", stderr="")


def _ok(stdout="") -> gh.RunResult:
    return gh.RunResult(returncode=0, stdout=stdout, stderr="")


def _fail(stderr="x", rc=1) -> gh.RunResult:
    return gh.RunResult(returncode=rc, stdout="", stderr=stderr)


class FakeHttp:
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


class GhHappyPathTests(unittest.TestCase):
    def test_creates_remote_and_sets_origin(self) -> None:
        runner = ScriptedRunner(
            [
                # git dir absence probe
                ("test -d", _fail()),
                # which gh
                ("command -v gh", _ok("/usr/bin/gh")),
                # gh auth status
                ("auth status", _ok()),
                # gh repo view → not found
                ("repo view", _fail()),
                # local init + commit
                ("init -b main", _ok()),
                ("commit --allow-empty", _ok()),
                # gh repo create (no --source/--push)
                ("repo create", _ok()),
                # remote add + push (always under launch_user)
                ("remote add", _ok()),
                ("push -u origin", _ok()),
            ]
        )
        result = orch.create_project_remote(
            _gh_profile(),
            "demo",
            "/tmp/demo",
            launch_user="devagent",
            current_user="devagent",
            run=runner,
        )
        self.assertEqual(result.ssh_url, "git@github.com:vzd3v/demo.git")
        joined = [" ".join(c) for c in runner.calls]
        self.assertTrue(any("init -b main" in j for j in joined))
        self.assertTrue(any("remote add" in j for j in joined))
        self.assertTrue(any("push -u origin" in j for j in joined))
        self.assertTrue(any("gh repo create" in j for j in joined))

    def test_existing_dot_git_refuses(self) -> None:
        runner = ScriptedRunner([("test -d", _ok())])
        with self.assertRaisesRegex(orch.CreationError, "already exists; refusing"):
            orch.create_project_remote(
                _gh_profile(),
                "demo",
                "/tmp/demo",
                launch_user="devagent",
                current_user="devagent",
                run=runner,
            )

    def test_preflight_failure_stops_before_local_init(self) -> None:
        runner = ScriptedRunner(
            [
                ("test -d", _fail()),
                ("command -v gh", _ok("/usr/bin/gh")),
                ("auth status", _fail("not logged in")),
            ]
        )
        with self.assertRaises(orch.CreationError) as ctx:
            orch.create_project_remote(
                _gh_profile(),
                "demo",
                "/tmp/demo",
                launch_user="devagent",
                current_user="devagent",
                run=runner,
            )
        self.assertEqual(ctx.exception.stage, "preflight")
        joined = [" ".join(c) for c in runner.calls]
        self.assertFalse(any("init -b main" in j for j in joined))


class TokenHappyPathTests(unittest.TestCase):
    def test_creates_remote_and_pushes(self) -> None:
        runner = ScriptedRunner(
            [
                ("test -d", _fail()),
                # preflight: cat token
                ("cat --", _ok(SECRET)),
                # local init + commit
                ("init -b main", _ok()),
                ("commit --allow-empty", _ok()),
                # create_remote: cat token again
                ("cat --", _ok(SECRET)),
                # remote add + push
                ("remote add", _ok()),
                ("push -u origin", _ok()),
            ]
        )
        http = FakeHttp(
            [
                # preflight
                _resp(200, {"login": "vzd3v"}),  # GET /user
                _resp(404),  # GET /repos
                # create
                _resp(200, {"login": "vzd3v"}),  # GET /user again
                _resp(201, {"ssh_url": "git@github.com:vzd3v/demo.git"}),
            ]
        )
        result = orch.create_project_remote(
            _tok_profile(),
            "demo",
            "/tmp/demo",
            launch_user="devagent",
            current_user="devagent",
            run=runner,
            http=http,
        )
        self.assertEqual(result.ssh_url, "git@github.com:vzd3v/demo.git")
        # Token should not surface in any captured command arg.
        for cmd in runner.calls:
            joined = " ".join(cmd)
            self.assertNotIn(SECRET, joined)


class DryRunTests(unittest.TestCase):
    def test_gh_dry_run_runs_preflight_only(self) -> None:
        runner = ScriptedRunner(
            [
                ("test -d", _fail()),
                ("command -v gh", _ok("/usr/bin/gh")),
                ("auth status", _ok()),
                ("repo view", _fail()),
            ]
        )
        result = orch.create_project_remote(
            _gh_profile(),
            "demo",
            "/tmp/demo",
            launch_user="devagent",
            current_user="devagent",
            dry_run=True,
            run=runner,
        )
        joined = " | ".join(result.commands)
        self.assertIn("init -b main", joined)
        self.assertIn("gh repo create", joined)
        # No actual init should have been executed.
        exec_joined = [" ".join(c) for c in runner.calls]
        self.assertFalse(any("init -b main" in j for j in exec_joined))

    def test_token_dry_run_hides_token(self) -> None:
        runner = ScriptedRunner(
            [
                ("test -d", _fail()),
                ("cat --", _ok(SECRET)),
            ]
        )
        http = FakeHttp(
            [
                _resp(200, {"login": "vzd3v"}),
                _resp(404),
            ]
        )
        result = orch.create_project_remote(
            _tok_profile(),
            "demo",
            "/tmp/demo",
            launch_user="devagent",
            current_user="devagent",
            dry_run=True,
            run=runner,
            http=http,
        )
        for cmd in result.commands:
            self.assertNotIn(SECRET, cmd)
        self.assertTrue(any("***" in c for c in result.commands))


class ResolveCredsUserTests(unittest.TestCase):
    def test_falls_back_to_launch_user(self) -> None:
        p = _gh_profile(creds_user="")
        self.assertEqual(orch.resolve_creds_user(p, "devagent"), "devagent")

    def test_keeps_explicit_creds_user(self) -> None:
        p = _gh_profile(creds_user="remdepl")
        self.assertEqual(orch.resolve_creds_user(p, "devagent"), "remdepl")


if __name__ == "__main__":
    unittest.main()

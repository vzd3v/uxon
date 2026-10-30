"""Remote creation protocols: exact commands, credentials and failure cutoffs."""

import dataclasses
import json
import os
import pwd
import shlex
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from helpers import make_config

from uxon.domain.git_profiles import GitRemoteProfile
from uxon.gitremote import backend_gh as gh
from uxon.gitremote import backend_token as tok
from uxon.gitremote import create

SECRET = "sentinel-private-token"


class CommandProtocol:
    """Consume full argv/deadline pairs; reject unspecified commands."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def __call__(self, argv, *, timeout=20):
        assert self.steps, f"unexpected command: {argv}"
        expected, deadline, result = self.steps.pop(0)
        assert (list(argv), timeout) == (expected, deadline)
        self.calls.append(list(argv))
        return result() if callable(result) else result

    def exhausted(self):
        assert self.steps == []


class HttpProtocol:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def __call__(self, method, url, token, *, body=None, timeout=15):
        assert (token, timeout) == (SECRET, 15)
        assert self.steps, f"unexpected HTTP: {method} {url}"
        expected, response = self.steps.pop(0)
        assert (method, url, body) == expected
        self.calls.append(expected)
        return response

    def exhausted(self):
        assert self.steps == []


def response(status, payload=None):
    return tok.HttpResponse(status, json.dumps(payload).encode() if payload is not None else b"")


def result(rc=0, stdout="", stderr=""):
    return gh.RunResult(rc, stdout, stderr)


class RemoteCreationTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.project = str(Path(directory.name) / "project")
        self.token = Path(directory.name) / "token"
        self.token.write_text(" \n" + SECRET + "\n ")
        self.cfg = make_config()
        self.launch = ["/usr/bin/sudo", "-H", "-u", "dana_agent", "--"]
        self.creds = ["/usr/bin/sudo", "-n", "-H", "-u", "erin", "--"]

    def profile(self, auth="gh", **changes):
        return dataclasses.replace(
            GitRemoteProfile(
                "work",
                "github.com",
                "alice",
                auth,
                "erin",
                str(self.token) if auth == "token" else "",
                "private",
            ),
            **changes,
        )

    def local(self, *argv):
        return self.launch + ["git", "-C", self.project, *argv]

    def existence(self):
        return (self.launch + ["test", "-d", self.project + "/.git"], 20, result(1))

    def init_steps(self):
        return [
            (self.local("init", "-b", "main"), 60, result()),
            (self.local("commit", "--allow-empty", "-m", "init"), 60, result()),
        ]

    def final_steps(self, url):
        return [
            (self.local("remote", "add", "origin", url), 60, result()),
            (self.local("push", "-u", "origin", "main"), 60, result()),
        ]

    def gh_preflight(self, host="github.com"):
        return [
            self.existence(),
            (self.creds + ["sh", "-c", "command -v gh"], 20, result(stdout="/usr/bin/gh\n")),
            (self.creds + ["gh", "auth", "status", "--hostname", host], 20, result()),
            (
                self.creds + ["gh", "repo", "view", f"{host}/alice/demo", "--json", "name"],
                20,
                result(1),
            ),
        ]

    def cat(self, prefix=None):
        return (
            (self.creds if prefix is None else prefix) + ["cat", "--", str(self.token)],
            20,
            lambda: result(stdout=self.token.read_text()),
        )

    def invoke(self, profile, runner, *, http=None, dry_run=False):
        return create.create_project_remote(
            self.cfg,
            profile,
            "demo",
            self.project,
            "dana_agent",
            "dana_agent",
            run=runner,
            http=http,
            dry_run=dry_run,
        )

    def test_gh_private_pipeline_and_dry_run_agree_on_all_intended_commands(self):
        from unittest.mock import patch

        current_user = pwd.getpwuid(os.getuid()).pw_name
        cross_user_prefix = list(self.creds)
        for host, credential_user in (
            ("github.com", "erin"),
            ("git.example.com", "erin"),
            ("github.com", current_user),
        ):
            self.creds = [] if credential_user == current_user else cross_user_prefix
            with (
                self.subTest(host=host, same_process_credentials=credential_user == current_user),
                patch.dict("os.environ", {"GH_HOST": "wrong.example.com"}),
            ):
                url = f"git@{host}:alice/demo.git"
                mutations = (
                    self.init_steps()
                    + [
                        (
                            self.creds
                            + ["gh", "repo", "create", f"{host}/alice/demo", "--private"],
                            60,
                            result(),
                        )
                    ]
                    + self.final_steps(url)
                )
                runner = CommandProtocol(self.gh_preflight(host) + mutations)
                actual = self.invoke(self.profile(host=host, creds_user=credential_user), runner)
                runner.exhausted()
                self.assertEqual((actual.profile_name, actual.ssh_url), ("work", url))
                self.assertEqual(
                    actual.commands,
                    [shlex.join(step[0]) for step in self.init_steps() + self.final_steps(url)],
                )
                dry = CommandProtocol(self.gh_preflight(host))
                described = self.invoke(
                    self.profile(host=host, creds_user=credential_user), dry, dry_run=True
                )
                dry.exhausted()
                self.assertEqual(described.commands, [shlex.join(step[0]) for step in mutations])
                self.assertFalse(Path(self.project).exists())
                refused = self.gh_preflight(host)
                refused[-1] = (refused[-1][0], 20, result())
                failed = CommandProtocol(refused)
                with self.assertRaises(create.CreationError) as caught:
                    self.invoke(
                        self.profile(host=host, creds_user=credential_user), failed, dry_run=True
                    )
                self.assertEqual(caught.exception.stage, "preflight")
                self.assertIn("already exists", str(caught.exception))
                failed.exhausted()

    def test_public_gh_creation_and_git_push_use_effective_launch_credentials(self):
        self.creds = ["/usr/bin/sudo", "-n", "-H", "-u", "dana_agent", "--"]
        url = "git@github.com:alice/demo.git"
        runner = CommandProtocol(
            self.gh_preflight()
            + self.init_steps()
            + [
                (
                    self.creds + ["gh", "repo", "create", "github.com/alice/demo", "--public"],
                    60,
                    result(),
                )
            ]
            + self.final_steps(url)
        )
        self.assertEqual(
            self.invoke(self.profile(creds_user="", visibility="public"), runner).ssh_url, url
        )
        runner.exhausted()

    def test_token_personal_private_and_enterprise_org_public_pipelines(self):
        current_user = pwd.getpwuid(os.getuid()).pw_name
        for owner, host, visibility, creds_user in [
            ("alice", "github.com", "private", "erin"),
            ("acme", "ghe.example.com", "public", ""),
            ("Alice", "github.com", "public", current_user),
        ]:
            with self.subTest(
                owner=owner, host=host, same_process_credentials=creds_user == current_user
            ):
                if creds_user == current_user:
                    prefix = []
                else:
                    prefix = (
                        self.creds
                        if creds_user
                        else ["/usr/bin/sudo", "-n", "-H", "-u", "dana_agent", "--"]
                    )
                base = (
                    "https://api.github.com"
                    if host == "github.com"
                    else "https://ghe.example.com/api/v3"
                )
                url = f"git@{host}:{owner}/demo.git"
                preflight = [(("GET", base + "/user", None), response(200, {"login": "alice"}))]
                preflight.append((("GET", f"{base}/repos/{owner}/demo", None), response(404)))
                endpoint = "/user/repos" if owner.casefold() == "alice" else "/orgs/acme/repos"
                payload = {"name": "demo", "private": visibility == "private", "auto_init": False}
                http = HttpProtocol(
                    preflight
                    + [
                        (("GET", base + "/user", None), response(200, {"login": "alice"})),
                        (("POST", base + endpoint, payload), response(201, {"ssh_url": url})),
                    ]
                )
                runner = CommandProtocol(
                    [self.existence(), self.cat(prefix)]
                    + self.init_steps()
                    + [self.cat(prefix)]
                    + self.final_steps(url)
                )
                profile = self.profile(
                    "token", owner=owner, host=host, visibility=visibility, creds_user=creds_user
                )
                actual = self.invoke(profile, runner, http=http)
                runner.exhausted()
                http.exhausted()
                self.assertEqual(actual.ssh_url, url)
                self.assertNotIn(SECRET, repr((runner.calls, actual)))
                dry_runner = CommandProtocol([self.existence(), self.cat(prefix)])
                dry_http = HttpProtocol(preflight)
                dry = self.invoke(profile, dry_runner, http=dry_http, dry_run=True)
                dry_runner.exhausted()
                dry_http.exhausted()
                self.assertEqual(
                    dry.commands[:2] + dry.commands[3:],
                    [shlex.join(step[0]) for step in self.init_steps() + self.final_steps(url)],
                )
                self.assertEqual(
                    dry.commands[2],
                    f"POST {base}/user/repos (or /orgs/{owner}/repos)  name=demo private={visibility == 'private'}  Authorization: Bearer ***",
                )
                self.assertNotIn(SECRET, repr(dry))
                self.assertFalse(Path(self.project).exists())

    def test_gh_preflight_and_each_mutation_failure_stop_at_exact_stage(self):
        url = "git@github.com:alice/demo.git"
        all_steps = (
            self.gh_preflight()
            + self.init_steps()
            + [
                (
                    self.creds + ["gh", "repo", "create", "github.com/alice/demo", "--private"],
                    60,
                    result(),
                )
            ]
            + self.final_steps(url)
        )
        faults = [
            (0, "local_init", "already exists; refusing", result()),
            (1, "preflight", "gh CLI not found", result(stdout="")),
            (2, "preflight", "auth-sentinel", result(1, stderr="auth-sentinel")),
            (3, "preflight", "already exists", result()),
            (4, "local_init", "init-sentinel", result(1, stderr="init-sentinel")),
            (5, "local_init", "commit-sentinel", result(1, stderr="commit-sentinel")),
            (6, "remote_create", "create-sentinel-422", result(1, stderr="create-sentinel-422")),
            (7, "remote_config", "origin-sentinel", result(1, stderr="origin-sentinel")),
            (8, "push", "push-sentinel", result(1, stderr="push-sentinel")),
        ]
        for index, stage, diagnostic, failed in faults:
            with self.subTest(stage=stage, command=index):
                steps = all_steps[:index] + [(all_steps[index][0], all_steps[index][1], failed)]
                runner = CommandProtocol(steps)
                with self.assertRaises(create.CreationError) as caught:
                    self.invoke(self.profile(), runner)
                self.assertEqual(caught.exception.stage, stage)
                self.assertIn(diagnostic, str(caught.exception))
                runner.exhausted()
                self.assertEqual(runner.calls, [step[0] for step in steps])

    def test_token_preflight_faults_require_failure_and_no_local_mutation(self):
        base = "https://api.github.com"
        cases = [
            ("empty", "alice", [], "is empty"),
            ("unreadable", "alice", [], "cannot read"),
            (
                "rejected",
                "alice",
                [
                    (
                        ("GET", base + "/user", None),
                        response(401, {"message": "bad-credential-sentinel " + SECRET}),
                    )
                ],
                "bad-credential-sentinel",
            ),
            (
                "bad-login",
                "alice",
                [(("GET", base + "/user", None), response(200, []))],
                "unexpected response",
            ),
            (
                "exists",
                "alice",
                [
                    (("GET", base + "/user", None), response(200, {"login": "alice"})),
                    (("GET", base + "/repos/alice/demo", None), response(200)),
                ],
                "already exists",
            ),
        ]
        for fault, owner, requests, diagnostic in cases:
            with self.subTest(fault=fault):
                self.token.write_text("\n" if fault == "empty" else SECRET)
                cat = self.cat()
                if fault == "unreadable":
                    cat = (cat[0], 20, result(1, stderr="permission-sentinel"))
                runner = CommandProtocol([self.existence(), cat])
                http = HttpProtocol(requests)
                with self.assertRaises(create.CreationError) as caught:
                    self.invoke(self.profile("token", owner=owner), runner, http=http)
                self.assertEqual(caught.exception.stage, "preflight")
                self.assertIn(diagnostic, str(caught.exception))
                self.assertNotIn(SECRET, repr(caught.exception))
                if fault == "unreadable":
                    self.assertIn(str(self.token), str(caught.exception))
                runner.exhausted()
                http.exhausted()
                self.assertFalse(Path(self.project).exists())

    def test_token_remote_failure_preserves_detail_and_stops_before_origin(self):
        base = "https://api.github.com"
        for owner, endpoint, status in [
            ("alice", "/user/repos", 422),
            ("engineering", "/orgs/engineering/repos", 403),
        ]:
            with self.subTest(owner=owner):
                self._assert_token_create_failure(base, owner, endpoint, status)

    def _assert_token_create_failure(self, base, owner, endpoint, status):
        runner = CommandProtocol([self.existence(), self.cat()] + self.init_steps() + [self.cat()])
        http = HttpProtocol(
            [
                (("GET", base + "/user", None), response(200, {"login": "alice"})),
                (("GET", base + f"/repos/{owner}/demo", None), response(404)),
                (("GET", base + "/user", None), response(200, {"login": "alice"})),
                (
                    (
                        "POST",
                        base + endpoint,
                        {"name": "demo", "private": True, "auto_init": False},
                    ),
                    response(status, {"message": "unique-create-sentinel " + SECRET}),
                ),
            ]
        )
        with self.assertRaises(create.CreationError) as caught:
            self.invoke(self.profile("token", owner=owner), runner, http=http)
        self.assertEqual(caught.exception.stage, "remote_create")
        self.assertIn(f"{status}: unique-create-sentinel ***", str(caught.exception))
        self.assertNotIn(SECRET, repr(caught.exception))
        runner.exhausted()
        http.exhausted()

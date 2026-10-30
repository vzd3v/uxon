import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from dataclasses import asdict
from pathlib import Path
from unittest import mock

from helpers import make_config

from uxon.domain import git_profiles as gp
from uxon.domain.execution import ExecutionBackendSpec, ExecutionConfig
from uxon.gitremote import backend_gh as gh
from uxon.gitremote import backend_token as tok
from uxon.gitremote import token_worker

SECRET = "ghp_SENSITIVE_DO_NOT_LEAK"


def _command_cfg():
    backend = ExecutionBackendSpec(
        id="boundary",
        kind="command",
        command_prefix=("/usr/local/libexec/boundary", "--target", "{user}"),
    )
    return make_config(
        execution=ExecutionConfig(
            default_backend="boundary",
            backends={"local": ExecutionConfig().backends["local"], "boundary": backend},
        )
    )


def _profile(**over):
    raw = {
        "name": "p",
        "host": "github.com",
        "owner": "alice",
        "auth": "token",
        "creds_user": "erin",
        "token_file": "/tmp/tok",
        "visibility": "private",
    }
    raw.update(over)
    return gp.load_profiles([raw])[0]


class NetworkErrorTests(unittest.TestCase):
    def test_url_error_in_default_http_becomes_backend_error(self) -> None:
        import unittest.mock
        import urllib.error

        with unittest.mock.patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("Network is unreachable " + SECRET),
        ):
            try:
                tok.default_http("GET", "https://api.github.com/user", SECRET)
            except gh.BackendError as exc:
                self.assertNotIn(SECRET, str(exc))
                self.assertNotIn(SECRET, repr(exc))
                self.assertIn("network error", str(exc).lower())
            else:
                self.fail("expected BackendError")


class CommandBackendWorkerTests(unittest.TestCase):
    def test_worker_reports_empty_token_as_structured_preflight_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token"
            token_file.write_text("\n")
            request = {
                "operation": "preflight",
                "profile": asdict(_profile(token_file=str(token_file))),
                "repo_name": "demo",
            }
            result = subprocess.run(
                [sys.executable, "-m", "uxon.gitremote.token_worker"],
                input=json.dumps(request),
                text=True,
                capture_output=True,
                timeout=5,
            )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            json.loads(result.stdout),
            {"ok": False, "stage": "preflight", "error": "token file is empty"},
        )

    def test_controller_sends_no_token_to_worker_argv_env_or_stdin(self) -> None:
        cfg = _command_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token"
            token_file.write_text(SECRET)
            profile = _profile(token_file=str(token_file))
            calls = []

            def worker(argv, **kwargs):
                self.assertEqual(
                    argv,
                    [
                        "/usr/local/libexec/boundary",
                        "--target",
                        "erin",
                        sys.executable,
                        "-m",
                        "uxon.gitremote.token_worker",
                    ],
                )
                self.assertEqual(set(kwargs), {"input", "timeout"})
                self.assertEqual(kwargs["timeout"], 30)
                request = json.loads(kwargs["input"])
                self.assertEqual(
                    request,
                    {
                        "operation": "create",
                        "profile": {
                            "name": "p",
                            "host": "github.com",
                            "owner": "alice",
                            "auth": "token",
                            "creds_user": "erin",
                            "token_file": str(token_file),
                            "visibility": "private",
                        },
                        "repo_name": "r",
                    },
                )
                self.assertNotIn(SECRET, repr((argv, kwargs)))
                calls.append(request)
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps({"ok": True, "ssh_url": "git@github.com:alice/r.git"}), ""
                )

            with (
                mock.patch("uxon.gitremote.backend_token.run_query", side_effect=worker),
                mock.patch.object(
                    Path, "read_text", side_effect=AssertionError("controller credential read")
                ),
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("controller HTTP")),
            ):
                url = tok.create_remote(cfg, profile, "r", "/tmp/r", "erin", "dana_agent")
            self.assertEqual(url, "git@github.com:alice/r.git")
            self.assertEqual(len(calls), 1)
            self.assertEqual(token_file.read_text(), SECRET)

    def test_worker_redacts_token_echoed_by_http_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token"
            token_file.write_text(SECRET)
            request = {
                "operation": "preflight",
                "profile": {
                    "name": "p",
                    "host": "github.com",
                    "owner": "alice",
                    "auth": "token",
                    "creds_user": "erin",
                    "token_file": str(token_file),
                    "visibility": "private",
                },
                "repo_name": "r",
            }

            def rejected_http(request, *, timeout):
                self.assertEqual(timeout, 15)
                self.assertEqual(request.full_url, "https://api.github.com/user")
                self.assertEqual(request.get_header("Authorization"), "Bearer " + SECRET)
                raise urllib.error.URLError(f"upstream echoed {SECRET}")

            with mock.patch("urllib.request.urlopen", side_effect=rejected_http):
                response = token_worker.execute(request)
        self.assertFalse(response["ok"])
        self.assertNotIn(SECRET, json.dumps(response))
        self.assertIn("***", response["error"])
        self.assertEqual(response["stage"], "preflight")
        self.assertIn("upstream echoed", response["error"])

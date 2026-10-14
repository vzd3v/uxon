"""Strict external command responses for synthetic target-user trees.

These responses exercise attestation and path policy, without claiming real
UID or namespace isolation. Real-user integration tests own that proof.
"""

from __future__ import annotations

import contextlib
import io
import json
import math
import shlex
import subprocess
from types import SimpleNamespace
from unittest import mock

from helpers import make_config

from uxon.domain.execution import ExecutionBackendSpec, ExecutionConfig
from uxon.infra import path_probe


def target_config(**overrides):
    return make_config(
        execution=ExecutionConfig(
            default_backend="boundary",
            backends={
                "boundary": ExecutionBackendSpec(
                    id="boundary",
                    kind="command",
                    command_prefix=("boundary", "{user}", "--"),
                    probe_timeout_seconds=1.25,
                )
            },
        ),
        **overrides,
    )


class TargetCommands:
    def __init__(
        self, respond=None, *, installed=("tmux", "claude", "codex", "cursor-agent"), denied=()
    ):
        self.respond = respond
        self.installed = set(installed)
        self.calls = []
        self.errors = []
        self.denied = set(denied)

    def __call__(self, argv, **kwargs):
        try:
            return self._run(argv, **kwargs)
        except AssertionError as error:
            self.errors.append(str(error))
            raise

    def _run(self, argv, **kwargs):
        argv = list(argv)
        assert argv[:1] == ["boundary"] and argv[2:3] == ["--"], argv
        assert argv.count("boundary") == 1, argv
        user, command = argv[1], argv[3:]
        assert kwargs.get("start_new_session") is True
        assert kwargs.get("stdin") == (
            None if kwargs.get("input") is not None else subprocess.DEVNULL
        )
        timeout = kwargs.get("timeout")
        assert timeout is None or (math.isfinite(timeout) and timeout > 0)
        self.calls.append((user, command, timeout))
        if command[-2:] == ["-m", "uxon.infra.execution_probe"]:
            assert timeout == 1.25
            if user in self.denied:
                return subprocess.CompletedProcess(argv, 1, "", "permission denied")
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"euid": 1001, "egid": 1002, "groups": [1002]}), ""
            )
        if "uxon.infra.path_probe" in command:
            assert timeout == 1.25
            output = io.StringIO()
            args = command[command.index("uxon.infra.path_probe") + 1 :]
            with contextlib.redirect_stdout(output):
                status = path_probe.main(args)
            return subprocess.CompletedProcess(argv, status, output.getvalue(), "")
        if command[:2] == ["sh", "-c"] and command[2].startswith("for c in "):
            assert timeout == 2
            names = shlex.split(command[2].split("; do", 1)[0])[3:]
            output = "".join(
                f"{name}\t" + (f"/usr/bin/{name}" if name in self.installed else "") + "\n"
                for name in names
            )
            return subprocess.CompletedProcess(argv, 0, output, "")
        assert self.respond is not None, (user, command, kwargs)
        return self.respond(user, command, kwargs)

    @contextlib.contextmanager
    def installed_boundary(self):
        with (
            mock.patch("uxon.infra.run.subprocess.run", side_effect=self),
            mock.patch(
                "uxon.infra.execution.pwd.getpwnam",
                return_value=SimpleNamespace(pw_uid=1001, pw_gid=1002),
            ),
            mock.patch("uxon.infra.execution.os.getgrouplist", return_value=[1002]),
        ):
            try:
                yield self
            finally:
                assert not self.errors, self.errors

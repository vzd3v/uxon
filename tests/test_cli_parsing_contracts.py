"""Public argv ownership, aliases and diagnostic contracts are cheap pure checks."""

import pytest

from uxon.cli.parsing import parse_args


def test_action_aliases_and_owned_flags_preserve_agent_passthrough(subtests):
    cases = [
        (["--version"], {"action": "version"}),
        (["-V"], {"action": "version"}),
        (["version", "--json"], {"action": "version", "json_output": True}),
        (
            ["doctor", "--json", "--remote"],
            {"action": "doctor", "json_output": True, "all_hosts": True},
        ),
        (
            ["kill-all", "--force", "--json"],
            {"action": "kill-all", "force": True, "json_output": True},
        ),
        (
            ["--killall", "--dry-run", "--json"],
            {"action": "kill-all", "dry_run": True, "json_output": True},
        ),
        (["list"], {"action": "list"}),
        (
            ["list", "--all-users", "--json"],
            {"action": "list", "all_users": True, "json_output": True},
        ),
        (
            ["-l", "--all-users", "--json"],
            {"action": "list", "all_users": True, "json_output": True},
        ),
        (
            ["--list", "--host", "prod", "--json"],
            {"action": "list", "host": "prod", "json_output": True},
        ),
        (["list", "--all-hosts"], {"action": "list", "all_hosts": True}),
        (
            ["kill", "example@claude", "--json"],
            {"action": "kill", "target_id": "example@claude", "json_output": True},
        ),
        (
            ["-k", "example", "--json", "--dry-run"],
            {"action": "kill", "target_id": "example", "json_output": True, "dry_run": True},
        ),
        (
            ["--kill", "example", "--user", "alice", "--host", "prod"],
            {"action": "kill", "target_id": "example", "user": "alice", "host": "prod"},
        ),
        (
            ["attach", "example", "--user", "alice", "--host", "prod"],
            {"action": "attach", "target_id": "example", "user": "alice", "host": "prod"},
        ),
        (
            ["-n", "demo", "--attach-existing"],
            {"action": "new", "target_id": "demo", "repeat_mode": "attach"},
        ),
        (
            ["new", "demo", "--new-session"],
            {"action": "new", "target_id": "demo", "repeat_mode": "new"},
        ),
        (
            ["new", "demo", "--git-remote", "work", "--git-visibility", "public"],
            {
                "action": "new",
                "target_id": "demo",
                "git_remote": "work",
                "git_visibility": "public",
            },
        ),
        (
            ["new", "demo", "--git-remote", "default"],
            {"action": "new", "target_id": "demo", "git_remote": "default"},
        ),
        (["new", "demo", "--no-git"], {"action": "new", "target_id": "demo", "no_git": True}),
        (
            [
                "run",
                "--profile",
                "codex",
                "--mode",
                "yolo",
                "--agent",
                "other",
                "--some-flag",
                "x",
                "--dsp",
            ],
            {
                "action": "run",
                "profile": "codex",
                "permission_mode": "yolo",
                "agent_args": ["--agent", "other", "--some-flag", "x", "--dsp"],
            },
        ),
        (["--model", "example"], {"action": "run", "agent_args": ["--model", "example"]}),
    ]
    defaults = {
        "target_id": None,
        "json_output": False,
        "force": False,
        "dry_run": False,
        "all_users": False,
        "all_hosts": False,
        "host": None,
        "user": None,
        "repeat_mode": None,
        "git_remote": None,
        "git_visibility": None,
        "no_git": False,
        "profile": None,
        "permission_mode": None,
        "agent_args": [],
    }
    for argv, expected in cases:
        with subtests.test(argv=argv):
            actual = parse_args(argv)
            assert {key: getattr(actual, key) for key in defaults | expected} == defaults | expected


def test_invalid_owned_flags_require_actionable_field_diagnostics(subtests):
    cases = [
        (
            ["new", "demo", "--git-remote", "work", "--no-git"],
            "cannot combine --no-git with --git-remote",
        ),
        (
            ["new", "demo", "--no-git", "--git-remote", "work"],
            "cannot combine --git-remote with --no-git",
        ),
        (
            ["new", "demo", "-w", "branch", "--git-remote", "work"],
            "cannot combine --git-remote with -w",
        ),
        (
            ["new", "demo", "--git-remote", "work", "-w", "branch"],
            "cannot combine -w with --git-remote",
        ),
        (
            ["new", "demo", "--git-visibility", "secret"],
            "--git-visibility must be 'private' or 'public'",
        ),
        (["run", "--git-remote", "work"], "--git-remote is only supported"),
        (["run", "--no-git"], "--no-git is only supported"),
        (["run", "--git-visibility", "private"], "--git-visibility is only supported"),
        (["list", "--host"], "--host requires a host name"),
        (
            ["list", "--host", "prod", "--all-hosts"],
            "--host and --all-hosts are mutually exclusive",
        ),
        (
            ["list", "--all-hosts", "--host", "prod"],
            "--host and --all-hosts are mutually exclusive",
        ),
        (["run", "--mode"], "--mode requires an id"),
        (["run", "--profile"], "--profile requires an id"),
        (["new", "demo", "--git-remote"], "--git-remote requires a profile name"),
        (["new", "demo", "--git-visibility"], "--git-visibility requires 'private' or 'public'"),
    ]
    for argv, diagnostic in cases:
        with subtests.test(argv=argv), pytest.raises(SystemExit) as caught:
            parse_args(argv)
        assert caught.value.code == 2
        assert diagnostic in caught.value.uxon_msg

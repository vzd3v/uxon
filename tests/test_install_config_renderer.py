"""Validated installer output and the public configuration boundary."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path

import pytest

from uxon.cli.main import main
from uxon.infra import config_loader
from uxon.infra.config_loader import ConfigValidationError, parse_config
from uxon.infra.config_renderer import render_config


def _rich_config() -> dict:
    return {
        "default_launch_user": "dana_agent",
        "default_launch_mode": "caller",
        "enable_all_users_list": True,
        "session_users": ["dana_agent", "bob"],
        "allowed_roots": ["/srv", "/tmp"],
        "session_prefix": "test-",
        "legacy_session_prefixes": ["old-"],
        "repeat_noninteractive_mode": "attach",
        "tmux_socket_template": "/tmp/uxon-{user}-{uid}-{execution_backend}.sock",
        "new_project_root": "/srv/projects",
        "worktree_root": "/data/wt",
        "worktree_base": "remote",
        "tui_refresh_interval_seconds": 5.5,
        "tui_ssh_refresh_interval_seconds": 12,
        "ssh_multiplex": "off",
        "ssh_control_persist_seconds": 60,
        "fetch_concurrency": 3,
        "launch_record_dir": "/var/lib/uxon/records",
        "git_create_enabled": True,
        "launch_user_by_caller": {"alice.dot": "dana_agent", 'quote"key': "bob", "开发者": "alice"},
        "agents": {"claude": {"default_args": ["--model", "example"]}},
        "launch": {
            "enabled_profiles": ["claude_box", "cursor"],
            "default_profile": "claude_box",
            "profiles": {
                "claude_box": {
                    "agent": "claude",
                    "runtime": "box",
                    "launch_user": "dana_agent",
                    "display_name": "Claude work",
                    "allowed_git_remote_profiles": ["work", "personal"],
                    "default_git_remote_profile": "work",
                }
            },
            "path_rules": [
                {
                    "path_prefix": "/srv/projects",
                    "allowed_profiles": ["claude_box"],
                    "default_profile": "claude_box",
                    "allowed_git_remote_profiles": ["work"],
                    "default_git_remote_profile": "work",
                }
            ],
        },
        "git_remote_profiles": [
            {
                "name": "work",
                "host": "github.com",
                "owner": "example",
                "auth": "gh",
                "creds_user": "bob",
                "visibility": "private",
            },
            {
                "name": "personal",
                "host": "github.com",
                "owner": "alice",
                "auth": "token",
                "creds_user": "alice",
                "token_file": "/home/alice/.secrets/git.token",
            },
        ],
        "execution": {
            "default_backend": "boundary",
            "backend_by_launch_user": {"dana_agent": "boundary"},
            "backends": {
                "boundary": {
                    "kind": "command",
                    "command_prefix": [
                        "/usr/bin/sudo",
                        "-n",
                        "--",
                        "/usr/local/libexec/uxon-exec",
                        "{user}",
                        "--",
                    ],
                    "probe_timeout_seconds": 3.0,
                }
            },
        },
        "runtimes": {
            "box": {
                "kind": "command",
                "resource_scope": "per_user",
                "resource_name_template": "{user}-{launch_profile}-{project_slug}",
                "exec_prefix": ["docker", "exec", "-w", "{runtime_dir}", "{resource}"],
                "readiness": {
                    "ready_command": ["docker", "top", "{resource}"],
                    "on_missing": "fail",
                },
                "identity": {"resolve_command": ["docker", "inspect", "{resource}"]},
                "session": {
                    "stop_command": ["docker", "exec", "{resource}", "stop-workload", "{pidfile}"]
                },
                "timeouts": {"probe_seconds": 4.5, "prepare_seconds": 90, "stop_seconds": 8},
                "path_map": {'/srv/projects/quote".目录': "/work/quote", "/srv/projects": "/work"},
            }
        },
        "tui": {
            "table": {"columns": ["name", "user", "cpu", "ram", "last"], "default_view": "by_host"},
            "search": {"fields": ["name", "host", "path", "cmd"]},
            "color_palette": ["cyan", "blue"],
        },
        "local_host": {"color": "green"},
        "audit": {"enabled": False, "syslog_facility": "local0"},
        "tmux": {
            "manage_options": True,
            "set_terminal_title": False,
            "options": {"mouse": True, "history-limit": 1000, "status-left": 'quote"目录'},
            "server_options": {"extended-keys": "on"},
            "append_server_options": {"terminal-features": "xterm*:extkeys"},
        },
        "remote_hosts": [
            {
                "name": "peer",
                "ssh_alias": "edge",
                "interval": "5s",
                "connect_timeout": "500ms",
                "total_timeout": "2m",
            }
        ],
    }


def test_renderer_roundtrip_preserves_complete_effective_config_and_quoted_maps() -> None:
    raw = _rich_config()
    original = deepcopy(raw)
    parsed = tomllib.loads(render_config(raw))
    cfg = parse_config(parsed)
    assert cfg == parse_config(original)
    assert raw == original
    assert parsed["launch_user_by_caller"] == original["launch_user_by_caller"]
    assert parsed["runtimes"]["box"] == original["runtimes"]["box"]
    assert parsed["tmux"]["options"] == original["tmux"]["options"]
    assert (
        cfg.default_launch_user,
        cfg.default_launch_mode,
        cfg.enable_all_users_list,
        cfg.session_users,
        cfg.allowed_roots,
        cfg.legacy_session_prefixes,
    ) == ("dana_agent", "caller", True, ["dana_agent", "bob"], ["/srv", "/tmp"], ("old-",))
    assert (cfg.launch.enabled_profiles, cfg.launch.default_profile) == (
        ("claude_box", "cursor"),
        "claude_box",
    )
    profile = cfg.launch.profiles["claude_box"]
    assert (
        profile.agent,
        profile.runtime,
        profile.launch_user,
        profile.git_remote.allowed_profiles,
        profile.git_remote.default_profile,
    ) == ("claude", "box", "dana_agent", ("work", "personal"), "work")
    assert cfg.launch.path_rules[0].allowed_git_remote_profiles == ("work",)
    assert [(p.name, p.auth, p.creds_user, p.token_file) for p in cfg.git_remote_profiles] == [
        ("work", "gh", "bob", ""),
        ("personal", "token", "alice", "/home/alice/.secrets/git.token"),
    ]
    runtime = cfg.runtimes["box"]
    assert (
        runtime.resource_scope,
        runtime.exec_prefix,
        runtime.probe_timeout_seconds,
        runtime.prepare_timeout_seconds,
        runtime.stop_timeout_seconds,
    ) == ("per_user", ("docker", "exec", "-w", "{runtime_dir}", "{resource}"), 4.5, 90.0, 8.0)
    assert cfg.execution.backends["boundary"].probe_timeout_seconds == 3.0
    assert cfg.execution.backends["boundary"].command_prefix == tuple(
        original["execution"]["backends"]["boundary"]["command_prefix"]
    )
    assert (cfg.worktree_root, cfg.worktree_base, cfg.repeat_noninteractive_mode) == (
        "/data/wt",
        "remote",
        "attach",
    )
    assert (
        cfg.tui_refresh_interval_seconds,
        cfg.tui_ssh_refresh_interval_seconds,
        cfg.tui_table_columns,
        cfg.tui_table_default_view,
        cfg.tui_search_fields,
    ) == (
        5.5,
        12.0,
        ("name", "user", "cpu", "ram", "last"),
        "by_host",
        ("name", "host", "path", "cmd"),
    )
    assert (cfg.tmux_manage_options, cfg.tmux_set_terminal_title, cfg.audit_enabled) == (
        True,
        False,
        False,
    )


class PublicConfigTests(unittest.TestCase):
    def test_safe_defaults_and_explicit_empty_normalization(self) -> None:
        cfg = parse_config({})
        self.assertEqual(
            (
                cfg.session_prefix,
                cfg.legacy_session_prefixes,
                cfg.remote_hosts,
                cfg.worktree_root,
                cfg.worktree_base,
                cfg.ssh_multiplex,
                cfg.ssh_control_persist_seconds,
                cfg.tui_table_columns,
                cfg.tui_refresh_interval_seconds,
                cfg.default_launch_mode,
            ),
            ("uxon-", (), [], "", "local", "auto", 300, None, 2.0, "caller"),
        )
        self.assertEqual(
            (
                cfg.launch.enabled_profiles,
                cfg.launch.default_profile,
                cfg.launch.effective_enabled_profiles,
                cfg.agents["claude"].default_args,
            ),
            ((), "", ("claude", "codex", "cursor"), ()),
        )
        self.assertEqual(
            (
                cfg.tmux_manage_options,
                cfg.tmux_set_terminal_title,
                cfg.tmux_options,
                cfg.tmux_server_options,
                cfg.tmux_append_server_options,
            ),
            (
                False,
                True,
                {"mouse": "on", "allow-passthrough": "on"},
                {"extended-keys": "on"},
                {"terminal-features": "xterm*:extkeys"},
            ),
        )
        empty = parse_config(
            {
                "tui": {"table": {"columns": []}},
                "launch": {"enabled_profiles": []},
                "session_prefix": "uxon-",
                "legacy_session_prefixes": ["uxon-", "old-"],
                "session_users": [" bob ", "alice", "bob", ""],
                "launch_user_by_caller": {" alice ": " bob "},
            }
        )
        self.assertIsNone(empty.tui_table_columns)
        self.assertEqual(
            (
                empty.launch.enabled_profiles,
                empty.launch.default_profile,
                empty.launch.effective_enabled_profiles,
            ),
            ((), "", ("claude", "codex", "cursor")),
        )
        self.assertEqual(empty.legacy_session_prefixes, ("old-",))
        self.assertEqual(empty.session_users, ["bob", "alice"])
        self.assertEqual(empty.launch_user_by_caller, {"alice": "bob"})

    def test_tmux_scope_replacement_and_explicit_profile_overrides(self) -> None:
        cfg = parse_config({"tmux": {"manage_options": True}})
        self.assertEqual(
            (cfg.tmux_options, cfg.tmux_server_options, cfg.tmux_append_server_options),
            (
                {"mouse": "on", "allow-passthrough": "on"},
                {"extended-keys": "on"},
                {"terminal-features": "xterm*:extkeys"},
            ),
        )
        cfg = parse_config({"tmux": {"options": {"mouse": "off"}}})
        self.assertEqual(
            (cfg.tmux_options, cfg.tmux_server_options, cfg.tmux_append_server_options),
            ({"mouse": "off"}, {"extended-keys": "on"}, {"terminal-features": "xterm*:extkeys"}),
        )
        disabled = parse_config({"tmux": {"manage_options": False, "set_terminal_title": False}})
        self.assertEqual(
            (disabled.tmux_manage_options, disabled.tmux_set_terminal_title), (False, False)
        )
        self.assertEqual(disabled.tmux_options, {"mouse": "on", "allow-passthrough": "on"})
        cfg = parse_config(
            {
                "launch": {
                    "enabled_profiles": ["claude", "cursor"],
                    "default_profile": "cursor",
                    "profiles": {"claude": {"agent": "claude", "launch_user": "dana_agent"}},
                },
                "agents": {"claude": {"default_args": ["--verbose"]}},
            }
        )
        self.assertEqual(
            (
                cfg.launch.enabled_profiles,
                cfg.launch.default_profile,
                cfg.launch.profiles["claude"].launch_user,
                cfg.agents["claude"].default_args,
            ),
            (("claude", "cursor"), "cursor", "dana_agent", ("--verbose",)),
        )

    def test_invalid_shapes_unknown_keys_and_strict_scalar_types(self) -> None:
        cases = [
            ({"legacy_session_prefixes": "old-"}, "legacy_session_prefixes"),
            ({"tui": {"table": {"columns": "name"}}}, "columns"),
            ({"tui": {"table": {"default_sort_by": "ram"}}}, "default_sort_by"),
            ({"tmux": {"options": "bad"}}, "options"),
            ({"tmux": {"options": {"mouse": [1, 2]}}}, "tmux.options"),
            ({"container": {"enabled": True}}, "container"),
            ({"execution": {"state_dir": "/run/uxon"}}, "state_dir"),
            (
                {
                    "launch": {
                        "profiles": {"custom": {"agent": "claude", "container_profile": "box"}}
                    }
                },
                "container_profile",
            ),
            (
                {
                    "launch": {
                        "profiles": {"custom": {"agent": "claude", "runtime_namespace": "per_user"}}
                    }
                },
                "runtime_namespace",
            ),
            (
                {"launch": {"profiles": {"custom": {"agent": "claude", "unknown_policy": True}}}},
                "unknown_policy",
            ),
            ({"default_git_remote_profile": "missing"}, "default_git_remote_profile"),
            ({"default_claude_args": []}, "default_claude_args"),
            ({"agents": {"enabled": ["claude"]}}, "agents.enabled"),
            ({"agents": {"default": "claude"}}, "agents.default"),
            ({"agents": {"claude": 5}}, "agents.claude"),
            ({"runtimes": {"box": {"resource_name_template": 123}}}, "resource_name_template"),
            ({"enable_all_users_list": "false"}, "enable_all_users_list"),
            ({"git_create_enabled": 1}, "git_create_enabled"),
            ({"audit": {"enabled": "yes"}}, "audit.enabled"),
            ({"tmux": {"manage_options": 0}}, "tmux.manage_options"),
            ({"tmux": {"set_terminal_title": "false"}}, "tmux.set_terminal_title"),
            ({"tmux": {"set_terminal_title": 0}}, "tmux.set_terminal_title"),
            ({"agents": {"claude": {"mode": [{"id": "normal", "dangerous": "no"}]}}}, "dangerous"),
            ({"fetch_concurrency": True}, "fetch_concurrency"),
            (
                {"execution": {"backends": {"box": {"probe_timeout_seconds": False}}}},
                "probe_timeout_seconds",
            ),
        ]
        for payload, field in cases:
            with (
                self.subTest(field=field, payload=payload),
                self.assertRaises(ConfigValidationError) as error,
            ):
                parse_config(payload)
            self.assertIn(field, str(error.exception))
        for value in (float("nan"), float("inf"), 10**400):
            with self.subTest(value=value), self.assertRaises(ConfigValidationError) as error:
                parse_config({"tui_refresh_interval_seconds": value})
            self.assertIn("tui_refresh_interval_seconds", str(error.exception))

    def test_unsafe_catalog_runtime_and_path_policies_are_rejected(self) -> None:
        cases = [
            ({"tui_refresh_interval_seconds": 0}, "tui_refresh_interval_seconds"),
            ({"ssh_control_persist_seconds": 0}, "ssh_control_persist_seconds"),
            ({"worktree_base": "origin"}, "worktree_base"),
            ({"execution": {"default_backend": "missing"}}, "missing"),
            (
                {
                    "runtimes": {
                        "box": {
                            "kind": "command",
                            "resource_name_template": "box",
                            "exec_prefix": ["runner", "{resource}"],
                            "telemetry": "bad",
                        }
                    }
                },
                "telemetry",
            ),
            ({"agents": {"custom": {"binary": "custom-bin"}}}, "mode"),
            ({"agents": {"bad:id": {"mode": [{"id": "normal"}]}}}, "agent id"),
            ({"agents": {"Bad": {"mode": [{"id": "normal"}]}}}, "agent id"),
            ({"agents": {"odd.id": {"binary": "custom-bin"}}}, "agent id"),
            ({"launch": {"profiles": {"bad.profile": {"agent": "claude"}}}}, "profile"),
            (
                {
                    "runtimes": {
                        "bad-profile": {
                            "kind": "command",
                            "resource_name_template": "{project_slug}",
                            "exec_prefix": ["runner", "{resource}"],
                        }
                    }
                },
                "runtime",
            ),
            (
                {
                    "runtimes": {
                        "box": {
                            "kind": "command",
                            "resource_name_template": "{resource}",
                            "exec_prefix": ["runner", "{resource}"],
                        }
                    }
                },
                "unsupported placeholder",
            ),
            (
                {
                    "runtimes": {
                        "box": {
                            "kind": "command",
                            "resource_name_template": "{project_slug}",
                            "exec_prefix": ["runner", "{resource}"],
                            "session": {"stop_command": ["stop", "{resource}", "{pidfile}"]},
                        }
                    }
                },
                "identity.resolve_command",
            ),
            (
                {
                    "launch": {
                        "profiles": {"claude": {"agent": "claude", "launch_user": "dana_agent"}}
                    }
                },
                "auto-mode",
            ),
        ]
        for payload, diagnostic in cases:
            with self.subTest(payload=payload), self.assertRaises(ConfigValidationError) as error:
                parse_config(payload)
            self.assertIn(diagnostic, str(error.exception))
        for prefix, diagnostic in (
            ("relative/app", "absolute"),
            ("/srv/app/../secret", "normalized"),
        ):
            with self.subTest(prefix=prefix), self.assertRaises(ConfigValidationError) as error:
                parse_config(
                    {
                        "launch": {
                            "enabled_profiles": ["claude"],
                            "path_rules": [{"path_prefix": prefix, "allowed_profiles": ["claude"]}],
                        }
                    }
                )
            self.assertIn(diagnostic, str(error.exception))
        payload = _rich_config()
        payload["launch"]["path_rules"][0].update(
            allowed_git_remote_profiles=["personal"], default_git_remote_profile="work"
        )
        with self.assertRaisesRegex(ConfigValidationError, "after git-remote policy intersection"):
            parse_config(payload)


def test_loader_missing_valid_malformed_semantic_and_demo_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    monkeypatch.setattr(config_loader, "OPERATOR_CONFIG_PATH", path)
    monkeypatch.delenv("UXON_DEMO_HOSTS", raising=False)
    assert config_loader.load_config().session_prefix == "uxon-"
    path.write_text(render_config(_rich_config()))
    assert config_loader.load_config() == parse_config(_rich_config())
    for content, diagnostic, code in (
        ("[broken", "invalid TOML", 1),
        ('worktree_base = "origin"\n', "worktree_base", 2),
    ):
        path.write_text(content)
        with pytest.raises(SystemExit) as error:
            config_loader.load_config()
        assert error.value.code == code
        assert diagnostic in error.value.uxon_msg
    path.write_text('[[remote_hosts]]\nname="real"\nssh_alias="real"\n')
    demo = tmp_path / "demo"
    demo.mkdir()
    (demo / "peer.json").write_text('{"demo_color":"cyan"}')
    (demo / "_local.json").write_text("{}")
    monkeypatch.setenv("UXON_DEMO_HOSTS", str(demo))
    cfg = config_loader.load_config()
    assert [(h.name, h.ssh_alias, h.color) for h in cfg.remote_hosts] == [
        ("peer", "__uxon_demo__:peer", "cyan")
    ]


def test_renderer_invalid_json_is_a_clean_cli_error(tmp_path: Path) -> None:
    payload = tmp_path / "bad.json"
    payload.write_text("{broken", encoding="utf-8")
    script = Path(__file__).resolve().parent.parent / "install" / "render_uxon_config.py"
    result = subprocess.run(
        [sys.executable, str(script), "--config-json", str(payload)],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "uxon: config render failed:" in result.stderr


def test_packaged_config_render_bypasses_hostile_installed_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed = tmp_path / "installed.toml"
    installed.write_text("[broken")
    monkeypatch.setattr(config_loader, "OPERATOR_CONFIG_PATH", installed)
    payload = tmp_path / "config.json"
    output = tmp_path / "config.toml"
    payload.write_text(json.dumps(_rich_config()))
    assert main(["config", "render", "--config-json", str(payload), "--output", str(output)]) == 0
    assert parse_config(tomllib.loads(output.read_text())) == parse_config(_rich_config())


def test_renderer_and_loader_share_semantic_validation() -> None:
    payload = {"launch": {"enabled_profiles": ["missing"]}}
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        with pytest.raises(ConfigValidationError) as loader_error:
            parse_config(payload)
        with pytest.raises(ConfigValidationError) as renderer_error:
            render_config(payload)
    assert str(renderer_error.value) == str(loader_error.value)
    assert "missing" in str(renderer_error.value)
    assert stdout.getvalue() == stderr.getvalue() == ""

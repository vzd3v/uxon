"""Contract for the bundled v4 JSON-to-TOML installer surface."""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

import pytest


def _renderer():
    path = Path(__file__).resolve().parent.parent / "install" / "render_uxon_config.py"
    spec = importlib.util.spec_from_file_location("render_uxon_config", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_renderer_emits_execution_launch_and_generic_runtime_tables() -> None:
    payload = {
        "default_launch_mode": "caller",
        "agents": {"claude": {"default_args": []}},
        "launch": {
            "enabled_profiles": ["claude_box"],
            "default_profile": "claude_box",
            "profiles": {
                "claude_box": {"agent": "claude", "runtime": "box"},
            },
        },
        "execution": {
            "default_backend": "netns",
            "backends": {
                "netns": {
                    "kind": "command",
                    "command_prefix": [
                        "/usr/bin/sudo",
                        "-n",
                        "--",
                        "/usr/local/libexec/uxon-exec",
                        "{user}",
                        "--",
                    ],
                }
            },
        },
        "runtimes": {
            "box": {
                "kind": "command",
                "resource_name_template": "box-{project_slug}",
                "exec_prefix": ["docker", "exec", "{resource}"],
                "readiness": {"ready_command": ["docker", "top", "{resource}"]},
                "path_map": {"/srv/projects": "/work"},
            }
        },
    }
    parsed = tomllib.loads(_renderer().render_config(payload))
    assert parsed["tmux_socket_template"] == "/tmp/uxon-{user}-{execution_backend}.sock"
    assert parsed["execution"]["backends"]["netns"]["kind"] == "command"
    assert parsed["launch"]["profiles"]["claude_box"]["runtime"] == "box"
    assert parsed["runtimes"]["box"]["path_map"]["/srv/projects"] == "/work"
    assert "container" not in parsed


def test_renderer_rejects_unknown_keys_at_nested_levels() -> None:
    renderer = _renderer()
    with pytest.raises(ValueError, match="unknown key"):
        renderer.render_config(
            {
                "launch": {
                    "profiles": {
                        "custom": {"agent": "claude", "unknown_policy": True},
                    }
                }
            }
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"container": {"enabled": True}},
        {"execution": {"state_dir": "/run/uxon"}},
        {"launch": {"profiles": {"custom": {"agent": "claude", "container_profile": "box"}}}},
        {"launch": {"profiles": {"custom": {"agent": "claude", "runtime_namespace": "per_user"}}}},
    ],
)
def test_renderer_rejects_removed_v3_isolation_keys_as_unknown(payload: dict) -> None:
    with pytest.raises(ValueError, match="unknown key"):
        _renderer().render_config(payload)

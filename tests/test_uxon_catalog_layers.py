"""Catalog overrides inherit fields while replacing whole permission policies."""

from copy import deepcopy

import pytest

from uxon.infra.config_loader import ConfigValidationError, parse_config


def test_catalog_overrides_and_custom_agent_survive_public_parsing() -> None:
    raw = {
        "agents": {
            "claude": {
                "default_args": ["--model", "example"],
                "mode": [
                    {"id": "safe"},
                    {"id": "wild", "flags": ["-x"], "dangerous": True},
                ],
            },
            "custom": {
                "binary": "custom-bin",
                "version_args": ["version"],
                "mode": [{"id": "normal"}],
            },
        }
    }
    original = deepcopy(raw)
    cfg = parse_config(raw)
    claude = cfg.agents["claude"]
    assert (claude.binary, claude.default_args, claude.version_args) == (
        "claude",
        ("--model", "example"),
        ("--version",),
    )
    assert [(m.id, m.label, m.flags, m.dangerous) for m in claude.permission_modes] == [
        ("safe", "safe", (), False),
        ("wild", "wild", ("-x",), True),
    ]
    assert set(cfg.agents) == {"claude", "codex", "cursor", "custom"}
    custom = cfg.agents["custom"]
    assert (custom.binary, custom.default_args, custom.version_args) == (
        "custom-bin",
        (),
        ("version",),
    )
    assert [(m.id, m.flags, m.dangerous) for m in custom.permission_modes] == [
        ("normal", (), False)
    ]
    assert raw == original
    raw["agents"]["claude"]["default_args"].append("--changed")
    assert claude.default_args == ("--model", "example")


@pytest.mark.parametrize("agent_id", ["custom", "claude"])
def test_duplicate_mode_ids_fail_configuration_instead_of_selecting_another_policy(agent_id):
    with pytest.raises(
        ConfigValidationError, match=f"duplicate mode id 'inspect' in agents.{agent_id}.mode"
    ):
        parse_config(
            {
                "agents": {
                    agent_id: {
                        "mode": [
                            {"id": "inspect", "label": "Read only", "flags": ["--read-only"]},
                            {
                                "id": "inspect",
                                "label": "Write",
                                "flags": ["--write"],
                                "dangerous": True,
                            },
                        ]
                    }
                }
            }
        )

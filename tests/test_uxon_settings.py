"""Settings resolution and atomic, comment-preserving operator edits."""

from __future__ import annotations

import os
import stat
import subprocess
import tomllib
from copy import deepcopy
from pathlib import Path
from unittest import mock

import pytest

from uxon.infra import config_loader, settings


def test_settings_resolution_preserves_falsy_overrides_and_readonly_policy() -> None:
    defaults = {
        "default_launch_user": "dana_agent",
        "enable_all_users_list": True,
        "tui_refresh_interval_seconds": 2.0,
        "tui": {"table": {"default_view": "flat"}, "search": {"fields": ["name", "user"]}},
        "tmux": {"set_terminal_title": True},
    }
    operator = {
        "default_launch_user": "",
        "enable_all_users_list": False,
        "tui_refresh_interval_seconds": 0,
        "tui": {"search": {"fields": []}},
        "tmux": {"set_terminal_title": False},
    }
    original = deepcopy(operator)
    expected = {
        "default_launch_user": ("", "operator"),
        "enable_all_users_list": (False, "operator"),
        "tui_refresh_interval_seconds": (0, "operator"),
        "tui.search.fields": ([], "operator"),
        "tui.table.default_view": ("flat", "default"),
        "tmux.set_terminal_title": (False, "operator"),
    }
    for uid, editable in ((0, True), (1000, False)):
        with mock.patch("os.geteuid", return_value=uid):
            entries = settings.resolve_setting_entries(operator, defaults)
        by_key = {e.spec.key: e for e in entries}
        assert {key: (by_key[key].value, by_key[key].source) for key in expected} == expected
        assert all(e.editable is editable for e in entries)
    assert operator == original
    assert not any(
        e.spec.key.split(".")[0] in {"agents", "launch", "runtimes", "execution"} for e in entries
    )


def test_persisted_batch_edit_remove_and_reload_preserve_unrelated_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    monkeypatch.setattr(config_loader, "OPERATOR_CONFIG_PATH", path)
    monkeypatch.delenv("UXON_DEMO_HOSTS", raising=False)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    ownership = []

    def chown(fd: int, uid: int, gid: int) -> None:
        assert stat.S_ISREG(os.fstat(fd).st_mode)
        ownership.append((uid, gid))

    monkeypatch.setattr(os, "fchown", chown)
    settings.remove_operator_key(path, "default_launch_user")
    assert not path.exists()
    requested = {
        "default_launch_user": "dana_agent",
        "worktree_root": "/data/wt",
        "worktree_base": "remote",
        "launch_user_by_caller": {"alice.dot": "bob", 'quote"目录': "dana_agent"},
        "tui_refresh_interval_seconds": 2.5,
        "tui.table.default_view": "by_host",
        "tui.table.columns": ["name", "cpu"],
        "tui.search.fields": ["name", "host"],
        "tui.color_palette": ["cyan", "blue"],
        "local_host.color": "bright_green",
        "ssh_multiplex": "off",
        "ssh_control_persist_seconds": 90,
        "tmux.manage_options": True,
        "tmux.set_terminal_title": False,
    }
    original_request = deepcopy(requested)
    settings.persist_operator_config_updates(path, requested)
    assert requested == original_request
    fresh = tomllib.loads(path.read_text())
    assert set(fresh) == {
        "default_launch_user",
        "worktree_root",
        "worktree_base",
        "launch_user_by_caller",
        "tui",
        "local_host",
        "ssh_multiplex",
        "ssh_control_persist_seconds",
        "tmux",
        "tui_refresh_interval_seconds",
    }
    cfg = config_loader.load_config()
    assert (
        cfg.default_launch_user,
        cfg.worktree_root,
        cfg.worktree_base,
        cfg.launch_user_by_caller,
        cfg.tui_table_default_view,
        cfg.tui_table_columns,
        cfg.tui_search_fields,
        cfg.tui_color_palette,
        cfg.local_host_color,
        cfg.ssh_multiplex,
        cfg.ssh_control_persist_seconds,
        cfg.tmux_manage_options,
        cfg.tmux_set_terminal_title,
    ) == (
        "dana_agent",
        "/data/wt",
        "remote",
        {"alice.dot": "bob", 'quote"目录': "dana_agent"},
        "by_host",
        ("name", "cpu"),
        ("name", "host"),
        ("cyan", "blue"),
        "bright_green",
        "off",
        90,
        True,
        False,
    )
    assert cfg.tui_refresh_interval_seconds == 2.5
    untouched = '\n# Workload policy remains file-only.\n[launch.profiles.work]\nagent = "claude"\nruntime = "box"\n\n[runtimes.box]\nkind = "command"\nresource_name_template = "box-{project_slug}"\nexec_prefix = ["runner", "{resource}"]\n'
    commented = "# Operator preferences\n" + path.read_text().replace(
        'default_launch_user = "dana_agent"',
        'default_launch_user = "dana_agent"  # launch identity',
    )
    commented = commented.replace(
        "[launch_user_by_caller]", "# Per-caller overrides\n[launch_user_by_caller]"
    )
    path.write_text(commented + untouched)
    settings.persist_operator_config_updates(
        path,
        {
            "default_launch_user": "bob",
            "launch_user_by_caller": {"bob": "alice"},
            "tmux.set_terminal_title": True,
        },
    )
    text = path.read_text()
    assert text.endswith(untouched)
    assert (
        "# Operator preferences\n" in text
        and "# launch identity" in text
        and "# Per-caller overrides\n" in text
    )
    cfg = config_loader.load_config()
    assert (
        cfg.default_launch_user,
        cfg.launch_user_by_caller,
        cfg.tmux_manage_options,
        cfg.tmux_set_terminal_title,
    ) == ("bob", {"bob": "alice"}, True, True)
    assert cfg.launch.profiles["work"].runtime == "box"
    assert cfg.runtimes["box"].exec_prefix == ("runner", "{resource}")
    for key in ("default_launch_user", "tui.search.fields", "session_prefix", "tui.search.fields"):
        settings.remove_operator_key(path, key)
    assert path.read_text().endswith(untouched)
    cfg = config_loader.load_config()
    assert cfg.default_launch_user == "" and cfg.tui_search_fields == ("name", "user")
    assert cfg.tmux_set_terminal_title and cfg.tmux_manage_options
    assert path.stat().st_mode & 0o777 == 0o644
    assert ownership and set(ownership) == {(0, 0)}
    assert list(tmp_path.glob(".*.tmp")) == []


def test_rejected_persistence_leaves_original_bytes_and_no_temporary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    original = b'# original\nsession_prefix = "test-"\n'
    path.write_bytes(original)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    for updates, error, diagnostic in (
        ({"unknown": 1}, KeyError, "unknown"),
        ({"launch_user_by_caller": "not-a-map"}, ValueError, "mapping"),
        ({"launch_user_by_caller": {"alice": 1}}, ValueError, "string"),
    ):
        with pytest.raises(error, match=diagnostic):
            settings.persist_operator_config_updates(path, updates)
        assert path.read_bytes() == original
        assert sorted(p.name for p in tmp_path.iterdir()) == ["config.toml"]


@pytest.mark.parametrize("stage", ["chown", "replace"])
def test_atomic_install_failure_preserves_old_file_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    path = tmp_path / "config.toml"
    original = b'# original\nsession_prefix = "test-"\n'
    path.write_bytes(original)
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    def chown(fd: int, uid: int, gid: int) -> None:
        assert (uid, gid) == (0, 0)
        if stage == "chown":
            raise OSError("ownership refused")

    def replace(source: Path, target: Path) -> None:
        assert target == path
        assert tomllib.loads(source.read_text())["session_prefix"] == "new-"
        assert source.stat().st_mode & 0o777 == 0o644
        assert target.read_bytes() == original
        raise OSError("replace refused")

    monkeypatch.setattr(os, "fchown", chown)
    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(OSError, match="refused"):
        settings.persist_operator_config_updates(path, {"session_prefix": "new-"})
    assert path.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.toml"]


def test_nonroot_write_refuses_without_mutation_or_external_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    path.write_bytes(b'# original\nsession_prefix="test-"\n')
    original = path.read_bytes()
    monkeypatch.setattr(os, "geteuid", lambda: 1000)

    def forbidden(*args, **kwargs):
        raise AssertionError("nonroot persistence must not spawn a helper")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with pytest.raises(PermissionError, match="read-only"):
        settings.persist_operator_config_updates(path, {"session_prefix": "new-"})
    assert path.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.toml"]


def test_config_and_settings_ignore_hostile_project_decoys_from_actual_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator = tmp_path / "etc" / "uxon" / "config.toml"
    operator.parent.mkdir(parents=True)
    operator.write_text('default_launch_user="dana_agent"\nsession_prefix="operator-"\n')
    project = tmp_path / "project"
    cwd = project / "nested"
    cwd.mkdir(parents=True)
    (project / ".uxon.toml").write_text('session_prefix="project-"\n[agents]\ndefault="removed"\n')
    (cwd / "config.toml").write_text("[malformed")
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(config_loader, "OPERATOR_CONFIG_PATH", operator)
    monkeypatch.delenv("UXON_DEMO_HOSTS", raising=False)
    cfg = config_loader.load_config()
    assert (cfg.default_launch_user, cfg.session_prefix) == ("dana_agent", "operator-")
    assert settings.load_settings_source() == {
        "default_launch_user": "dana_agent",
        "session_prefix": "operator-",
    }
    assert config_loader.config_sources() == (operator,)
    assert config_loader.operator_config_path() == operator

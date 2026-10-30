"""Peer settings are accepted and rejected through the public TOML parser."""

import dataclasses
import subprocess
import tomllib
import unittest

import pytest

from uxon.infra.config_loader import ConfigValidationError, parse_config
from uxon.infra.remote.collector import fetch_remote_snapshot


class RemoteHostConfigTests(unittest.TestCase):
    def test_ordered_peers_preserve_defaults_options_and_duration_formats(self) -> None:
        cfg = parse_config(
            tomllib.loads("""
[[remote_hosts]]
name = "  A.B-C_d.1  "
ssh_alias = "  edge  "
[[remote_hosts]]
name = "full"
ssh_alias = "edge-eu"
description = "  EU edge node  "
remote_uxon = "/opt/uxon/bin/uxon"
interval = "5s"
connect_timeout = "500MS"
total_timeout = " 2m "
extra_ssh_options = ["-o", "ProxyJump=bastion"]
command_template = ["ssh", "{ssh_alias}", "{remote_command}"]
color = "bright_cyan"
[[remote_hosts]]
name = "numeric"
ssh_alias = "numeric"
interval = 10
connect_timeout = 1.5
total_timeout = "3.25"
""")
        )
        self.assertEqual(
            [dataclasses.asdict(h) for h in cfg.remote_hosts],
            [
                dict(
                    name="A.B-C_d.1",
                    ssh_alias="edge",
                    description="",
                    remote_uxon="uxon",
                    interval=None,
                    connect_timeout=None,
                    total_timeout=None,
                    extra_ssh_options=(),
                    command_template=None,
                    color=None,
                ),
                dict(
                    name="full",
                    ssh_alias="edge-eu",
                    description="EU edge node",
                    remote_uxon="/opt/uxon/bin/uxon",
                    interval=5.0,
                    connect_timeout=0.5,
                    total_timeout=120.0,
                    extra_ssh_options=("-o", "ProxyJump=bastion"),
                    command_template=("ssh", "{ssh_alias}", "{remote_command}"),
                    color="bright_cyan",
                ),
                dict(
                    name="numeric",
                    ssh_alias="numeric",
                    description="",
                    remote_uxon="uxon",
                    interval=10.0,
                    connect_timeout=1.5,
                    total_timeout=3.25,
                    extra_ssh_options=(),
                    command_template=None,
                    color=None,
                ),
            ],
        )
        for name in ("prod-1", "host_42", "edge.eu"):
            with self.subTest(name=name):
                self.assertEqual(
                    parse_config({"remote_hosts": [{"name": name, "ssh_alias": "x"}]})
                    .remote_hosts[0]
                    .name,
                    name,
                )

    def test_invalid_peer_records_name_the_field(self) -> None:
        valid = {"name": "peer", "ssh_alias": "edge"}
        for raw, field in (
            ("peer", "remote_hosts"),
            (["peer"], "remote_hosts"),
            ([{"ssh_alias": "edge"}], "name"),
            ([dict(valid, name=" ")], "name"),
            ([{"name": "peer"}], "ssh_alias"),
            ([valid, valid], "duplicate name"),
            ([dict(valid, description=42)], "description"),
            ([dict(valid, remote_uxon="")], "remote_uxon"),
            ([dict(valid, ssh_alaias="typo")], "ssh_alaias"),
            ([dict(valid, intervall=5)], "intervall"),
            ([dict(valid, color=" ")], "color"),
            ([dict(valid, extra_ssh_options="-o")], "extra_ssh_options"),
            ([dict(valid, extra_ssh_options=[1])], "extra_ssh_options"),
            ([dict(valid, extra_ssh_options=[""])], "extra_ssh_options"),
            ([dict(valid, command_template=[])], "command_template"),
            ([dict(valid, command_template="ssh")], "command_template"),
        ):
            with self.subTest(raw=raw), self.assertRaises(ConfigValidationError) as error:
                parse_config({"remote_hosts": raw})
            self.assertIn(field, str(error.exception))
        for name in ("foo bar", "foo/bar", "foo:bar", "foo$bar", "../etc", "é"):
            with self.subTest(name=name), self.assertRaises(ConfigValidationError) as error:
                parse_config({"remote_hosts": [dict(valid, name=name)]})
            self.assertIn("name", str(error.exception))

    def test_every_duration_field_rejects_nonpositive_malformed_and_nonfinite_values(self) -> None:
        for field in ("interval", "connect_timeout", "total_timeout"):
            for value in (
                0,
                "0ms",
                -1,
                "-1s",
                True,
                False,
                None,
                [],
                "",
                "1h",
                "ms",
                "abc",
                float("nan"),
                float("inf"),
                10**400,
                "NaN",
                "Infms",
                "1e309s",
                "1e308m",
            ):
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaises(ConfigValidationError) as error,
                ):
                    parse_config(
                        {"remote_hosts": [{"name": "peer", "ssh_alias": "edge", field: value}]}
                    )
                self.assertIn(field, str(error.exception))


def test_public_timeout_strings_reach_actual_fetch_with_ceil_and_default_deadlines(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = parse_config(
        tomllib.loads("""
[[remote_hosts]]
name = "fractional"
ssh_alias = "fractional"
connect_timeout = "500ms"
total_timeout = "1.5s"
[[remote_hosts]]
name = "defaults"
ssh_alias = "defaults"
[[remote_hosts]]
name = "minimum"
ssh_alias = "minimum"
connect_timeout = "1ms"
total_timeout = "1ms"
""")
    )
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("UXON_DEMO_HOSTS", raising=False)
    expected = iter((("fractional", 1, 2), ("defaults", 5, 10), ("minimum", 1, 1)))
    calls = []

    def query(argv, **kwargs):
        alias, connect, total = next(expected)
        assert argv[0] == "ssh" and argv[-2] == alias
        assert f"ConnectTimeout={connect}" in argv
        assert kwargs["timeout"] == total
        calls.append(alias)
        return subprocess.CompletedProcess(argv, 255, "", "fixture transport unavailable")

    monkeypatch.setattr(subprocess, "run", query)
    for host in cfg.remote_hosts:
        result = fetch_remote_snapshot(host, ssh_multiplex="off")
        assert result.sessions == [] and result.host_stats is None
        assert "fixture transport unavailable" in result.error
    assert calls == ["fractional", "defaults", "minimum"]
    assert next(expected, None) is None

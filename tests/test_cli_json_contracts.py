"""Public version and peer JSON consumers use strict external protocols."""

import io
import json
import os
import pwd
import shlex
import subprocess
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from helpers import make_config

from uxon import __version__
from uxon.app import listing
from uxon.cli.main import main
from uxon.cli.parsing import parse_args
from uxon.infra import config_loader, probes, tmux, version_probe
from uxon.infra.remote import collector
from uxon.infra.remote.envelope import decode_envelope
from uxon.infra.remote_hosts import RemoteHost

_REAL_PROBE_HOST = probes.probe_host


def test_public_version_human_and_json_cover_clean_dirty_missing_and_foreign_checkout(
    tmp_path, monkeypatch, subtests
):
    operator = tmp_path / "config.toml"
    operator.write_text("[audit]\nenabled=false\n")
    monkeypatch.setattr(config_loader, "OPERATOR_CONFIG_PATH", operator)
    root = str(Path(version_probe.__file__).resolve().parents[3])
    base = ["git", "-c", f"safe.directory={root}", "-C", root]
    monkeypatch.setattr(probes, "probe_host", _REAL_PROBE_HOST)
    monkeypatch.setattr(
        probes,
        "run_query",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("version host probe")),
    )
    for commit, dirty in [("abc1234", False), ("abc1234", True), (None, False)]:
        with subtests.test(commit=commit, dirty=dirty):
            calls = []

            def git(argv, *, commit=commit, dirty=dirty, calls=calls, **kwargs):
                assert kwargs == {}
                allowed = [
                    base + ["rev-parse", "--short", "HEAD"],
                    base + ["update-index", "-q", "--refresh"],
                    base + ["status", "--porcelain", "--untracked-files=no"],
                ]
                assert argv in allowed
                calls.append(argv)
                if argv == allowed[0]:
                    return subprocess.CompletedProcess(argv, 0 if commit else 128, commit or "", "")
                return subprocess.CompletedProcess(
                    argv, 0, " M tracked.txt\n" if dirty and argv == allowed[2] else "", ""
                )

            with patch.object(version_probe, "run_query", side_effect=git):
                for argv in [["version"], ["version", "--json"]]:
                    with redirect_stdout(io.StringIO()) as output:
                        assert main(argv) == 0
                    if "--json" in argv:
                        assert json.loads(output.getvalue()) == {
                            "schema_version": "3",
                            "uxon_version": __version__,
                            "kind": "version",
                            "data": {
                                "uxon_version": __version__,
                                "commit": commit,
                                "commit_dirty": dirty,
                            },
                        }
                    else:
                        assert (
                            output.getvalue()
                            == f"uxon {__version__}"
                            + (f" ({commit}{'-dirty' if dirty else ''})" if commit else "")
                            + "\n"
                        )
            assert len(calls) == (6 if commit else 2)
    foreign = (
        tmp_path
        / "consumer"
        / ".venv"
        / "lib"
        / "python3.13"
        / "uxon"
        / "infra"
        / "version_probe.py"
    )
    foreign.parent.mkdir(parents=True)
    foreign.write_text("# installed module location\n")
    with (
        patch.object(version_probe, "__file__", str(foreign)),
        patch.object(
            version_probe, "run_query", side_effect=AssertionError("consumer Git metadata")
        ),
    ):
        with redirect_stdout(io.StringIO()) as output:
            assert main(["version", "--json"]) == 0
    assert json.loads(output.getvalue())["data"] == {
        "uxon_version": __version__,
        "commit": None,
        "commit_dirty": False,
    }


def test_public_peer_listing_decodes_jsonl_and_suppresses_failed_payloads(
    tmp_path, monkeypatch, subtests
):
    user = pwd.getpwuid(os.getuid()).pw_name
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = make_config(
        allowed_roots=[],
        tmux_socket_template=str(tmp_path / "uxon.sock"),
        ssh_multiplex="off",
        remote_hosts=[
            RemoteHost("prod", "prod-alias", "", "/opt/uxon"),
            RemoteHost("stage", "stage-alias", "", "uxon"),
        ],
    )
    records = {
        "prod-alias": {
            "user": "alice",
            "name": "uxon-example@claude",
            "short_id": "example@claude",
            "profile": "claude",
            "agent": "claude",
            "runtime": "direct",
            "runtime_resource": "",
            "active_path": "/work/example",
        },
        "stage-alias": {
            "user": "bob",
            "name": "uxon-unconfirmed@codex",
            "short_id": "unconfirmed@codex",
        },
    }
    calls = []

    def ssh(argv, *, timeout):
        assert argv[:-2] == [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            "-o",
            "ConnectTimeout=5",
        ]
        alias = argv[-2]
        assert alias in records and timeout == 10
        command = shlex.split(argv[-1])
        assert command[:4] == (
            ["/opt/uxon", "list", "--all-users", "--json"]
            if alias == "prod-alias"
            else ["uxon", "list", "--all-users", "--json"]
        )
        assert command[4] == "--audit-correlation-id" and len(command) == 6
        calls.append(alias)
        envelope = {
            "schema_version": "3",
            "uxon_version": "4.0.0",
            "kind": "list",
            "data": {"scope_users": [records[alias]["user"]], "sessions": [records[alias]]},
        }
        return subprocess.CompletedProcess(
            argv,
            0 if alias == "prod-alias" else 255,
            json.dumps(envelope),
            "" if alias == "prod-alias" else "peer-failure-sentinel",
        )

    def absent(argv, *, timeout):
        assert argv == [
            os.sys.executable,
            "-m",
            "uxon.infra.tmux_server_probe",
            "--socket",
            str(tmp_path / "uxon.sock"),
        ]
        assert timeout == cfg.execution.backend_for_user(user).probe_timeout_seconds
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"state": "absent", "sessions": [], "error": ""}), ""
        )

    monkeypatch.setattr(collector, "run_query", ssh)
    monkeypatch.setattr(tmux, "run_query", absent)
    with redirect_stdout(io.StringIO()) as output:
        assert listing.do_list(parse_args(["list", "--all-hosts", "--json"]), cfg, user) == 1
    lines = output.getvalue().splitlines()
    assert len(lines) == 3
    envs = [json.loads(line) for line in lines]
    assert calls == ["prod-alias", "stage-alias"]
    assert "host" not in envs[0]
    assert envs[0]["data"] == {
        "all_users": False,
        "scope_users": [user],
        "session_prefix": "uxon-",
        "sessions": [],
    }
    assert [env.get("host") for env in envs] == [None, "prod", "stage"]
    decoded = [decode_envelope(line) for line in lines]
    assert decoded[0].sessions == [] and decoded[0].error is None
    assert decoded[1].sessions == [records["prod-alias"]] and decoded[1].error is None
    assert decoded[2].sessions is None and "peer-failure-sentinel" in decoded[2].error
    assert envs[2]["data"]["sessions"] == []
    assert envs[2]["data"]["peer"] == {"uxon_version": "4.0.0", "schema_version": "3"}
    assert envs[2]["data"]["error"]["code"] == "ssh_error"
    for host, expected_rc, expected_records in [
        ("prod", 0, [records["prod-alias"]]),
        ("stage", 1, []),
    ]:
        with subtests.test(host=host):
            with redirect_stdout(io.StringIO()) as output:
                assert (
                    listing.do_list(parse_args(["list", "--host", host, "--json"]), cfg, user)
                    == expected_rc
                )
            envelope = json.loads(output.getvalue())
            assert envelope["host"] == host and envelope["kind"] == "list"
            assert envelope["data"]["sessions"] == expected_records
    for configured, host, diagnostic in [
        ([], "prod", "no [[remote_hosts]]"),
        (cfg.remote_hosts, "missing", "prod, stage"),
    ]:
        with subtests.test(host=host, configured=len(configured)):
            cfg.remote_hosts = configured
            import pytest

            with pytest.raises(SystemExit) as caught:
                listing.do_list(parse_args(["list", "--host", host, "--json"]), cfg, user)
            assert diagnostic in caught.value.uxon_msg
    assert calls == ["prod-alias", "stage-alias", "prod-alias", "stage-alias"]

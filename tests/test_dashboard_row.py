"""Local and wire adapters preserve routing identity and normalize sparse metadata."""

from dataclasses import replace

from uxon.domain.session import SessionIdentity
from uxon.tui.context import TuiSession
from uxon.tui.dashboard.row import from_tui_session, from_wire_record


def _local() -> TuiSession:
    return TuiSession(
        name="uxon-proj@fast",
        short="proj@fast",
        attached=True,
        pid="4321",
        cpu="5.0",
        ram="1.2G",
        created="now",
        last_activity="now",
        cmd="claude run",
        path="/srv/proj",
        user="alice",
        stem="proj",
        profile="fast",
        agent="claude",
        legacy=True,
        created_iso="2026-05-01T12:00:00+00:00",
        last_attached_iso="2026-05-01T13:00:00+00:00",
    )


def test_adapters_project_values_and_distinct_routing_keys() -> None:
    local = from_tui_session(_local())
    remote = from_wire_record(
        "peer-a",
        dict(
            name="uxon-proj@fast",
            short_id="proj@fast",
            user="bob",
            agent="claude",
            profile="fast",
            runtime="box",
            runtime_resource="uxon-box",
            attached=True,
            active_pid=1234,
            cpu_pct=12.3,
            rss_kib=4096,
            active_cmd="claude",
            active_path="/srv/remote",
            legacy=True,
            created="2026-05-01T12:00:00+00:00",
            last_attached="2026-05-01T13:00:00+00:00",
        ),
    )

    def projection(r):
        return (
            r.host,
            r.user,
            r.name,
            r.short,
            r.profile,
            r.agent,
            r.runtime,
            r.runtime_resource,
            r.attached,
            r.legacy,
            r.pid,
            r.cpu_pct,
            r.rss_kib,
            r.cmd,
            r.path,
            r.created_epoch,
            r.last_attached_epoch,
        )

    assert projection(local) == (
        None,
        "alice",
        "uxon-proj@fast",
        "proj@fast",
        "fast",
        "claude",
        "",
        "",
        True,
        True,
        4321,
        5.0,
        1258291,
        "claude run",
        "/srv/proj",
        1777636800.0,
        1777640400.0,
    )
    assert projection(remote) == (
        "peer-a",
        "bob",
        "uxon-proj@fast",
        "proj@fast",
        "fast",
        "claude",
        "box",
        "uxon-box",
        True,
        True,
        1234,
        12.3,
        4096,
        "claude",
        "/srv/remote",
        1777636800.0,
        1777640400.0,
    )
    assert (local.key, remote.key) == (
        "local/alice/uxon-proj@fast",
        "remote:peer-a/bob/uxon-proj@fast",
    )


def test_row_identity_distinguishes_peer_local_and_preserves_launch_continuity():
    identity = SessionIdentity(
        "alice",
        "uxon-proj@fast",
        "$1",
        "100",
        "a" * 32,
        "/tmp/tmux-1001/uxon-local.sock",
        "local",
        "fingerprint",
    )
    local = replace(from_tui_session(_local()), identity=identity)
    assert local.key != replace(local, host="local").key
    assert (
        local.key == replace(local, name="renamed", identity=replace(identity, name="renamed")).key
    )
    legacy = replace(identity, launch_nonce="")
    assert legacy.key == replace(legacy, name="renamed").key
    assert legacy.key != replace(legacy, created="101").key
    assert identity.key != replace(identity, launch_nonce="b" * 32).key


def test_local_numeric_units_and_invalid_metadata() -> None:
    base = _local()
    for pid, cpu, ram, created, last, expected in (
        ("-", "-", "-", "", "", (None, 0.0, 0, None, None)),
        ("", "", "", "", "", (None, 0.0, 0, None, None)),
        ("abc", "bogus", "garbage", "invalid", "invalid", (None, 0.0, 0, None, None)),
        ("42", "12.3", "456M", "", "", (42, 12.3, 466944, None, None)),
        ("42", "12.3", "456 MiB", "", "", (42, 12.3, 466944, None, None)),
        ("42", "12.3", "42K", "", "", (42, 12.3, 42, None, None)),
        ("42", "12.3", "1.5 GiB", "", "", (42, 12.3, 1572864, None, None)),
    ):
        row = from_tui_session(
            replace(
                base,
                pid=pid,
                cpu=cpu,
                ram=ram,
                created_iso=created,
                last_attached_iso=last,
                legacy=False,
            )
        )
        assert (
            row.pid,
            row.cpu_pct,
            row.rss_kib,
            row.created_epoch,
            row.last_attached_epoch,
        ) == expected, (pid, cpu, ram)
        assert not row.legacy


def test_sparse_wire_and_invalid_timestamp_pid_fallbacks() -> None:
    sparse = from_wire_record("old-peer", {"user": "bob", "name": "n"})
    assert (
        sparse.host,
        sparse.user,
        sparse.name,
        sparse.short,
        sparse.agent,
        sparse.attached,
        sparse.legacy,
        sparse.pid,
        sparse.cpu_pct,
        sparse.rss_kib,
        sparse.created_epoch,
        sparse.last_attached_epoch,
        sparse.cmd,
        sparse.path,
        sparse.profile,
        sparse.runtime,
        sparse.runtime_resource,
        sparse.runtime_down,
        sparse.exited,
        sparse.active_exit_status,
        sparse.has_diagnostics,
    ) == (
        "old-peer",
        "bob",
        "n",
        "n",
        "",
        False,
        False,
        None,
        0.0,
        0,
        None,
        None,
        "",
        "",
        "",
        "",
        "",
        False,
        False,
        None,
        False,
    )
    invalid = from_wire_record(
        "old-peer",
        {
            "user": "bob",
            "name": "n",
            "short_id": "",
            "active_pid": "42",
            "created": "bad",
            "last_attached": "",
            "legacy": False,
        },
    )
    assert invalid == sparse

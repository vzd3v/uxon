"""Survivors keep their positions while arrivals follow the host-grouped model."""

from dataclasses import replace

from uxon.tui.dashboard.order import place
from uxon.tui.dashboard.row import SessionRow


def _row(name, host=None, last=1.0):
    return SessionRow(
        host=host,
        user="alice",
        name=name,
        short=name,
        agent="claude",
        attached=False,
        legacy=False,
        pid=None,
        cpu_pct=0.0,
        rss_kib=0,
        created_epoch=None,
        last_attached_epoch=last,
        cmd="",
        path="",
    )


def test_survivor_order_fresh_telemetry_arrivals_and_departures() -> None:
    a, b, c = (_row(name, last=last) for name, last in (("a", 3.0), ("b", 2.0), ("c", 1.0)))
    initial = (a, b, c)
    order = tuple(r.key for r in initial)
    fresh_b = replace(b, cpu_pct=99.0, last_attached_epoch=500.0)
    result = place(order, (fresh_b, a, c))
    assert result == (a, fresh_b, c)
    assert initial == (a, b, c) and b.cpu_pct == 0.0
    x, y = _row("x", last=999.0), _row("y", last=2.5)
    arrived = place(tuple(r.key for r in result), (x, a, y, fresh_b, c))
    assert arrived == (x, a, y, fresh_b, c)
    departed = place(tuple(r.key for r in arrived), (a, y, c))
    assert departed == (a, y, c)
    d = _row("d", last=2.0)
    mixed = place(tuple(r.key for r in departed), (a, d, c))
    assert mixed == (a, d, c)
    assert place(tuple(r.key for r in mixed), ()) == ()
    assert arrived == (x, a, y, fresh_b, c)


def test_cold_start_and_arrivals_preserve_host_blocks() -> None:
    local_new, local_old = _row("local-new", last=2.0), _row("local-old", last=1.0)
    peer_new, peer_old = (
        _row("peer-new", host="r", last=100.0),
        _row("peer-old", host="r", last=99.0),
    )
    current = (local_new, local_old, peer_new, peer_old)
    result = place((), current)
    assert result == (local_new, local_old, peer_new, peer_old)
    arrival = _row("arrival", host="r", last=999.0)
    result = place(
        tuple(r.key for r in result), (local_new, local_old, arrival, peer_new, peer_old)
    )
    assert result == (local_new, local_old, arrival, peer_new, peer_old)
    assert place(tuple(r.key for r in result), ()) == ()

from __future__ import annotations

from uxon.tui.dashboard.buckets import FleetSummary
from uxon.tui.widgets.fleet_status_bar import format_collapsed


def _summary(alerts=(), *, hosts=3, sessions=8):
    return FleetSummary(host_count=hosts, session_count=sessions, alerts=tuple(alerts))


def test_collapsed_counts_alert_cap_and_remainder():
    for alerts, expected in (
        ((), "3 hosts · 8 sess"),
        (
            ("gpu-box mem 92%", "dev-box unreachable"),
            "3 hosts · 8 sess · ⚠ gpu-box mem 92% · ⚠ dev-box unreachable",
        ),
        (
            ("a unreachable", "b unreachable", "c unreachable", "d unreachable"),
            "3 hosts · 8 sess · ⚠ a unreachable · ⚠ b unreachable · +2 more",
        ),
    ):
        assert format_collapsed(_summary(alerts)) == expected

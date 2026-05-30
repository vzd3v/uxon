from __future__ import annotations

from uxon.tui.dashboard.buckets import FleetSummary
from uxon.tui.widgets.fleet_status_bar import format_collapsed


def _summary(alerts=(), *, hosts=3, sessions=8):
    return FleetSummary(host_count=hosts, session_count=sessions, alerts=tuple(alerts))


def test_collapsed_counts_only_when_no_alerts():
    assert format_collapsed(_summary()) == "3 hosts · 8 sess"


def test_collapsed_appends_alert_tokens_with_warning_glyph():
    out = format_collapsed(_summary(("gpu-box mem 92%", "dev-nadia unreachable")))
    assert out == "3 hosts · 8 sess · ⚠ gpu-box mem 92% · ⚠ dev-nadia unreachable"


def test_collapsed_caps_alerts_and_summarises_remainder():
    out = format_collapsed(
        _summary(("a unreachable", "b unreachable", "c unreachable", "d unreachable")),
        max_alerts=2,
    )
    assert out == "3 hosts · 8 sess · ⚠ a unreachable · ⚠ b unreachable · +2 more"


def test_collapsed_exactly_at_cap_has_no_more_suffix():
    out = format_collapsed(_summary(("a unreachable", "b unreachable")), max_alerts=2)
    assert "more" not in out

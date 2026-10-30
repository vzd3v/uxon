"""Exact cell outputs at telemetry and naming boundaries."""

from dataclasses import replace
from unittest.mock import patch

from rich.text import Text

from uxon.tui.dashboard.columns import REGISTRY, format_cpu, format_ram, format_relative_time
from uxon.tui.dashboard.row import SessionRow


def _row():
    return SessionRow(
        host=None,
        user="alice",
        name="uxon-foo@claude",
        short="foo",
        agent="claude",
        attached=False,
        legacy=False,
        pid=42,
        cpu_pct=0.0,
        rss_kib=0,
        created_epoch=None,
        last_attached_epoch=None,
        cmd="claude run",
        path="/srv/foo",
    )


def test_cpu_thresholds_and_numeric_zero() -> None:
    row = _row()
    for cpu, plain, style in (
        (0.0, "0.0", ""),
        (10.0, "10.0", ""),
        (10.5, "10.5", "yellow"),
        (50.0, "50.0", "yellow"),
        (50.1, "50.1", "bold red"),
        (100.0, "100", "bold red"),
        (120.0, "120", "bold red"),
    ):
        text = format_cpu(replace(row, cpu_pct=cpu))
        assert (text.plain, str(text.style)) == (plain, style), cpu


def test_ram_scale_boundaries() -> None:
    row = _row()
    for kib, expected in (
        (-1, "-"),
        (0, "-"),
        (1023, "1023K"),
        (1024, "1M"),
        (1048575, "1024M"),
        (1048576, "1.0G"),
        (1572864, "1.5G"),
    ):
        assert format_ram(replace(row, rss_kib=kib)) == expected, kib


def test_relative_time_boundaries_and_clock_skew() -> None:
    now = 500000.0
    for age, expected in (
        (None, "-"),
        (-1, "0s"),
        (0, "0s"),
        (59, "59s"),
        (60, "1m"),
        (3599, "59m"),
        (3600, "1h"),
        (86399, "23h"),
        (86400, "1d"),
    ):
        epoch = None if age is None else now - age
        assert format_relative_time(epoch, now=now) == expected, age


def test_profile_names_attachment_and_legacy_agent_labels() -> None:
    columns = {c.id: c for c in REGISTRY}
    row = _row()
    for short, profile, agent, attached, legacy, name_out, agent_out in (
        ("foo", "", "claude", False, False, "○ foo", "claude"),
        ("foo", "", "claude", True, False, "● foo", "claude"),
        ("billing-api@claude", "", "claude", False, False, "○ billing-api", "claude"),
        ("billing-api@fast", "fast", "claude", False, False, "○ billing-api", "claude"),
        ("proj@fast-2", "fast", "claude", False, False, "○ proj-2", "claude"),
        (
            "weird@claude_in_name@claude",
            "",
            "claude",
            False,
            False,
            "○ weird@claude_in_name",
            "claude",
        ),
        ("legacy-name", "", "claude", False, True, "○ legacy-name", "claude (legacy)"),
        ("foo", "", "codex", False, True, "○ foo", "codex"),
        ("foo", "", "codex", False, False, "○ foo", "codex"),
        ("foo", "", "", False, False, "○ foo", "-"),
    ):
        current = replace(
            row, short=short, profile=profile, agent=agent, attached=attached, legacy=legacy
        )
        assert (columns["name"].format(current).plain, columns["agent"].format(current)) == (
            name_out,
            agent_out,
        ), short


def test_normal_and_sparse_optional_cells() -> None:
    columns = {c.id: c for c in REGISTRY}
    normal = replace(_row(), host="peer-a", created_epoch=99880.0, last_attached_epoch=99940.0)
    sparse = replace(
        normal,
        host=None,
        user="",
        pid=None,
        cmd="",
        path="",
        created_epoch=None,
        last_attached_epoch=None,
    )
    ids = ("host", "user", "pid", "cmd", "path", "new", "last")
    with patch("uxon.tui.dashboard.columns.time.time", return_value=100000.0):
        for row, expected in (
            (normal, ("peer-a", "alice", "42", "claude run", "/srv/foo", "2m", "1m")),
            (sparse, ("local", "-", "-", "-", "-", "-", "-")),
        ):
            cells = tuple(columns[c].format(row) for c in ids)
            assert tuple(c.plain if isinstance(c, Text) else c for c in cells) == expected
            assert all(str(c.style) == "" for c in cells if isinstance(c, Text))

"""Tests for :mod:`uxon.tui.dashboard.columns`.

Each formatter is a pure function over a :class:`SessionRow`; sort
keys are pure functions producing comparable values. Visual semantics:
attached state is shown via a ``●``/``○`` glyph (no colour override),
CPU is red/yellow at >50/>10, and per-host block hue is layered by
the widget at dispatch time — formatters emit plain ``Text``.
"""

from __future__ import annotations

import unittest
from typing import Any

from rich.text import Text

from uxon.tui.dashboard import KNOWN_COLUMN_IDS
from uxon.tui.dashboard.columns import (
    REGISTRY,
    ColumnSpec,
    assign_block_colors,
    format_cpu,
    format_ram,
    format_relative_time,
)
from uxon.tui.dashboard.row import SessionRow


def _row(**overrides: Any) -> SessionRow:
    base: dict[str, Any] = dict(
        host=None,
        user="alice",
        name="cc-foo",
        short="foo",
        agent="claude",
        attached=False,
        legacy=False,
        pid=1234,
        cpu_pct=5.0,
        rss_kib=4096,
        created_epoch=None,
        last_attached_epoch=None,
        cmd="claude",
        path="/home/alice/foo",
    )
    base.update(overrides)
    return SessionRow(**base)


def _by_id(col_id: str) -> ColumnSpec:
    for c in REGISTRY:
        if c.id == col_id:
            return c
    raise KeyError(col_id)


class RegistryShapeTests(unittest.TestCase):
    def test_registry_has_twelve_columns(self) -> None:
        self.assertEqual(len(REGISTRY), 12)

    def test_registry_ids_unique(self) -> None:
        ids = [c.id for c in REGISTRY]
        self.assertEqual(len(ids), len(set(ids)))

    def test_expected_column_ids_present(self) -> None:
        ids = {c.id for c in REGISTRY}
        self.assertEqual(
            ids,
            {
                "host",
                "user",
                "name",
                "agent",
                "cpu",
                "ram",
                "new",
                "last",
                "cmd",
                "path",
                "pid",
                "wins",
            },
        )

    def test_show_when_gating(self) -> None:
        self.assertEqual(_by_id("host").show_when, "multi_host")
        self.assertEqual(_by_id("user").show_when, "cross_user")
        self.assertEqual(_by_id("name").show_when, "always")

    def test_default_visibility(self) -> None:
        self.assertFalse(_by_id("host").default_visible)
        self.assertFalse(_by_id("user").default_visible)
        self.assertFalse(_by_id("pid").default_visible)
        self.assertFalse(_by_id("wins").default_visible)
        # PATH and CMD flip to off-by-default in 3.4 — both duplicate
        # information already in NAME/AGENT for uxon-launched sessions
        # and pushed useful columns off-screen on narrow terminals.
        # Operators opt back in via ``[tui.table] columns``.
        self.assertFalse(_by_id("path").default_visible)
        self.assertFalse(_by_id("cmd").default_visible)
        for col_id in ("name", "agent", "cpu", "ram", "new", "last"):
            self.assertTrue(_by_id(col_id).default_visible, col_id)

    def test_alignment(self) -> None:
        for col_id in ("cpu", "ram", "new", "last", "pid", "wins"):
            self.assertEqual(_by_id(col_id).align, "right", col_id)
        for col_id in ("host", "user", "name", "agent", "cmd", "path"):
            self.assertEqual(_by_id(col_id).align, "left", col_id)

    def test_known_column_ids_matches_registry(self) -> None:
        # ``KNOWN_COLUMN_IDS`` is the lightweight mirror imported by
        # ``uxon.cli`` (which must stay Rich-free); a drift between
        # the two would silently break config validation. Order must
        # match too — config.example.toml documents the lists in
        # registry order.
        self.assertEqual(KNOWN_COLUMN_IDS, tuple(c.id for c in REGISTRY))


class AssignBlockColorsSmokeTests(unittest.TestCase):
    """One smoke case for ``assign_block_colors``.

    Full cycle / pin / adjacency coverage lives in
    ``tests/test_dashboard_block_colors.py``. This case keeps the
    column module wired through via at least one direct import.
    """

    def test_locals_only(self) -> None:
        out = assign_block_colors((), local_color="green", palette=("cyan",))
        self.assertEqual(out, {None: "green"})


class FormatCpuTests(unittest.TestCase):
    def test_zero_renders_numeric(self) -> None:
        # The legacy idle-CPU rendering emitted "0.0" for idle
        # sessions; the unified pipeline preserves that — only a
        # missing input would blank the cell, but the adapter normalises
        # missing→0.0 at the boundary so we always emit a number.
        text = format_cpu(_row(cpu_pct=0.0))
        self.assertEqual(text.plain, "0.0")
        self.assertEqual(str(text.style), "")

    def test_low_cpu_plain_style(self) -> None:
        text = format_cpu(_row(cpu_pct=5.0))
        self.assertEqual(text.plain, "5.0")
        # No colour style attached.
        self.assertEqual(str(text.style), "")

    def test_warm_cpu_yellow(self) -> None:
        text = format_cpu(_row(cpu_pct=25.0))
        self.assertEqual(text.plain, "25.0")
        self.assertEqual(str(text.style), "yellow")

    def test_hot_cpu_bold_red(self) -> None:
        text = format_cpu(_row(cpu_pct=75.0))
        self.assertEqual(text.plain, "75.0")
        self.assertEqual(str(text.style), "bold red")

    def test_thresholds_match_legacy(self) -> None:
        # >50 → bold red; >10 → yellow; <=10 → plain. Mirrors the
        # legacy idle-CPU rendering.
        self.assertEqual(str(format_cpu(_row(cpu_pct=10.0)).style), "")
        self.assertEqual(str(format_cpu(_row(cpu_pct=10.5)).style), "yellow")
        self.assertEqual(str(format_cpu(_row(cpu_pct=50.0)).style), "yellow")
        self.assertEqual(str(format_cpu(_row(cpu_pct=50.1)).style), "bold red")

    def test_format_above_100(self) -> None:
        text = format_cpu(_row(cpu_pct=120.0))
        self.assertEqual(text.plain, "120")


class FormatRamTests(unittest.TestCase):
    def test_zero_dash(self) -> None:
        self.assertEqual(format_ram(_row(rss_kib=0)), "-")

    def test_small_kib(self) -> None:
        self.assertEqual(format_ram(_row(rss_kib=512)), "512K")

    def test_mib(self) -> None:
        self.assertEqual(format_ram(_row(rss_kib=2048)), "2M")

    def test_gib(self) -> None:
        self.assertEqual(format_ram(_row(rss_kib=int(1.5 * 1024 * 1024))), "1.5G")


class FormatRelativeTimeTests(unittest.TestCase):
    def test_none_dash(self) -> None:
        self.assertEqual(format_relative_time(None, now=1000.0), "-")

    def test_seconds(self) -> None:
        self.assertEqual(format_relative_time(995.0, now=1000.0), "5s")

    def test_minutes(self) -> None:
        self.assertEqual(format_relative_time(700.0, now=1000.0), "5m")

    def test_hours(self) -> None:
        # 2 hours = 7200s.
        self.assertEqual(format_relative_time(0.0, now=7200.0), "2h")

    def test_days(self) -> None:
        # 3 days = 259200s.
        self.assertEqual(format_relative_time(0.0, now=3 * 86400.0), "3d")

    def test_future_clamps_to_zero(self) -> None:
        # Clock skew safety: epoch ahead of "now" → 0s, not negative.
        self.assertEqual(format_relative_time(1000.0, now=900.0), "0s")


class NameFormatterTests(unittest.TestCase):
    def test_local_row_emits_plain_text_with_unattached_glyph(self) -> None:
        col = _by_id("name")
        text = col.format(_row(host=None, short="foo", attached=False))
        self.assertIsInstance(text, Text)
        # ``○`` is the unattached glyph; block hue is layered by the
        # widget at render time, NOT by the formatter. The formatter
        # stays pure data so the reconciler can diff cells.
        self.assertEqual(text.plain, "○ foo")

    def test_remote_row_emits_plain_text(self) -> None:
        col = _by_id("name")
        text = col.format(_row(host="peer-1", short="bar"))
        self.assertEqual(text.plain, "○ bar")

    def test_attached_uses_filled_glyph(self) -> None:
        col = _by_id("name")
        text = col.format(_row(attached=True, short="foo"))
        # Attach state is encoded by the glyph (●/○), not by colour.
        self.assertEqual(text.plain, "● foo")

    def test_strips_agent_suffix(self) -> None:
        # Sessions are named ``<prefix><stem>@<agent>``; ``row.short``
        # is the prefix-stripped form ``<stem>@<agent>``. The AGENT
        # column carries the agent already, so NAME drops it.
        col = _by_id("name")
        text = col.format(_row(short="vz_devagent_cli_tool@claude", agent="claude"))
        self.assertEqual(text.plain, "○ vz_devagent_cli_tool")

    def test_preserves_disambiguator_index(self) -> None:
        # ``-N`` comes after ``@<agent>`` in the session name. Two
        # siblings at the same stem rely on this number to stay
        # visually distinct after the agent is stripped.
        col = _by_id("name")
        text = col.format(_row(short="proj@claude-2", agent="claude"))
        self.assertEqual(text.plain, "○ proj-2")

    def test_agent_substring_in_stem_is_safe(self) -> None:
        # If a stem coincidentally contains ``@<agent>`` (unlikely but
        # not impossible when stems are derived from filesystem
        # paths), only the trailing suffix is stripped — not the
        # earlier substring match.
        col = _by_id("name")
        text = col.format(_row(short="weird@claude_in_name@claude", agent="claude"))
        self.assertEqual(text.plain, "○ weird@claude_in_name")

    def test_unparseable_short_passes_through(self) -> None:
        # Legacy / unrecognised names that don't carry the suffix
        # render as-is — never invent a stripped form.
        col = _by_id("name")
        text = col.format(_row(short="legacy-name", agent="claude"))
        self.assertEqual(text.plain, "○ legacy-name")


class AgentFormatterTests(unittest.TestCase):
    def test_legacy_claude_marked(self) -> None:
        col = _by_id("agent")
        self.assertEqual(col.format(_row(agent="claude", legacy=True)), "claude (legacy)")

    def test_non_legacy_plain(self) -> None:
        col = _by_id("agent")
        self.assertEqual(col.format(_row(agent="codex", legacy=False)), "codex")

    def test_legacy_codex_does_not_get_marker(self) -> None:
        # The "(legacy)" suffix is reserved for the historical
        # cc-<stem> claude shape; non-claude legacy is impossible
        # in practice.
        col = _by_id("agent")
        self.assertEqual(col.format(_row(agent="codex", legacy=True)), "codex")

    def test_empty_agent_dash(self) -> None:
        col = _by_id("agent")
        self.assertEqual(col.format(_row(agent="", legacy=False)), "-")


class UserFormatterTests(unittest.TestCase):
    def test_user_renders_plain(self) -> None:
        # The USER column header signals cross-user mode; per-row colour
        # would also paint the operator's own user, which diverges from
        # the legacy intent (yellow was a non-self marker on the
        # dedicated #sessions-other table). Render plain.
        col = _by_id("user")
        text = col.format(_row(user="alice"))
        self.assertIsInstance(text, Text)
        self.assertEqual(text.plain, "alice")
        self.assertEqual(str(text.style), "")

    def test_user_missing_dash(self) -> None:
        col = _by_id("user")
        text = col.format(_row(user=""))
        self.assertEqual(text.plain, "-")
        self.assertEqual(str(text.style), "")


class HostFormatterTests(unittest.TestCase):
    def test_local_renders_local(self) -> None:
        col = _by_id("host")
        text = col.format(_row(host=None))
        self.assertEqual(text.plain, "local")

    def test_remote_renders_host_name(self) -> None:
        # Block hue is layered by the widget at dispatch time, not by
        # the formatter.
        col = _by_id("host")
        text = col.format(_row(host="peer-1"))
        self.assertEqual(text.plain, "peer-1")


class SimpleFormatterTests(unittest.TestCase):
    def test_pid_int_to_string(self) -> None:
        self.assertEqual(_by_id("pid").format(_row(pid=42)), "42")

    def test_pid_none_dash(self) -> None:
        self.assertEqual(_by_id("pid").format(_row(pid=None)), "-")

    def test_cmd_empty_dash(self) -> None:
        self.assertEqual(_by_id("cmd").format(_row(cmd="")), "-")
        self.assertEqual(_by_id("cmd").format(_row(cmd="claude run")), "claude run")

    def test_path_empty_dash(self) -> None:
        self.assertEqual(_by_id("path").format(_row(path="")), "-")

    def test_wins_placeholder(self) -> None:
        # WINS column ships in REGISTRY for forward-compat but renders
        # "-" until SessionRow gains the windows field (follow-up).
        self.assertEqual(_by_id("wins").format(_row()), "-")


class RelativeColumnFormatterTests(unittest.TestCase):
    def test_new_renders_relative(self) -> None:
        # Use a fixed now via the helper directly; column wraps it
        # without a now param, so test the helper instead.
        self.assertEqual(format_relative_time(0.0, now=120.0), "2m")

    def test_new_none_dash(self) -> None:
        self.assertEqual(_by_id("new").format(_row(created_epoch=None)), "-")

    def test_last_none_dash(self) -> None:
        self.assertEqual(_by_id("last").format(_row(last_attached_epoch=None)), "-")


class SortKeyTests(unittest.TestCase):
    def test_cpu_desc(self) -> None:
        rows = [
            _row(name="a", cpu_pct=5.0),
            _row(name="b", cpu_pct=80.0),
            _row(name="c", cpu_pct=20.0),
        ]
        key = _by_id("cpu").sort_key
        ordered = sorted(rows, key=key, reverse=True)
        self.assertEqual([r.name for r in ordered], ["b", "c", "a"])

    def test_ram_desc(self) -> None:
        rows = [
            _row(name="a", rss_kib=1024),
            _row(name="b", rss_kib=8192),
            _row(name="c", rss_kib=2048),
        ]
        key = _by_id("ram").sort_key
        ordered = sorted(rows, key=key, reverse=True)
        self.assertEqual([r.name for r in ordered], ["b", "c", "a"])

    def test_last_desc_with_none(self) -> None:
        rows = [
            _row(name="a", last_attached_epoch=100.0),
            _row(name="b", last_attached_epoch=None),
            _row(name="c", last_attached_epoch=500.0),
        ]
        key = _by_id("last").sort_key
        ordered = sorted(rows, key=key, reverse=True)
        # Most-recent first; missing epochs sort to bottom.
        self.assertEqual([r.name for r in ordered], ["c", "a", "b"])

    def test_name_asc(self) -> None:
        rows = [_row(name=n, short=n) for n in ("c", "a", "b")]
        key = _by_id("name").sort_key
        ordered = sorted(rows, key=key)
        self.assertEqual([r.name for r in ordered], ["a", "b", "c"])

    def test_host_local_before_remote(self) -> None:
        rows = [
            _row(name="a", host="peer-z"),
            _row(name="b", host=None),
            _row(name="c", host="peer-a"),
        ]
        key = _by_id("host").sort_key
        ordered = sorted(rows, key=key)
        self.assertEqual([r.name for r in ordered], ["b", "c", "a"])

    def test_new_and_last_sort_with_mixed_none_and_float(self) -> None:
        # Defensive: a row may still have ``created_epoch=None`` when
        # the source carries no ISO timestamp at all (older test
        # fixtures, partial wire records, or a local TuiSession built
        # without ``created_iso``). Sort by ``_sort_new`` must be
        # deterministic and not crash. ``float("-inf")`` fallback puts
        # None-epoch rows first asc, last desc — pinning the ordering
        # so a future tweak surfaces. Production local rows now carry
        # real epochs (Goal 4); this test exercises only the fallback.
        rows = [
            _row(name="local1", host=None, created_epoch=None),
            _row(name="local2", host=None, created_epoch=None),
            _row(name="remote-old", host="peer-a", created_epoch=1234567890.0),
            _row(name="remote-new", host="peer-b", created_epoch=1234567900.0),
        ]
        key_new = _by_id("new").sort_key

        asc = sorted(rows, key=key_new)
        # None-epoch rows sit at the front in ascending sort.
        self.assertEqual(
            [r.name for r in asc],
            ["local1", "local2", "remote-old", "remote-new"],
        )

        desc = sorted(rows, key=key_new, reverse=True)
        # In descending order they fall to the back.
        self.assertEqual(
            [r.name for r in desc],
            ["remote-new", "remote-old", "local1", "local2"],
        )

        # Mirror behaviour for the LAST column: build rows where
        # ``last_attached_epoch`` carries the same mixed shape.
        rows_last = [
            _row(name="L1", last_attached_epoch=None),
            _row(name="L2", last_attached_epoch=None),
            _row(name="R-old", last_attached_epoch=100.0),
            _row(name="R-new", last_attached_epoch=500.0),
        ]
        key_last = _by_id("last").sort_key
        asc_last = sorted(rows_last, key=key_last)
        self.assertEqual([r.name for r in asc_last], ["L1", "L2", "R-old", "R-new"])


if __name__ == "__main__":
    unittest.main()

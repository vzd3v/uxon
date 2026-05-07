"""Tests for :mod:`uxon.tui.dashboard.layout`.

The selector picks the active column tuple from three inputs:
optional user config, runtime layout flags, and the registry as the
source of truth for defaults and show_when gates. Unknown ids must
drop silently (forward-compat for old TOMLs); show_when gates must
be respected on both code paths (defaults + explicit cfg).
"""

from __future__ import annotations

import unittest

from uxon.tui.dashboard import layout as layout_mod
from uxon.tui.dashboard.columns import REGISTRY
from uxon.tui.dashboard.layout import LayoutFlags, build_active_columns


def _ids(cols: tuple) -> list[str]:
    return [c.id for c in cols]


class _LayoutTestBase(unittest.TestCase):
    """Shared setup: reset the once-per-process unknown-id memo so
    state from one test cannot silently mask a missing warn in the
    next. ``_WARNED_UNKNOWN_IDS`` is module-level by design (production
    callers want once-per-process), so tests must clear it explicitly.
    """

    def setUp(self) -> None:
        super().setUp()
        layout_mod._reset_warned()

    def tearDown(self) -> None:
        layout_mod._reset_warned()
        super().tearDown()


class DefaultsPathTests(_LayoutTestBase):
    def test_no_cfg_single_host_single_user(self) -> None:
        flags = LayoutFlags(multi_host=False, cross_user=False)
        cols = build_active_columns(cfg_columns=None, flags=flags)
        # Defaults: every default_visible column, in registry order.
        # host/user/pid/wins are default_visible=False so excluded.
        self.assertEqual(
            _ids(cols),
            ["name", "agent", "cpu", "ram", "new", "last", "cmd", "path"],
        )

    def test_no_cfg_multi_host_pulls_in_host(self) -> None:
        flags = LayoutFlags(multi_host=True, cross_user=False)
        cols = build_active_columns(cfg_columns=None, flags=flags)
        # host is default_visible=False but show_when=multi_host now
        # matches → included in registry order (first).
        self.assertEqual(cols[0].id, "host")
        self.assertNotIn("user", _ids(cols))

    def test_no_cfg_cross_user_pulls_in_user(self) -> None:
        flags = LayoutFlags(multi_host=False, cross_user=True)
        cols = build_active_columns(cfg_columns=None, flags=flags)
        self.assertIn("user", _ids(cols))
        self.assertNotIn("host", _ids(cols))

    def test_no_cfg_multi_host_and_cross_user(self) -> None:
        flags = LayoutFlags(multi_host=True, cross_user=True)
        cols = build_active_columns(cfg_columns=None, flags=flags)
        ids = _ids(cols)
        # Registry order has host before user; both included.
        self.assertEqual(ids[0], "host")
        self.assertEqual(ids[1], "user")


class ExplicitCfgTests(_LayoutTestBase):
    def test_explicit_cfg_wins_over_defaults(self) -> None:
        flags = LayoutFlags(multi_host=False, cross_user=False)
        cols = build_active_columns(
            cfg_columns=("name", "cpu", "cmd"),
            flags=flags,
        )
        self.assertEqual(_ids(cols), ["name", "cpu", "cmd"])

    def test_explicit_cfg_preserves_user_order(self) -> None:
        flags = LayoutFlags(multi_host=False, cross_user=False)
        cols = build_active_columns(
            cfg_columns=("path", "name", "cpu"),
            flags=flags,
        )
        self.assertEqual(_ids(cols), ["path", "name", "cpu"])

    def test_unknown_id_silently_dropped(self) -> None:
        flags = LayoutFlags(multi_host=False, cross_user=False)
        cols = build_active_columns(
            cfg_columns=("name", "bogus_id", "cpu"),
            flags=flags,
        )
        self.assertEqual(_ids(cols), ["name", "cpu"])

    def test_show_when_gating_drops_unmet_id(self) -> None:
        # User asks for HOST but multi_host is False: drop it.
        flags = LayoutFlags(multi_host=False, cross_user=False)
        cols = build_active_columns(
            cfg_columns=("host", "name", "cpu"),
            flags=flags,
        )
        self.assertEqual(_ids(cols), ["name", "cpu"])

    def test_show_when_gating_keeps_met_id(self) -> None:
        flags = LayoutFlags(multi_host=True, cross_user=False)
        cols = build_active_columns(
            cfg_columns=("host", "name", "cpu"),
            flags=flags,
        )
        self.assertEqual(_ids(cols), ["host", "name", "cpu"])


class AutoInsertTests(_LayoutTestBase):
    def test_multi_host_prepends_host_when_missing(self) -> None:
        flags = LayoutFlags(multi_host=True, cross_user=False)
        cols = build_active_columns(
            cfg_columns=("name", "cpu"),
            flags=flags,
        )
        self.assertEqual(_ids(cols), ["host", "name", "cpu"])

    def test_multi_host_does_not_duplicate_user_provided_host(self) -> None:
        flags = LayoutFlags(multi_host=True, cross_user=False)
        cols = build_active_columns(
            cfg_columns=("name", "host", "cpu"),
            flags=flags,
        )
        # User listed it; respect the position they chose.
        self.assertEqual(_ids(cols), ["name", "host", "cpu"])

    def test_cross_user_inserts_user_after_name(self) -> None:
        flags = LayoutFlags(multi_host=False, cross_user=True)
        cols = build_active_columns(
            cfg_columns=("name", "cpu", "cmd"),
            flags=flags,
        )
        self.assertEqual(_ids(cols), ["name", "user", "cpu", "cmd"])

    def test_cross_user_inserts_user_after_host_then_name(self) -> None:
        flags = LayoutFlags(multi_host=True, cross_user=True)
        cols = build_active_columns(
            cfg_columns=("name", "cpu"),
            flags=flags,
        )
        # host auto-prepended → [host, name, cpu]; then user inserted
        # after the last of host/name → [host, name, user, cpu].
        self.assertEqual(_ids(cols), ["host", "name", "user", "cpu"])

    def test_cross_user_does_not_duplicate_user_provided_user(self) -> None:
        flags = LayoutFlags(multi_host=False, cross_user=True)
        cols = build_active_columns(
            cfg_columns=("user", "name", "cpu"),
            flags=flags,
        )
        self.assertEqual(_ids(cols), ["user", "name", "cpu"])

    def test_cross_user_auto_insert_when_host_explicit(self) -> None:
        # When the user lists ``host`` explicitly *and* cross_user is
        # set, ``user`` lands after the *last* of host/name in the
        # resulting list (the loop walks selected and updates
        # ``insert_at`` on every match). With cfg=(name, host, cpu)
        # and host in cfg, the loop iterates name→insert_at=1,
        # host→insert_at=2, cpu→skipped, so user lands at index 2.
        cfg = ("name", "host", "cpu")
        flags = LayoutFlags(multi_host=True, cross_user=True)
        out = build_active_columns(cfg_columns=cfg, flags=flags)
        self.assertEqual(_ids(out), ["name", "host", "user", "cpu"])

    def test_cross_user_with_no_name_or_host_inserts_at_front(self) -> None:
        flags = LayoutFlags(multi_host=False, cross_user=True)
        cols = build_active_columns(
            cfg_columns=("cpu", "cmd"),
            flags=flags,
        )
        # No name or host in selected → user inserts at front.
        self.assertEqual(_ids(cols), ["user", "cpu", "cmd"])


class RegistryConsistencyTests(_LayoutTestBase):
    def test_default_path_uses_registry_order(self) -> None:
        flags = LayoutFlags(multi_host=True, cross_user=True)
        cols = build_active_columns(cfg_columns=None, flags=flags)
        # The defaults path returns registry order; verify by scanning
        # against the canonical ordering.
        registry_ids = [c.id for c in REGISTRY]
        self.assertEqual(
            _ids(cols),
            [i for i in registry_ids if i in set(_ids(cols))],
        )

    def test_empty_cfg_returns_empty_when_no_flags(self) -> None:
        # An empty tuple is "user explicitly chose nothing" — distinct
        # from None ("use defaults"). Auto-inserts still apply when a
        # flag is set.
        flags = LayoutFlags(multi_host=False, cross_user=False)
        cols = build_active_columns(cfg_columns=(), flags=flags)
        self.assertEqual(cols, ())


if __name__ == "__main__":
    unittest.main()

"""Unit tests for lib/ccw_tui_mouse.py — SGR-1006 parser and hit-test."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "lib"))

from ccw_tui_mouse import parse_mouse_sgr, MouseEvent, HitRegion, hit_test


class ParseSgrTests(unittest.TestCase):
    def test_left_click_press(self) -> None:
        ev = parse_mouse_sgr("\x1b[<0;15;7M")
        self.assertEqual(ev, MouseEvent(button=0, x=15, y=7, pressed=True, wheel=0))

    def test_left_click_release(self) -> None:
        ev = parse_mouse_sgr("\x1b[<0;15;7m")
        self.assertIsNotNone(ev)
        assert ev is not None
        self.assertFalse(ev.pressed)

    def test_wheel_up(self) -> None:
        ev = parse_mouse_sgr("\x1b[<64;1;1M")
        self.assertIsNotNone(ev)
        assert ev is not None
        self.assertEqual(ev.wheel, -1)

    def test_wheel_down(self) -> None:
        ev = parse_mouse_sgr("\x1b[<65;1;1M")
        self.assertIsNotNone(ev)
        assert ev is not None
        self.assertEqual(ev.wheel, 1)

    def test_rejects_non_mouse(self) -> None:
        self.assertIsNone(parse_mouse_sgr("\x1b[A"))
        self.assertIsNone(parse_mouse_sgr("a"))
        self.assertIsNone(parse_mouse_sgr("\x1b[<abc"))


class HitTestTests(unittest.TestCase):
    def test_finds_region_by_y(self) -> None:
        regions = [
            HitRegion(y=3, action="row", payload=0),
            HitRegion(y=4, action="row", payload=1),
            HitRegion(y=5, action="row", payload=2),
        ]
        result = hit_test(regions, y=4)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.payload, 1)

    def test_misses_outside(self) -> None:
        regions = [HitRegion(y=3, action="row", payload=0)]
        self.assertIsNone(hit_test(regions, y=99))


if __name__ == "__main__":
    unittest.main()

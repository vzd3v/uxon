# SPDX-License-Identifier: MIT
"""Tests for uxon.duration.parse_duration_seconds."""

from __future__ import annotations

import unittest

from uxon.duration import parse_duration_seconds


class ParseDurationSecondsTests(unittest.TestCase):
    def test_seconds_suffix(self) -> None:
        self.assertEqual(parse_duration_seconds("10s"), 10.0)
        self.assertEqual(parse_duration_seconds("0s"), 0.0)
        self.assertEqual(parse_duration_seconds("1.5s"), 1.5)

    def test_milliseconds_suffix(self) -> None:
        self.assertEqual(parse_duration_seconds("500ms"), 0.5)
        self.assertEqual(parse_duration_seconds("0ms"), 0.0)
        self.assertAlmostEqual(parse_duration_seconds("250ms"), 0.25)

    def test_minutes_suffix(self) -> None:
        self.assertEqual(parse_duration_seconds("2m"), 120.0)
        self.assertEqual(parse_duration_seconds("0.5m"), 30.0)

    def test_bare_int_pass_through_as_seconds(self) -> None:
        self.assertEqual(parse_duration_seconds(10), 10.0)
        self.assertEqual(parse_duration_seconds(0), 0.0)

    def test_bare_float_pass_through(self) -> None:
        self.assertEqual(parse_duration_seconds(1.5), 1.5)

    def test_bare_numeric_string_accepted_as_seconds(self) -> None:
        self.assertEqual(parse_duration_seconds("10"), 10.0)
        self.assertEqual(parse_duration_seconds("1.5"), 1.5)

    def test_case_insensitive(self) -> None:
        self.assertEqual(parse_duration_seconds("10S"), 10.0)
        self.assertEqual(parse_duration_seconds("500MS"), 0.5)
        self.assertEqual(parse_duration_seconds("2M"), 120.0)

    def test_whitespace_tolerated(self) -> None:
        self.assertEqual(parse_duration_seconds("  10s  "), 10.0)

    def test_empty_string_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_duration_seconds("")
        with self.assertRaises(ValueError):
            parse_duration_seconds("   ")

    def test_unknown_suffix_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_duration_seconds("10x")
        with self.assertRaises(ValueError):
            parse_duration_seconds("1h")  # h deliberately rejected
        with self.assertRaises(ValueError):
            parse_duration_seconds("1d")

    def test_suffix_without_number_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_duration_seconds("ms")
        with self.assertRaises(ValueError):
            parse_duration_seconds("s")

    def test_non_numeric_garbage_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_duration_seconds("abc")

    def test_negative_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_duration_seconds("-5s")
        with self.assertRaises(ValueError):
            parse_duration_seconds(-1.0)

    def test_bool_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_duration_seconds(True)  # type: ignore[arg-type]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""Duration grammar, including finite values at every conversion boundary."""

import unittest

from uxon.infra.duration import parse_duration_seconds


class ParseDurationSecondsTests(unittest.TestCase):
    def test_supported_forms_and_zero(self) -> None:
        for value, expected in (
            (5, 5.0),
            (1.5, 1.5),
            ("2.5", 2.5),
            ("5s", 5.0),
            ("500ms", 0.5),
            ("2m", 120.0),
            (" 1.5 M ", 90.0),
            ("250MS", 0.25),
            (0, 0.0),
            ("0s", 0.0),
        ):
            with self.subTest(value=value):
                self.assertEqual(parse_duration_seconds(value), expected)

    def test_invalid_forms_and_nonfinite_results(self) -> None:
        for value in (
            "",
            " ",
            "1h",
            "1day",
            "ms",
            "s",
            "m",
            "abc",
            "1xs",
            "1ss",
            -1,
            "-0.5ms",
            "-2m",
            True,
            False,
            None,
            [],
            {},
            float("nan"),
            float("inf"),
            float("-inf"),
            10**400,
            "nan",
            "inf",
            "-inf",
            "NaNS",
            "infms",
            "1e309s",
            "1e308m",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_duration_seconds(value)

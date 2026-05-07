# SPDX-License-Identifier: MIT
"""Duration parser for config values.

Accepts a small closed set of suffixes (case-insensitive): ``ms``, ``s``,
``m``. Bare ints/floats pass through as seconds. No ``h``/``d`` —
operator-facing per-host intervals are seconds-to-minutes; longer values
are a smell that should surface as a typo, not a silent acceptance.
"""

from __future__ import annotations

__all__ = ["parse_duration_seconds"]


def parse_duration_seconds(value: str | int | float) -> float:
    """Parse a duration to fractional seconds.

    >>> parse_duration_seconds("10s")
    10.0
    >>> parse_duration_seconds("500ms")
    0.5
    >>> parse_duration_seconds("2m")
    120.0
    >>> parse_duration_seconds(1.5)
    1.5

    Raises ValueError for unrecognised suffixes, empty strings, or
    non-numeric inputs. Negative durations are also rejected.
    """
    if isinstance(value, bool):
        raise ValueError(f"duration must be number or string, got bool: {value!r}")
    if isinstance(value, int | float):
        seconds = float(value)
        if seconds < 0:
            raise ValueError(f"duration must be non-negative, got {value!r}")
        return seconds
    if not isinstance(value, str):
        raise ValueError(f"duration must be number or string, got {type(value).__name__}")
    text = value.strip().lower()
    if not text:
        raise ValueError("duration string is empty")

    suffix_map: list[tuple[str, float]] = [
        ("ms", 0.001),
        ("s", 1.0),
        ("m", 60.0),
    ]
    for suffix, multiplier in suffix_map:
        if text.endswith(suffix):
            head = text[: -len(suffix)].strip()
            if not head:
                raise ValueError(f"duration {value!r} has suffix but no number")
            try:
                number = float(head)
            except ValueError as exc:
                raise ValueError(f"duration {value!r} is not a valid number") from exc
            if number < 0:
                raise ValueError(f"duration must be non-negative, got {value!r}")
            return number * multiplier

    # No recognised suffix — accept bare numeric strings as seconds.
    try:
        seconds = float(text)
    except ValueError as exc:
        raise ValueError(f"duration {value!r} is not a recognised form") from exc
    if seconds < 0:
        raise ValueError(f"duration must be non-negative, got {value!r}")
    return seconds

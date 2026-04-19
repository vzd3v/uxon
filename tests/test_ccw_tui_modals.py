"""Unit tests for lib/ccw_tui_modals.py."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "lib"))
sys.path.insert(0, str(_REPO / "tests"))

from ccw_tui_modals import run_modal, MenuModal, ModalResult
from test_ccw_tui import _FakeTerm


class _ScriptedKey:
    """Minimal blessed.Keystroke stand-in used by _ScriptingTerm below."""

    def __init__(self, s: str) -> None:
        self.s = s
        self.name = {
            "\r": "KEY_ENTER", "\n": "KEY_ENTER",
            "\x1b": "KEY_ESCAPE", "\x7f": "KEY_BACKSPACE",
        }.get(s, None)
        self.is_sequence = self.name is not None

    def __eq__(self, other) -> bool:
        if isinstance(other, _ScriptedKey):
            return self.s == other.s
        return self.s == other

    def __hash__(self) -> int:
        return hash(self.s)

    def __str__(self) -> str:
        return self.s

    def __bool__(self) -> bool:
        return bool(self.s)


class _ScriptingTerm(_FakeTerm):
    """_FakeTerm extension that scripts inkey() returns."""

    def __init__(self) -> None:
        self.scripted_keys: list = []

    def inkey(self, timeout=None):
        if not self.scripted_keys:
            raise RuntimeError("_ScriptingTerm ran out of scripted keys")
        raw = self.scripted_keys.pop(0)
        if isinstance(raw, _ScriptedKey):
            return raw
        return _ScriptedKey(raw)


class _ScriptedModal:
    """Modal that returns a preset sequence of results ignoring input."""

    def __init__(self, script) -> None:
        self._script = list(script)
        self.render_count = 0

    def render(self, t) -> None:
        self.render_count += 1

    def handle(self, key):
        return self._script.pop(0) if self._script else None


class RunModalTests(unittest.TestCase):
    def test_returns_first_non_none(self) -> None:
        t = _ScriptingTerm()
        m = _ScriptedModal([None, None, ModalResult("ok", 42)])
        t.scripted_keys = ["a", "b", "c"]
        result = run_modal(t, m)
        self.assertEqual(result.name, "ok")
        self.assertEqual(result.value, 42)
        self.assertEqual(m.render_count, 3)


class MenuModalTests(unittest.TestCase):
    def test_enter_on_first_row_returns_that_row(self) -> None:
        t = _ScriptingTerm()
        t.scripted_keys = ["\r"]
        menu = MenuModal(title="pick", rows=[("a", "A"), ("b", "B")])
        result = run_modal(t, menu)
        self.assertEqual(result.name, "selected")
        self.assertEqual(result.value, "a")

    def test_escape_cancels(self) -> None:
        t = _ScriptingTerm()
        t.scripted_keys = ["\x1b"]
        menu = MenuModal(title="pick", rows=[("a", "A")])
        result = run_modal(t, menu)
        self.assertEqual(result.name, "cancel")


if __name__ == "__main__":
    unittest.main()

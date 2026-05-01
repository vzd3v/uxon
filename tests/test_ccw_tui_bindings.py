"""Drift-guard tests for ccw_tui screen BINDINGS.

Two assertions (per plan T18):
  (a) Every destructive binding (action_kill*) carries ``show=True``
      and a non-empty description.
  (b) No Screen subclass in ``lib/ccw_tui/screens/`` overrides ``on_key``.
      All keystroke handling must go through ``BINDINGS``. Catches the
      ``g`` footgun from PR 10 where a dev added ``if event.key == "g":
      ...`` in ``on_key``, bypassing the declarative registry.
"""

from __future__ import annotations

import ast
import inspect
import os
import pkgutil
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB = os.path.abspath(os.path.join(_HERE, "..", "lib"))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)


def _textual_available() -> bool:
    try:
        import textual  # noqa: F401
    except ImportError:
        return False
    return True


def _iter_screen_classes():
    """Yield (module, cls) for every Screen/ModalScreen subclass under ``lib/ccw_tui/screens``."""
    import ccw_tui.screens as screens_pkg
    from textual.screen import ModalScreen, Screen

    for modinfo in pkgutil.walk_packages(screens_pkg.__path__, prefix="ccw_tui.screens."):
        mod = __import__(modinfo.name, fromlist=["*"])
        for name, obj in inspect.getmembers(mod, inspect.isclass):
            if obj.__module__ != modinfo.name:
                continue
            if issubclass(obj, (Screen, ModalScreen)) and obj not in (Screen, ModalScreen):
                yield mod, obj


@unittest.skipUnless(_textual_available(), "textual not installed")
class DestructiveBindingVisibilityTests(unittest.TestCase):
    """Every destructive binding must be visible in the footer."""

    def test_destructive_bindings_are_shown(self) -> None:
        offenders: list[str] = []
        for mod, cls in _iter_screen_classes():
            bindings = getattr(cls, "BINDINGS", [])
            for b in bindings:
                action_name = _action_name(b)
                if not action_name.startswith("kill"):
                    continue
                show = getattr(b, "show", True)
                description = getattr(b, "description", "") or ""
                if not show or not description.strip():
                    offenders.append(
                        f"{cls.__module__}.{cls.__name__}: "
                        f"binding {b!r} has action_kill* but show={show} desc={description!r}"
                    )
        self.assertFalse(
            offenders,
            "destructive bindings MUST have show=True and a non-empty description; "
            + "\n".join(offenders),
        )


@unittest.skipUnless(_textual_available(), "textual not installed")
class NoOnKeyOverrideTests(unittest.TestCase):
    """No Screen subclass may override ``on_key``.

    All keystroke handling must go through ``BINDINGS``. AST-walks every
    screen file to spot ``def on_key`` at class scope.
    """

    def test_no_on_key_override_in_screens(self) -> None:
        screens_dir = os.path.join(_LIB, "ccw_tui", "screens")
        offenders: list[str] = []
        for fname in os.listdir(screens_dir):
            if not fname.endswith(".py") or fname == "__init__.py":
                continue
            path = os.path.join(screens_dir, fname)
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    for item in node.body:
                        if (
                            isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and item.name == "on_key"
                        ):
                            offenders.append(
                                f"{fname}::{node.name}.{item.name} at line {item.lineno}"
                            )
        self.assertFalse(
            offenders,
            f"Screen classes must not override on_key — use BINDINGS instead; found: {offenders}",
        )


@unittest.skipUnless(_textual_available(), "textual not installed")
class SettingsGKeyRetiredTests(unittest.TestCase):
    """``g`` must not be bound on SettingsScreen (PR 10 footgun)."""

    def test_g_key_not_bound(self) -> None:
        from ccw_tui.screens.settings import SettingsScreen

        keys = {_binding_key(b) for b in SettingsScreen.BINDINGS}
        self.assertNotIn("g", keys, "g must not be a SettingsScreen binding")


def _action_name(binding) -> str:
    """Extract the action name from a Binding, stripping arguments."""
    action = getattr(binding, "action", "")
    # Strip parens: ``kill_all_global()`` → ``kill_all_global``.
    name = action.split("(")[0]
    return name


def _binding_key(binding) -> str:
    return getattr(binding, "key", "")


if __name__ == "__main__":
    unittest.main()

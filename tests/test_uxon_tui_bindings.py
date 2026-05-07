"""Drift-guard tests for uxon TUI screen BINDINGS.

Two assertions (per plan T18):
  (a) Every destructive binding (action_kill*) carries ``show=True``
      and a non-empty description.
  (b) No Screen subclass in ``src/uxon/tui/screens/`` overrides ``on_key``.
      All keystroke handling must go through ``BINDINGS``. Catches the
      ``g`` footgun from PR 10 where a dev added ``if event.key == "g":
      ...`` in ``on_key``, bypassing the declarative registry.
"""

from __future__ import annotations

import ast
import inspect
import os
import pkgutil
import unittest


def _textual_available() -> bool:
    try:
        import textual  # noqa: F401
    except ImportError:
        return False
    return True


def _iter_screen_classes():
    """Yield (module, cls) for every Screen/ModalScreen subclass under ``src/uxon/tui/screens``."""
    from textual.screen import ModalScreen, Screen

    import uxon.tui.screens as screens_pkg

    for modinfo in pkgutil.walk_packages(screens_pkg.__path__, prefix="uxon.tui.screens."):
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
        import uxon.tui.screens as screens_pkg

        screens_dir = os.path.dirname(screens_pkg.__file__)
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
        from uxon.tui.screens.settings import SettingsScreen

        keys = {_binding_key(b) for b in SettingsScreen.BINDINGS}
        self.assertNotIn("g", keys, "g must not be a SettingsScreen binding")


@unittest.skipUnless(_textual_available(), "textual not installed")
class MainScreenSortBindingsRetiredTests(unittest.TestCase):
    """``s`` / ``S`` (sort cycle / sort dir) are gone in 3.4.

    Sort is now a hard contract owned by the model selector — there's
    no UI-state knob to flip. ``q`` and ``r`` / ``d`` / ``D`` remain.
    """

    def test_s_and_S_not_bound(self) -> None:
        from uxon.tui.screens.main import MainScreen

        keys = {_binding_key(b) for b in MainScreen.BINDINGS}
        self.assertNotIn("s", keys)
        self.assertNotIn("S", keys)

    def test_core_keys_remain(self) -> None:
        from uxon.tui.screens.main import MainScreen

        keys = {_binding_key(b) for b in MainScreen.BINDINGS}
        for k in ("q", "r", "d", "D"):
            self.assertIn(k, keys, f"{k} must still be bound on MainScreen")


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

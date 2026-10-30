"""Drift-guard tests for uxon TUI screen BINDINGS.

Two architectural contracts:
  (a) Every destructive binding (action_kill*) carries ``show=True``
      and a non-empty description.
  (b) No Screen subclass in ``src/uxon/tui/screens/`` — and no custom
      widget in ``src/uxon/tui/widgets/`` — overrides ``on_key``. All
      keystroke handling must go through ``BINDINGS``. Catches the ``g`` footgun from PR 10 where a dev added
      ``if event.key == "g": ...`` in ``on_key``, bypassing the
      declarative registry, and holds the render-on-demand widget
      (``SessionListView``) + the ``GatedFooter`` to the same rule.
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
    """Every destructive binding must be visible in the footer.

    JCUKEN twins (added by :func:`uxon.tui.keymap.bindings_with_aliases`)
    legitimately ride along with ``show=False`` — the primary QWERTY
    key carries the visible footer entry and the twin shares its
    action 1:1. We exempt only those exact twin keys.
    """

    def test_destructive_bindings_are_shown(self) -> None:
        from uxon.tui.keymap import LAYOUT_ALIASES

        twin_keys = set(LAYOUT_ALIASES.values())
        offenders: list[str] = []
        for mod, cls in _iter_screen_classes():
            bindings = getattr(cls, "BINDINGS", [])
            for b in bindings:
                action_name = _action_name(b)
                if not action_name.startswith("kill"):
                    continue
                if _binding_key(b) in twin_keys:
                    # Hidden RU twin of a primary visible binding.
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
    """No Screen subclass — and no custom widget — may override ``on_key``.

    All keystroke handling must go through ``BINDINGS``. AST-walks every
    screen and widget file to spot ``def on_key`` at class scope.
    """

    @staticmethod
    def _on_key_offenders(directory: str) -> list[str]:
        offenders: list[str] = []
        for fname in os.listdir(directory):
            if not fname.endswith(".py") or fname == "__init__.py":
                continue
            path = os.path.join(directory, fname)
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
        return offenders

    def test_no_on_key_override_in_screens(self) -> None:
        import uxon.tui.screens as screens_pkg

        offenders = self._on_key_offenders(os.path.dirname(screens_pkg.__file__))
        self.assertFalse(
            offenders,
            f"Screen classes must not override on_key — use BINDINGS instead; found: {offenders}",
        )

    def test_no_on_key_override_in_widgets(self) -> None:
        import uxon.tui.widgets as widgets_pkg

        offenders = self._on_key_offenders(os.path.dirname(widgets_pkg.__file__))
        self.assertFalse(
            offenders,
            f"Custom widgets must not override on_key — use BINDINGS instead; found: {offenders}",
        )


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

"""GatedFooter — stock Textual ``Footer`` with a recompose gate.

The stock ``Footer`` fully **recomposes** (unmount + re-mount every key widget)
on *every* focus change, because ``Screen`` publishes ``bindings_updated_signal``
on every ``refresh_bindings`` and the footer's subscriber
(:meth:`Footer.bindings_changed`) unconditionally schedules ``recompose``.
uxon's bindings are screen-level and invariant across MainScreen's focusable
children, so that recompose is pure waste — and it tears down focusable nodes
while a key is in flight.

This subclass overrides :meth:`bindings_changed` to keep the base behaviour
**verbatim** — set ``_bindings_ready = True``, honour the ``app_focus`` /
``is_attached`` / ``screen is self.screen`` guards — but gate **only** the
final ``call_after_refresh(self.recompose)`` on a hash of the active binding
set. First paint always composes (the stored hash starts at ``None``, so the
first publish is a guaranteed mismatch → composes); same-hash focus changes
skip the recompose; ``_bindings_ready`` is **never** gated, so the blank-footer
trap cannot occur.

Why a ``Footer`` subclass and not a ``Screen.refresh_bindings`` override: the
screen cannot know *when* the footer subscribed to the signal, and
``Signal.publish`` has no replay for late subscribers — a screen-side gate that
records the (invariant) hash on the early mount-time publish would then suppress
every later publish, ``bindings_changed`` would never run, and the footer would
stay blank. Gating inside ``bindings_changed`` (after ``_bindings_ready`` is
set) is the only safe seam.

Hash domain
-----------

``Footer.compose`` renders, per binding: ``key``, ``key_display`` (via
``app.get_key_display(binding)``), ``description``, the ``enabled`` flag, the
``tooltip``, ``action`` (groups by it), and the ``show`` filter (and ``group``).
To stay drift-proof we hash the **full per-binding tuple**
``(key, key_display, description, enabled, tooltip, action, show)`` over
``screen.active_bindings``. In practice only the
``enabled`` flag varies across MainScreen's focusable children, so any visible
change still recomposes while pure focus moves do not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.widgets import Footer

if TYPE_CHECKING:
    from textual.screen import Screen

# Import-time guard: this subclass overrides ``bindings_changed`` and relies on
# the private ``_bindings_ready`` reactive existing on the stock ``Footer`` (we
# must keep setting it so the footer's first paint is never suppressed). If a
# future Textual refactor drops or renames it, fail loudly at import with a
# pointer to the version pin — mirrors the deleted ``_row_locations`` guard in
# the old ``session_dashboard_table.py``.
assert hasattr(Footer, "_bindings_ready"), (
    "Textual API changed: Footer._bindings_ready no longer exists. GatedFooter "
    "relies on it to avoid the blank-footer first-paint trap. Pin the "
    "Textual version in pyproject.toml and re-derive the gate before bumping."
)


class GatedFooter(Footer):
    """``Footer`` that recomposes only when the active binding set changes."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        # ``None`` until the first publish — guarantees first paint composes.
        self._bindings_hash: int | None = None

    def _active_bindings_hash(self, screen: Screen) -> int:
        """Hash every field ``Footer.compose`` renders, per binding.

        See the module docstring for the field domain. Built off
        ``screen.active_bindings`` (the same source ``compose`` reads).
        """
        app = screen.app
        fingerprint: list[tuple[str, str, str, bool, str, str, bool]] = []
        for _node, binding, enabled, tooltip in screen.active_bindings.values():
            fingerprint.append(
                (
                    binding.key,
                    app.get_key_display(binding),
                    binding.description,
                    enabled,
                    tooltip,
                    binding.action,
                    binding.show,
                )
            )
        return hash(tuple(fingerprint))

    def bindings_changed(self, screen: Screen) -> None:
        # Base behaviour, verbatim — ``_bindings_ready`` is NEVER gated so
        # the footer's first compose is never suppressed (blank-footer trap).
        self._bindings_ready = True
        if not screen.app.app_focus:
            return
        if not (self.is_attached and screen is self.screen):
            return
        # Gate ONLY the recompose on the active-binding-set hash. First
        # publish: stored hash is ``None`` → always a mismatch → composes.
        new_hash = self._active_bindings_hash(screen)
        if new_hash == self._bindings_hash:
            return
        self._bindings_hash = new_hash
        self.call_after_refresh(self.recompose)

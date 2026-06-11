"""JCUKEN ↔ QWERTY layout aliases for ``BINDINGS``.

Every binding declared via :func:`bindings_with_aliases` ships with
a hidden RU twin when its physical key has an entry in
:data:`LAYOUT_ALIASES`. Unknown keys pass through untouched — no
warnings, no errors. The map grows when a new alias is needed.

Forward-compat note: when bindings move to TOML in a future pass,
the same helper still applies — only the source of binding tuples
changes.
"""

from __future__ import annotations

from textual.binding import Binding

LAYOUT_ALIASES: dict[str, str] = {
    # JCUKEN ↔ QWERTY shared positions.
    "d": "в",
    "D": "В",
    "r": "к",
    "R": "К",
    "v": "м",
    "V": "М",
    "q": "й",
    "Q": "Й",
    "a": "ф",
    "A": "Ф",
    "x": "ч",
    "X": "Ч",
    "s": "ы",
    "S": "Ы",
    "h": "р",
    "H": "Р",
    "/": ".",
}


def bindings_with_aliases(*specs: Binding) -> list[Binding]:
    """Return each spec, plus a hidden RU twin where one exists in
    :data:`LAYOUT_ALIASES`.

    Specs whose key is not in the map pass through untouched. No
    errors, no whitelist — the map is the contract.
    """
    out: list[Binding] = []
    for spec in specs:
        out.append(spec)
        twin_key = LAYOUT_ALIASES.get(spec.key)
        if twin_key is None:
            continue
        out.append(
            Binding(
                twin_key,
                spec.action,
                spec.description,
                show=False,
                priority=spec.priority,
            )
        )
    return out

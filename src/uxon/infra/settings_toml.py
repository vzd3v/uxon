# SPDX-License-Identifier: MIT
"""TOML serialization for the repo-level ``config/config.toml``.

Split out of :mod:`uxon.infra.settings` so the schema + resolution +
persistence concern stays separate from the render/round-trip/escape
concern. A true serialization leaf: imports nothing from ``uxon`` — the
caller injects the schema constants (``schema_keys`` / ``table_keys``).
Two render paths:

* :func:`render_repo_config_toml` — minimal from-scratch emit (no
  comments), used only when no file exists yet. Pure stdlib.
* :func:`update_repo_config_text` — comment-preserving round-trip that
  mutates only the changed keys, lazily importing ``tomlkit`` (the
  AGENTS.md "config writes require tomlkit" rule); CLI read paths stay
  on stdlib ``tomllib``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

# ── Dotted-key helper for nested TOML tables ─────────────────────────


def set_dotted(doc: Any, dotted_key: str, value: Any) -> None:
    """Walk/create nested tomlkit tables and set the leaf value."""
    import tomlkit

    parts = dotted_key.split(".")
    node = doc
    for part in parts[:-1]:
        if part not in node:
            node[part] = tomlkit.table()
        node = node[part]
    node[parts[-1]] = value


# ── TOML rendering (minimal, for fresh files only) ───────────────────


def _escape_string(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _escape_key(k: str) -> str:
    if k and all(c.isalnum() or c in "_-" for c in k):
        return k
    return _escape_string(k)


def _format_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(v)
    if isinstance(v, str):
        return _escape_string(v)
    if isinstance(v, list):
        parts = []
        for x in v:
            if isinstance(x, str):
                parts.append(_escape_string(x))
            else:
                parts.append(_format_value(x))
        return "[" + ", ".join(parts) + "]"
    raise ValueError(f"unsupported TOML value type: {type(v).__name__}")


def render_repo_config_toml(
    repo_data: dict,
    *,
    schema_keys: Sequence[str],
    table_keys: Sequence[str],
) -> str:
    """Render a minimal repo-level config.toml body from scratch.

    Used only when there is no existing file to update (e.g. fresh
    install). No comments are emitted — an installer that wants a
    commented starter should ship a hand-written template.
    Keys are emitted in ``schema_keys`` order for stability. The
    ``launch_user_by_caller`` table is always emitted (even when empty)
    so operators see it when opening the file directly.
    """
    lines: list[str] = []

    for key in schema_keys:
        if key in table_keys:
            continue
        if key in repo_data:
            lines.append(f"{key} = {_format_value(repo_data[key])}")

    for table_key in table_keys:
        lines.append("")
        lines.append(f"[{table_key}]")
        table = repo_data.get(table_key) or {}
        if isinstance(table, dict):
            for sub_key in sorted(table):
                sub_val = table[sub_key]
                if not isinstance(sub_val, str):
                    raise ValueError(
                        f"{table_key}.{sub_key}: expected string value, got {type(sub_val).__name__}"
                    )
                lines.append(f"{_escape_key(sub_key)} = {_escape_string(sub_val)}")

    return "\n".join(lines) + "\n"


# ── Round-trip update (comment-preserving) ───────────────────────────


def update_repo_config_text(
    existing_text: str,
    updates: dict,
    *,
    schema_keys: Sequence[str],
    table_keys: Sequence[str],
) -> str:
    """Apply ``updates`` to ``existing_text`` (a config.toml body) and
    return the new text with comments and formatting of untouched parts
    preserved byte-identical.

    ``updates`` maps schema keys to their new values. Table-kind keys
    (those in ``table_keys``) are replaced wholesale: the table body is
    rewritten but the table header line and any comments above it stay
    intact.

    Raises ``KeyError`` for unknown keys and ``ValueError`` for type
    mismatches (mirrors :func:`apply_setting`/:func:`replace_mapping`).
    """
    import tomlkit  # lazy: only the writer path pulls tomlkit in

    doc = tomlkit.parse(existing_text)
    for key, value in updates.items():
        if key not in schema_keys:
            raise KeyError(f"unknown setting key: {key}")
        if key in table_keys:
            if not isinstance(value, dict):
                raise ValueError(f"{key} must be a mapping")
            tbl = tomlkit.table()
            for sub_k in sorted(value):
                sub_v = value[sub_k]
                if not isinstance(sub_k, str) or not isinstance(sub_v, str):
                    raise ValueError(f"table {key} requires string keys and values")
                tbl[sub_k] = sub_v
            doc[key] = tbl
        elif "." in key:
            set_dotted(doc, key, value)
        else:
            doc[key] = value
    return tomlkit.dumps(doc)

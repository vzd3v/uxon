"""Layout selector: pick the active column set for a runtime context.

The dashboard widget mounts with a *fixed* column tuple (Textual's
``DataTable`` does not support post-mount column changes cleanly); on
runtime layout flips (e.g. first other-user session appears) the
screen recomposes with a new widget. This module computes that
column tuple from three inputs:

* the optional user config (``cfg_columns``: ordered tuple of ids),
* runtime ``LayoutFlags`` (multi-host, cross-user),
* the registry as the source of truth for defaults / show_when gates.

Unknown ids in user config are dropped silently with a single
debug-log entry per id (memoised) so removing a column from the
registry between versions does not break existing TOMLs.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..events import debug
from .columns import REGISTRY, ColumnSpec


@dataclass(frozen=True)
class LayoutFlags:
    """Runtime gate inputs for column visibility.

    ``multi_host`` is config-driven: ``True`` iff ``cfg.remote_hosts``
    is non-empty. Stays set even when remote slots are empty so the
    HOST column stays mounted whenever the operator is "in multi-host
    mode" — its job is to disambiguate the cell, not to indicate that
    data is currently present.

    ``cross_user`` is data-driven but *latched*: ``True`` once two
    distinct usernames have been observed in this process across any
    of the model's three sources (local own, local other-user, remote
    peers). The accumulator behind it lives on
    :attr:`uxon.tui.dashboard.ui_state.MainScreenUiState.seen_users`
    and never shrinks for the App's lifetime, so the USER column does
    not flicker when a foreign user's sessions die, a remote peer
    drops a tick, or the operator narrows the table with a filter.
    See :mod:`uxon.tui.dashboard.seen_users` for the contract.
    """

    multi_host: bool
    cross_user: bool


# Memoised set of unknown column ids the operator has been warned
# about. Avoids spamming the debug log when an old TOML lists a
# removed column id and the screen recomposes 60 times a tick.
_WARNED_UNKNOWN_IDS: set[str] = set()


def _matches_flag(col: ColumnSpec, flags: LayoutFlags) -> bool:
    """True iff the column's show_when gate is open.

    "always"-gated columns ARE always reachable, but the defaults path
    still requires ``default_visible=True`` to surface them — opt-in
    columns like ``pid`` / ``wins`` stay hidden until requested via
    TOML. The explicit-cfg path uses this helper directly to decide
    whether a user-listed id can render under the current flags.
    """
    if col.show_when == "always":
        return True
    if col.show_when == "multi_host":
        return flags.multi_host
    if col.show_when == "cross_user":
        return flags.cross_user
    return False


def _is_default_visible(col: ColumnSpec, flags: LayoutFlags) -> bool:
    """Defaults-path predicate: keep visible columns + flag-met gates.

    A column shows on the default path iff:

    * it is ``default_visible=True`` AND its gate ("always") is open,
      OR
    * its ``show_when`` is a runtime flag that is currently set (the
      gate "promotes" it into view even when not default_visible).

    Opt-in columns (``default_visible=False``, ``show_when="always"``)
    stay hidden unless the user lists them in TOML.
    """
    if col.show_when == "always":
        return col.default_visible
    return _matches_flag(col, flags)


def _warn_unknown_once(col_id: str) -> None:
    if col_id in _WARNED_UNKNOWN_IDS:
        return
    _WARNED_UNKNOWN_IDS.add(col_id)
    debug("tui", reason="unknown_column_id", id=col_id)


def _reset_warned() -> None:
    """Test hook: clear the once-per-process warned-id memo.

    Production callers never invoke this. Tests that exercise
    ``_warn_unknown_once`` must reset between runs so memo state from
    one test does not silently mask a missing warn in the next.
    """
    _WARNED_UNKNOWN_IDS.clear()


def build_active_columns(
    *,
    cfg_columns: tuple[str, ...] | None,
    flags: LayoutFlags,
    registry: tuple[ColumnSpec, ...] = REGISTRY,
) -> tuple[ColumnSpec, ...]:
    """Return the column tuple the widget should mount with.

    ``cfg_columns is None`` → take registry defaults (every column
    that is ``default_visible`` plus any whose ``show_when`` matches
    the current flags).

    ``cfg_columns`` provided → take user order, drop unknown ids
    (logged via ``UXON_DEBUG=tui`` once per id) and drop ids whose
    ``show_when`` is unmet. Auto-prepend ``host`` when ``multi_host``
    is set and the user did not list it; auto-insert ``user`` after
    the leading ``host``/``name`` block when ``cross_user`` is set
    and the user did not list it.
    """
    by_id = {c.id: c for c in registry}

    if cfg_columns is None:
        return tuple(c for c in registry if _is_default_visible(c, flags))

    selected: list[ColumnSpec] = []
    for col_id in cfg_columns:
        col = by_id.get(col_id)
        if col is None:
            _warn_unknown_once(col_id)
            continue
        if not _matches_flag(col, flags):
            # Silent: requested but the runtime gate is closed (e.g.
            # ``host`` requested in single-host mode). Operators see
            # the column as soon as the gate opens.
            continue
        selected.append(col)

    requested_ids = set(cfg_columns)

    if flags.multi_host and "host" not in requested_ids:
        host_col = by_id.get("host")
        if host_col is not None:
            selected.insert(0, host_col)

    if flags.cross_user and "user" not in requested_ids:
        user_col = by_id.get("user")
        if user_col is not None:
            insert_at = 0
            for i, c in enumerate(selected):
                if c.id in ("host", "name"):
                    insert_at = i + 1
            selected.insert(insert_at, user_col)

    return tuple(selected)

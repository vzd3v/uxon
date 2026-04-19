"""Settings sub-screens for the ccw TUI.

Entry point: :func:`show_settings` — blocks until the operator dismisses
the screen. Edits are pushed through a tiny ``SettingsCallbacks`` object
provided by the main TUI, which hides the file I/O in ``ccw_settings``.

As of PR 10 (2026-04-18) the settings screen includes a virtual
"Git remote profiles (view)" entry as its first row when any profile
rows are wired in. That entry is a read-only view into
``[[git_remote_profiles]]`` — Enter opens :func:`_show_git_remotes`.
It replaces the former hidden ``g`` shortcut (retired in 0.10.4) with
an explicit, visible, list-driven entry. No bare-key shortcut reaches
any sub-screen anymore.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from ccw_tui_widgets import confirm_yn, dim, flash_error, text_input

if TYPE_CHECKING:
    from blessed import Terminal

    from ccw_settings import SettingEntry


# ── Callback bundle ──────────────────────────────────────────────────


@dataclass
class SettingsCallbacks:
    """Thin glue that the settings UI calls to persist changes."""

    get_entries: Callable[[], list]  # -> list[SettingEntry] (re-read after each write)
    save_setting: Callable[[str, Any], None]  # (key, new_value)
    remove_setting: Callable[[str], None]  # (key) — revert to default
    save_mapping: Callable[[str, dict], None]  # (key, new_mapping)
    # Optional: returns full profile rows for a read-only subscreen.
    # Each row is a tuple (name, host, owner, auth, creds_user_display, visibility, token_file).
    get_git_remote_profile_rows: "Callable[[], list[tuple]]" = None  # type: ignore[assignment]


# ── Formatting ───────────────────────────────────────────────────────


def _format_value(entry: "SettingEntry") -> str:
    v = entry.value
    kind = entry.spec.kind
    if kind == "bool":
        return "true" if v else "false"
    if kind == "table":
        if not v:
            return "(empty)"
        return ", ".join(f"{k}→{vv}" for k, vv in sorted(v.items()))
    if kind == "array":
        return ", ".join(v) if v else "(empty)"
    if v is None or v == "":
        return "(unset)"
    return str(v)


def _source_label(t: "Terminal", source: str) -> str:
    if source == "repo":
        return t.green("repo")
    if source == "default":
        return dim(t, "default")
    return t.yellow(source)  # project:<path>


# ── Main settings screen ─────────────────────────────────────────────


# Sentinel label used to identify the virtual "Git remote profiles (view)"
# row at the top of the settings list. It has no SettingEntry because
# it is not a persistable key/value — it's a link to a read-only
# sub-screen. Exposed as a module constant so tests can assert the
# entry is rendered without knowing internal string details.
GIT_REMOTES_VIEW_LABEL = "Git remote profiles (view)"


def show_settings(t: "Terminal", cbs: SettingsCallbacks) -> None:
    """Show the settings list. Calls :meth:`cbs.get_entries` whenever the
    data needs to be refreshed (initial load + after every write).

    As of PR 10 (2026-04-18) the first row is a virtual
    "Git remote profiles (view)" entry (when the callbacks wire one
    in) that opens the read-only profiles sub-screen on Enter. This
    replaces the former hidden ``g`` shortcut with an explicit,
    visible list entry — no bare-key shortcut reaches any sub-screen.
    """
    entries = cbs.get_entries()
    cursor = 0
    scroll_offset = 0

    # Compute whether we expose the virtual git-remotes row. It exists
    # iff the callback bundle has a profile-rows provider (wired by
    # bin/ccw whenever the user has sudo).
    def _has_git_remotes_view() -> bool:
        return cbs.get_git_remote_profile_rows is not None

    while True:
        if not entries:
            return

        # Virtual rows prepended to the settings list. Each row is a
        # callable-on-Enter link; it has no SettingEntry and cannot be
        # reset via 'x'. Indices 0..len(virtual)-1 are virtual; the
        # remaining indices map 1:1 to entries offset by len(virtual).
        virtual_rows: list[tuple[str, Callable[[], None]]] = []
        if _has_git_remotes_view():
            def _open_git_remotes() -> None:
                rows = cbs.get_git_remote_profile_rows() if cbs.get_git_remote_profile_rows else []
                _show_git_remotes(t, rows)
            virtual_rows.append((GIT_REMOTES_VIEW_LABEL, _open_git_remotes))

        total_rows = len(virtual_rows) + len(entries)
        if cursor >= total_rows:
            cursor = max(0, total_rows - 1)

        print(t.home + t.clear, end="")
        print(t.bold_white_on_blue(" ⚙ Settings (repo-level) "))
        print(dim(t, "─" * t.width))
        print(
            "  "
            + dim(
                t,
                "SRC: repo=config/config.toml · project=.ccw.toml (read-only) · default=built-in",
            )
        )
        print()

        key_w = max(len(e.spec.key) for e in entries)
        val_w = min(40, max(len(_format_value(e)) for e in entries))

        available = max(3, t.height - 9)
        if cursor < scroll_offset:
            scroll_offset = cursor
        elif cursor >= scroll_offset + available:
            scroll_offset = cursor - available + 1
        visible_end = min(scroll_offset + available, total_rows)

        for i in range(scroll_offset, visible_end):
            sel = i == cursor
            prefix = t.bold_cyan("▸ ") if sel else "  "
            num = i + 1
            if 1 <= num <= 9:
                num_str = dim(t, f"{num} ") if not sel else f"{num} "
            else:
                num_str = "  "

            if i < len(virtual_rows):
                # Virtual link row (read-only).
                vlabel, _ = virtual_rows[i]
                label_str = t.bold_yellow(vlabel) if sel else vlabel
                line = f"{prefix}{num_str}{label_str}  {dim(t, '(Enter to view)')}"
                if sel:
                    print(t.reverse(t.ljust(line, t.width)))
                else:
                    print(line)
                continue

            e = entries[i - len(virtual_rows)]
            key_str = e.spec.key.ljust(key_w)
            if sel:
                key_str = t.bold(key_str)
            val = _format_value(e)
            if len(val) > val_w:
                val = val[: val_w - 1] + "…"
            val_str = val.ljust(val_w)
            src_str = _source_label(t, e.source)
            ro_mark = "" if e.editable else dim(t, " (ro)")
            line = f"{prefix}{num_str}{key_str}  {val_str}  {src_str}{ro_mark}"
            if sel:
                print(t.reverse(t.ljust(line, t.width)))
            else:
                print(line)

        # Description for selected entry
        if 0 <= cursor < total_rows:
            with t.location(0, t.height - 3):
                if cursor < len(virtual_rows):
                    desc = "Read-only view of [[git_remote_profiles]] in config/config.toml."
                else:
                    desc = entries[cursor - len(virtual_rows)].spec.description
                print(t.clear_eol + "  " + dim(t, desc), end="")

        footer_y = t.height - 1
        with t.location(0, footer_y):
            from ccw_tui import _build_footer  # lazy to avoid cycle
            print("  " + _build_footer(t, "settings"), end="")

        key = t.inkey(timeout=None)
        if key.name == "KEY_ESCAPE" or key == "q":
            return
        if key.name == "KEY_UP" or key == "k":
            cursor = max(0, cursor - 1)
        elif key.name == "KEY_DOWN" or key == "j":
            cursor = min(total_rows - 1, cursor + 1)
        elif key.name == "KEY_HOME":
            cursor = 0
        elif key.name == "KEY_END" or key == "G":
            cursor = total_rows - 1
        elif key.name == "KEY_ENTER" or key == "\n" or key == "\r":
            if cursor < len(virtual_rows):
                _, activate = virtual_rows[cursor]
                activate()
                # After returning from the sub-screen, reload entries
                # in case the underlying config was edited externally.
                entries = cbs.get_entries()
            else:
                changed = _edit_entry(t, entries[cursor - len(virtual_rows)], cbs)
                if changed:
                    entries = cbs.get_entries()
                    total_rows = len(virtual_rows) + len(entries)
                    if cursor >= total_rows:
                        cursor = max(0, total_rows - 1)
        elif key == "x":
            if cursor < len(virtual_rows):
                # Virtual rows are read-only; 'x' is a no-op here.
                continue
            e = entries[cursor - len(virtual_rows)]
            if e.editable and e.source == "repo":
                if confirm_yn(
                    t,
                    t.bold_red(f"  Reset {e.spec.key} to default? ") + dim(t, "y/N "),
                    t.height - 3,
                ):
                    try:
                        cbs.remove_setting(e.spec.key)
                        entries = cbs.get_entries()
                    except Exception as exc:  # pragma: no cover - user-visible error
                        flash_error(t, str(exc))
        elif not key.is_sequence and str(key) in "123456789":
            idx = int(str(key)) - 1
            if 0 <= idx < total_rows:
                cursor = idx


# ── Git remotes: read-only view ──────────────────────────────────────


def _show_git_remotes(t: "Terminal", rows: list) -> None:
    """Read-only table of [[git_remote_profiles]]. Edit via config.toml."""
    while True:
        print(t.home + t.clear, end="")
        print(t.bold_white_on_blue(" Git remote profiles (read-only) "))
        print(dim(t, "─" * t.width))
        print(
            "  "
            + dim(
                t,
                "To add/edit profiles, open config/config.toml directly.",
            )
        )
        print()

        if not rows:
            print("  " + dim(t, "(no profiles configured)"))
        else:
            headers = ("name", "host", "owner", "auth", "creds_user", "visibility", "token_file")
            widths = [max(len(h), max(len(str(r[i])) for r in rows)) for i, h in enumerate(headers)]
            header_line = "  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
            print(t.bold(header_line))
            print("  " + dim(t, "  ".join("─" * w for w in widths)))
            for r in rows:
                parts = [str(r[i]).ljust(widths[i]) for i in range(len(headers))]
                print("  " + "  ".join(parts))

        footer_y = t.height - 1
        with t.location(0, footer_y):
            print(dim(t, "  Esc back"), end="")

        key = t.inkey(timeout=None)
        if key.name == "KEY_ESCAPE" or key == "q":
            return


# ── Per-type edit dispatcher ─────────────────────────────────────────


def _edit_entry(t: "Terminal", entry: "SettingEntry", cbs: SettingsCallbacks) -> bool:
    """Dispatch an edit by kind. Returns True if something changed."""
    if not entry.editable:
        flash_error(t, f"{entry.spec.key} is a project-level override ({entry.source}); edit the .ccw.toml directly")
        return False

    spec = entry.spec
    if spec.kind == "bool":
        new_val = not bool(entry.value)
        return _try_save(t, lambda: cbs.save_setting(spec.key, new_val))

    if spec.kind == "enum":
        choices = list(spec.choices or ())
        if not choices:
            return False
        cur = str(entry.value) if entry.value is not None else ""
        try:
            idx = choices.index(cur)
        except ValueError:
            idx = -1
        new_val = choices[(idx + 1) % len(choices)]
        return _try_save(t, lambda: cbs.save_setting(spec.key, new_val))

    if spec.kind == "string":
        current = "" if entry.value is None else str(entry.value)
        result = text_input(t, f"Edit {spec.key}", current=current, detail=spec.description)
        if result is None:
            return False
        return _try_save(t, lambda: cbs.save_setting(spec.key, result))

    if spec.kind == "array":
        items = entry.value if isinstance(entry.value, list) else []
        current = ", ".join(str(x) for x in items)
        detail = spec.description + "  (comma-separated values)"
        result = text_input(t, f"Edit {spec.key}", current=current, detail=detail)
        if result is None:
            return False
        new_list = [p.strip() for p in result.split(",") if p.strip()]
        return _try_save(t, lambda: cbs.save_setting(spec.key, new_list))

    if spec.kind == "table":
        return _edit_mapping(t, entry, cbs)

    flash_error(t, f"unsupported kind: {spec.kind}")
    return False


def _try_save(t: "Terminal", op) -> bool:
    try:
        op()
        return True
    except Exception as exc:  # pragma: no cover - user-visible
        flash_error(t, str(exc))
        return False


# ── Mapping editor (launch_user_by_caller) ───────────────────────────


def _edit_mapping(t: "Terminal", entry: "SettingEntry", cbs: SettingsCallbacks) -> bool:
    """Sub-screen for editing a string→string table. Returns True if saved."""
    mapping: dict[str, str] = dict(entry.value or {})
    cursor = 0

    while True:
        items = sorted(mapping.items())
        if cursor >= len(items):
            cursor = max(0, len(items) - 1)

        print(t.home + t.clear, end="")
        print(t.bold_white_on_blue(f" ⚙ Edit {entry.spec.key} "))
        print(dim(t, "─" * t.width))
        print("  " + dim(t, entry.spec.description))
        print()

        if not items:
            print("  " + dim(t, "(empty — press 'a' to add a mapping)"))
        else:
            key_w = max(len(k) for k, _ in items)
            for i, (k, v) in enumerate(items):
                sel = i == cursor
                prefix = t.bold_cyan("▸ ") if sel else "  "
                text = f"{k.ljust(key_w)} → {v}"
                if sel:
                    print(t.reverse(t.ljust(prefix + text, t.width)))
                else:
                    print(prefix + text)

        footer_y = t.height - 1
        with t.location(0, footer_y):
            print(dim(t, "  Enter edit · a add · d delete · s save · Esc cancel"), end="")

        key = t.inkey(timeout=None)
        if key.name == "KEY_ESCAPE":
            return False
        if key == "s":
            return _try_save(t, lambda: cbs.save_mapping(entry.spec.key, mapping))
        if key.name == "KEY_UP" or key == "k":
            cursor = max(0, cursor - 1)
        elif key.name == "KEY_DOWN" or key == "j":
            cursor = min(max(0, len(items) - 1), cursor + 1)
        elif key == "a":
            k = text_input(t, f"Add to {entry.spec.key}", "", detail="Caller user (key):")
            if k is None or not k.strip():
                continue
            k = k.strip()
            v = text_input(
                t,
                f"Add to {entry.spec.key}",
                "",
                detail=f"Launch user for caller '{k}':",
            )
            if v is None or not v.strip():
                continue
            mapping[k] = v.strip()
        elif key.name == "KEY_ENTER" or key == "\n" or key == "\r":
            if items:
                k, v = items[cursor]
                new_v = text_input(
                    t,
                    f"Edit {k}",
                    current=v,
                    detail=f"Launch user for caller '{k}':",
                )
                if new_v is None or not new_v.strip():
                    continue
                mapping[k] = new_v.strip()
        elif key == "d":
            if items:
                k, _ = items[cursor]
                if confirm_yn(
                    t,
                    t.bold_red(f"  Remove mapping for '{k}'? ") + dim(t, "y/N "),
                    t.height - 3,
                ):
                    mapping.pop(k, None)

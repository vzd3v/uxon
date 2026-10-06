# SPDX-License-Identifier: MIT
"""Native tmux error-screen controls; no watcher, polling, or root-key overrides."""

from __future__ import annotations

import hashlib
import re
import shlex

from uxon.infra.launch_records import LAUNCH_NONCE_ENV

DISMISS_EXIT_CODE = 125
_HOOK_INDEX = 42000
_LIVE_TABLE = (
    "#{?#{==:#{key-table},#{@uxon-diagnostics-table}},#{@uxon-live-key-table},#{key-table}}"
)


def _binding_command(text: str) -> str:
    """Undo list-keys' outer separator escaping, preserving quoted arguments."""
    result: list[str] = []
    quote = ""
    depth = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            following = text[index + 1]
            result.append(
                following if following == ";" and not quote and not depth else char + following
            )
            index += 2
            continue
        if char in {"'", '"'}:
            if quote == char:
                quote = ""
            elif not quote:
                quote = char
        elif not quote:
            depth += (char == "{") - (char == "}")
        result.append(char)
        index += 1
    return "".join(result)


def diagnostics_query(session: str) -> list[str]:
    """Append one live-table line and native bindings to an existing query."""
    return [
        ";",
        "display-message",
        "-p",
        "-t",
        session,
        _LIVE_TABLE,
        ";",
        "list-keys",
    ]


def _diagnostics_commands(session: str, live_table: str, bindings: str) -> list[list[str]]:
    """Install idempotent, managed-session-only controls on tmux 3.2+."""
    # Share by original table, not session name: the native commands resolve
    # session options at execution time, and finished sessions leave no new
    # table behind for every historical launch.
    key_table = "uxon-diagnostics-" + hashlib.sha256(live_table.encode()).hexdigest()[:16]
    # list-keys prints native command text, including brace groups. Parse only
    # its canonical prefix; never tokenize/reconstruct the command tail.
    pattern = re.compile(
        r"^bind-key\s+(?P<repeat>-r\s+)?-T\s+"
        + re.escape(live_table)
        + r"\s+(?P<key>(?:\\.|[^\s])+?)\s+(?P<command>.+)$"
    )
    original: dict[str, tuple[bool, str]] = {}
    for line in bindings.splitlines():
        match = pattern.fullmatch(line)
        if match:
            key = shlex.split(match["key"])[0]
            original[key] = (bool(match["repeat"]), _binding_command(match["command"]))
    chain: list[list[str]] = []

    def add(*argv: str) -> None:
        chain.append(list(argv))

    def native(argv: list[str]) -> str:
        # Brace groups are native tmux command arguments (available in 3.2).
        # They keep nested conditionals compact without repeated escaping.
        if argv[:2] == ["if-shell", "-F"]:
            return shlex.join(argv[:3]) + " " + " ".join("{ " + cmd + " }" for cmd in argv[3:])
        return shlex.join(argv)

    managed = "#{&&:#{!=:#{E:" + LAUNCH_NONCE_ENV + "},},#{!=:#{@uxon-live-key-table},}}"
    # Event panes need not be selected. Inspect the session's actual current
    # window/pane rather than inheriting a pane-died hook's stale target.
    selected_dead = "#{W:#{?window_active,#{P:#{?pane_active,#{pane_dead},}},}}"
    sync_table = native(
        [
            "if-shell",
            "-F",
            selected_dead,
            native(["set-option", "-F", "key-table", "#{@uxon-diagnostics-table}"]),
            native(["set-option", "-F", "key-table", "#{@uxon-live-key-table}"]),
        ]
    )
    save_options = " ; ".join(
        native(command)
        for command in (
            ["set-option", "-wF", "@uxon-border-status", "#{pane-border-status}"],
            ["set-option", "-wF", "@uxon-border-format", "#{pane-border-format}"],
        )
    )
    save_border = (
        native(["if-shell", "-F", "#{==:#{@uxon-border-status},}", save_options])
        + " ; "
        + " ; ".join(
            native(command)
            for command in (
                [
                    "set-option",
                    "-wF",
                    "pane-border-status",
                    "#{?#{==:#{@uxon-border-status},off},bottom,#{@uxon-border-status}}",
                ],
                [
                    "set-option",
                    "-w",
                    "pane-border-format",
                    "#{?pane_dead,Process exited#{?pane_dead_status, (#{pane_dead_status}),}. Enter / Esc / q: close and return to Uxon.,#{E:@uxon-border-format}}",
                ],
            )
        )
    )
    restore_border = " ; ".join(
        native(command)
        for command in (
            ["set-option", "-wF", "pane-border-status", "#{@uxon-border-status}"],
            ["set-option", "-wF", "pane-border-format", "#{@uxon-border-format}"],
            ["set-option", "-wu", "@uxon-border-status"],
            ["set-option", "-wu", "@uxon-border-format"],
        )
    )
    sync_border = native(
        [
            "if-shell",
            "-F",
            "#{m:*1*,#{P:#{pane_dead}}}",
            save_border,
            native(["if-shell", "-F", "#{!=:#{@uxon-border-status},}", restore_border]),
        ]
    )
    update = native(["if-shell", "-F", managed, sync_border + " ; " + sync_table])
    ack = " ; ".join(
        (
            native(["set-option", "-pF", "@uxon-dismissed", "#{pane_pid}"]),
            native(["detach-client", "-E", f"exit {DISMISS_EXIT_CODE}"]),
        )
    )
    guard = "#{&&:#{pane_dead},#{==:#{pane_in_mode},0}}"
    restore_live = native(["set-option", "-F", "key-table", "#{@uxon-live-key-table}"])

    def bind(key: str, dead_command: str) -> None:
        repeat, command = original.get(
            key, original.get("Any", (False, native(["send-keys", key])))
        )
        live_command = restore_live + " ; " + command
        flags = ["-r"] if repeat else []
        add(
            "bind-key",
            *flags,
            "-T",
            key_table,
            key,
            native(["if-shell", "-F", guard, dead_command, live_command]),
        )

    add("bind-key", "-T", key_table, "Any")
    add("unbind-key", "-a", "-T", key_table)
    for key, (repeat, command) in original.items():
        flags = ["-r"] if repeat else []
        add("bind-key", *flags, "-T", key_table, key, command)
    for key in ("Enter", "Escape", "q"):
        bind(key, ack)
    for key, command in (
        ("WheelUpPane", "copy-mode -e"),
        ("MouseDown1Pane", "select-pane -t ="),
        ("MouseDrag1Pane", "copy-mode -M"),
    ):
        bind(key, command)
    add("set-option", "-t", session, "@uxon-live-key-table", live_table)
    add("set-option", "-t", session, "@uxon-diagnostics-table", key_table)
    # Pane/window events are window-scoped in tmux. A guarded global hook
    # covers new and linked windows without installing per-window watchers.
    for hook in ("pane-died", "window-pane-changed", "window-layout-changed"):
        add("set-hook", "-g", f"{hook}[{_HOOK_INDEX}]", update)
    for hook in ("session-window-changed", "client-attached", "client-session-changed"):
        add("set-hook", "-t", session, f"{hook}[{_HOOK_INDEX}]", update)
    add("if-shell", "-F", "-t", session, "1", update)
    return chain


def diagnostics_script(session: str, live_table: str, bindings: str) -> str:
    """Stream configuration instead of exceeding tmux's argv protocol limit."""
    return (
        "\n".join(shlex.join(argv) for argv in _diagnostics_commands(session, live_table, bindings))
        + "\n"
    )

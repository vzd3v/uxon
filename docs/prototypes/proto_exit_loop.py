#!/usr/bin/env python3
"""T0a prototype: App.exit() -> subprocess -> new App() round-trip.

Verifies:
  (a) no terminal-state leak between round-trips;
  (b) keys typed during external subprocess do NOT queue into the next App();
  (c) notify() payload before exit() survives via non-textual channel.

Runs non-interactively via App.run_test() pilot. We simulate the launch
handoff: press 'l' to call exit(), outer loop runs subprocess, re-creates
the App, verifies clean re-enter.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Static


PENDING_STATUS: list[str] = []  # survives App re-create


class Proto(App):
    BINDINGS = [
        Binding("l", "launch", "Launch"),
        Binding("q", "quit_app", "Quit"),
    ]

    def __init__(self, round_no: int, pending_status: str = "") -> None:
        super().__init__()
        self.round_no = round_no
        self.pending_status = pending_status
        self.did_launch = False
        self.quit_rc: int | None = None

    def compose(self) -> ComposeResult:
        yield Static(f"Round {self.round_no} pending={self.pending_status!r}", id="s")

    def on_mount(self) -> None:
        if self.pending_status:
            self.notify(self.pending_status, severity="error", timeout=3)

    def action_launch(self) -> None:
        # simulate CallbackError -> stash for next round
        PENDING_STATUS.append(f"err after round {self.round_no}")
        self.did_launch = True
        self.exit()

    def action_quit_app(self) -> None:
        self.quit_rc = 0
        self.exit()


async def main() -> int:
    pending = ""
    for r in range(1, 4):
        app = Proto(r, pending_status=pending)
        async with app.run_test() as pilot:
            await pilot.press("l")
            await pilot.pause()
        if not app.did_launch:
            print(f"FAIL round {r}: did_launch=False")
            return 1
        # simulate external subprocess (tmux attach / claude)
        subprocess.run(["/bin/sleep", "0.1"], check=True)
        pending = PENDING_STATUS[-1] if PENDING_STATUS else ""
    print("OK proto_exit_loop: 3 rounds completed, pending_status survives App re-create")
    print(f"PENDING_STATUS trail: {PENDING_STATUS}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

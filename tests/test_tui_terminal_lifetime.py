"""Terminal-lifetime regression tests for the outer TUI runner."""

from __future__ import annotations

import os
import threading

from uxon.tui.runner import _run_app_with_terminal_lifetime


def test_terminal_hangup_requests_app_exit() -> None:
    master_fd, slave_fd = os.openpty()
    exit_requested = threading.Event()
    request_count = 0

    def request_exit() -> None:
        nonlocal request_count
        request_count += 1
        exit_requested.set()

    def run_app() -> None:
        os.close(master_fd)
        assert exit_requested.wait(timeout=2)

    try:
        _run_app_with_terminal_lifetime(run_app, request_exit, slave_fd)
    finally:
        os.close(slave_fd)

    assert request_count == 1


def test_normal_app_exit_stops_terminal_watcher() -> None:
    master_fd, slave_fd = os.openpty()
    exit_requested = False

    def request_exit() -> None:
        nonlocal exit_requested
        exit_requested = True

    try:
        _run_app_with_terminal_lifetime(lambda: None, request_exit, slave_fd)
    finally:
        os.close(master_fd)
        os.close(slave_fd)

    assert exit_requested is False

# SPDX-License-Identifier: MIT
"""Real terminal regressions for failed-screen acknowledgement and live siblings."""

from __future__ import annotations

import json
import os
import pty
import signal
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pytest
from helpers import make_config

from uxon.app.session_handoff import run_launch_request
from uxon.domain.config import Config
from uxon.domain.execution import ExecutionTarget
from uxon.domain.launch_profiles import ResolvedLaunchProfile
from uxon.domain.launch_request import LaunchRequest, TmuxHandoff
from uxon.infra import launch_records, process, sessions_probe, tmux
from uxon.infra.identity import process_user
from uxon.infra.run import run_query
from uxon.infra.tmux_diagnostics import diagnostics_script

pytestmark = pytest.mark.slow
_SESSION = "uxon-workspace@claude"


def _wait(predicate: Callable[[], Any]) -> Any:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("terminal condition did not become true")


@contextmanager
def _operations() -> Iterator[Counter[str]]:
    counts: Counter[str] = Counter()
    active = True

    def audit(event: str, _args: tuple[Any, ...]) -> None:
        if active and event in {"subprocess.Popen", "open", "os.listdir", "os.scandir"}:
            counts[event] += 1

    sys.addaudithook(audit)
    try:
        yield counts
    finally:
        active = False


@dataclass
class _Client:
    pid: int
    fd: int
    result: Path
    reaped: bool = False

    def send(self, keys: bytes) -> None:
        os.write(self.fd, keys)

    def finish(self) -> dict[str, Any]:
        _wait(self.result.exists)
        _, status = os.waitpid(self.pid, 0)
        self.reaped = True
        assert os.waitstatus_to_exitcode(status) == 0
        return json.loads(self.result.read_text())

    def close(self) -> None:
        if not self.reaped:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            os.waitpid(self.pid, 0)
        os.close(self.fd)


@dataclass
class _Terminal:
    directory: Path
    cfg: Config
    user: str
    record: Path
    clients: list[_Client] = field(default_factory=list)

    def command(self, *args: str, check: bool = True) -> str:
        result = run_query(
            ["tmux", "-f", "/dev/null", "-S", self.cfg.tmux_socket_template, *args],
            timeout=5,
            check=False,
        )
        if check and result.returncode:
            raise AssertionError(result.stderr or result.stdout)
        return result.stdout.strip()

    def value(self, target: str, fmt: str) -> str:
        return self.command("display-message", "-p", "-t", target, fmt)

    def fail(self, pane: str) -> None:
        self.command("respawn-pane", "-k", "-t", pane, "sh", "-c", "echo TEST FAILURE; exit 17")
        _wait(lambda: self.value(pane, "#{pane_dead}") == "1")

    def snapshot(self):
        return sessions_probe.collect_session_snapshot_for_user(
            self.cfg, self.user, self.cfg.session_prefix, self.cfg.tmux_socket_template
        )

    def install(self, table: str) -> None:
        tmux._install_diagnostics(
            tmux.configured_tmux_base(self.cfg, self.user),
            diagnostics_script(_SESSION, table, self.command("list-keys", "-T", table)),
        )

    def attach(self, request: LaunchRequest | None = None) -> _Client:
        result_path = self.directory / f"client-{len(self.clients)}.json"
        request = request or LaunchRequest(
            tuple(
                tmux.configured_tmux_base(self.cfg, self.user) + ["attach-session", "-t", _SESSION]
            ),
            handoff=TmuxHandoff(self.cfg, self.user, _SESSION, True),
        )
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.environ["TERM"] = "xterm-256color"
                with _operations() as counts:
                    result = run_launch_request(request)
                result_path.write_text(json.dumps({**asdict(result), "operations": dict(counts)}))
                os._exit(0)
            except BaseException as exc:
                os.write(2, repr(exc).encode())
                os._exit(1)
        client = _Client(pid, fd, result_path)
        self.clients.append(client)
        _wait(
            lambda: (
                len(self.command("list-clients", "-F", "#{client_name}").splitlines())
                == sum(not item.reaped for item in self.clients)
            )
        )
        return client


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Terminal]:
    monkeypatch.delenv("TMUX", raising=False)
    with tempfile.TemporaryDirectory(prefix="uxon-diagnostics-") as temporary:
        directory = Path(temporary)
        monkeypatch.setenv("XDG_STATE_HOME", str(directory / "state"))
        cfg = make_config(tmux_socket_template=str(directory / "tmux.sock"))
        user = process_user()
        resolved = ResolvedLaunchProfile(
            cfg.launch.profiles["claude"],
            cfg.agents["claude"],
            user,
            execution=ExecutionTarget(user, cfg.execution.backend_for_user(user)),
        )
        pending = launch_records.pending_from_resolved(
            socket_path=cfg.tmux_socket_template,
            session_name=_SESSION,
            resolved=resolved,
            target_dir=str(directory),
        )
        record = launch_records.create_pending_record(pending)
        terminal = _Terminal(directory, cfg, user, record)
        terminal.command(
            "new-session",
            "-d",
            "-s",
            _SESSION,
            "-e",
            f"{launch_records.LAUNCH_NONCE_ENV}={pending.launch_nonce}",
            "sleep",
            "600",
            ";",
            "set-window-option",
            "-t",
            _SESSION,
            "remain-on-exit",
            "failed",
            ";",
            "set-option",
            "-t",
            _SESSION,
            "status",
            "off",
        )
        metadata = terminal.value(_SESSION, "#{session_id}\t#{session_created}").split("\t")
        launch_records.finalize_pending_record(
            pending,
            launch_records.TmuxSessionMetadata(
                metadata[0], metadata[1], _SESSION, pending.launch_nonce
            ),
        )
        try:
            terminal.install("root")
            yield terminal
        finally:
            terminal.command("kill-server", check=False)
            for client in terminal.clients:
                client.close()


@pytest.mark.parametrize("key", [b"\r", b"\x1b", b"q"], ids=["Enter", "Escape", "q"])
def test_acknowledgement_closes_last_failed_screen_and_record(
    terminal: _Terminal, key: bytes
) -> None:
    pane = terminal.value(_SESSION, "#{pane_id}")
    terminal.fail(pane)
    _wait(lambda: "Enter / Esc / q" in terminal.value(pane, "#{E:pane-border-format}"))
    client = terminal.attach()
    client.send(key)
    result = client.finish()
    assert (result["rc"], result["warning"]) == (0, "")
    assert terminal.snapshot().sessions == ()
    assert not terminal.record.exists()


def test_error_acknowledgement_preserves_live_sibling_and_copy_mode(terminal: _Terminal) -> None:
    pane = terminal.value(_SESSION, "#{pane_id}")
    sibling = terminal.command(
        "new-window", "-d", "-P", "-F", "#{pane_id}", "-t", _SESSION, "sleep", "600"
    )
    sibling_pid = int(terminal.value(sibling, "#{pane_pid}"))
    identity = Path(f"/proc/{sibling_pid}/stat").read_text()
    terminal.fail(pane)
    client = terminal.attach()
    terminal.command("copy-mode", "-t", pane)
    client.send(b"q")
    _wait(lambda: terminal.value(pane, "#{pane_in_mode}") == "0")
    assert terminal.value(pane, "#{@uxon-dismissed}") == ""
    assert not client.result.exists()
    client.send(b"q")
    assert client.finish()["warning"] == ""
    assert terminal.value(sibling, "#{pane_pid}") == str(sibling_pid)
    assert Path(f"/proc/{sibling_pid}/stat").read_text().split()[21] == identity.split()[21]
    assert terminal.snapshot().sessions[0].pane_pids == (sibling_pid,)
    assert terminal.record.exists()


def test_mixed_session_attach_selects_live_terminal_in_another_window(terminal: _Terminal) -> None:
    pane = terminal.value(_SESSION, "#{pane_id}")
    sibling = terminal.command(
        "new-window", "-d", "-P", "-F", "#{pane_id}", "-t", _SESSION, "sleep", "600"
    )
    terminal.fail(pane)
    target = terminal.snapshot().sessions[0]
    assert target.has_diagnostics and not target.exited
    request = tmux._build_tmux_attach_request(target, terminal.cfg, terminal.user)
    client = terminal.attach(request)
    assert terminal.value(_SESSION, "#{pane_id}") == sibling
    assert terminal.value(_SESSION, "#{pane_dead}") == "0"
    client.send(b"\x02d")
    assert client.finish()["warning"] == ""
    assert terminal.value(pane, "#{pane_dead}") == "1"


def test_large_custom_table_is_streamed_without_command_size_limit(terminal: _Terminal) -> None:
    table = "large-live"
    for number in range(1, 51):
        key = chr(ord("a") + number - 1) if number <= 26 else chr(ord("A") + number - 27)
        terminal.command(
            "bind-key", "-T", table, key, "set-option", "-p", "@large-value", "x" * 1024
        )
    terminal.command("set-option", "-t", _SESSION, "key-table", table)
    client = terminal.attach()
    assert (
        len(
            terminal.command(
                "list-keys", "-T", terminal.value(_SESSION, "#{@uxon-diagnostics-table}")
            )
        )
        > 32768
    )
    terminal.command("detach-client")
    result = client.finish()
    assert result["warning"] == ""
    assert result["operations"]["subprocess.Popen"] == 3


def test_unseen_window_failure_and_custom_live_bindings(terminal: _Terminal) -> None:
    original = terminal.value(_SESSION, "#{pane_id}")
    terminal.command("bind-key", "-T", "custom-live", "q", "set-option", "-p", "@custom-q", "works")
    terminal.command("set-option", "-t", _SESSION, "key-table", "custom-live")
    terminal.command("set-option", "-u", "-t", _SESSION, "@uxon-live-key-table")
    terminal.install("custom-live")
    failed = terminal.command(
        "new-window", "-d", "-P", "-F", "#{pane_id}", "-t", _SESSION, "sleep", "600"
    )
    terminal.command("set-window-option", "-t", failed, "remain-on-exit", "failed")
    terminal.command(
        "set-window-option", "-t", failed, "pane-border-format", "Custom #{pane_title}"
    )
    terminal.fail(failed)
    assert terminal.value(original, "#{pane_active}") == "1"
    assert "Enter / Esc / q" in terminal.value(failed, "#{E:pane-border-format}")
    client = terminal.attach()
    client.send(b"q")
    _wait(lambda: terminal.value(original, "#{@custom-q}") == "works")
    terminal.command("select-window", "-t", failed)
    _wait(
        lambda: terminal.command("list-clients", "-F", "#{client_key_table}").startswith(
            "uxon-diagnostics-"
        )
    )
    client.send(b"\x02p")
    _wait(lambda: terminal.command("list-clients", "-F", "#{client_key_table}") == "custom-live")
    client.send(b"\x02d")
    result = client.finish()
    assert result["operations"]["subprocess.Popen"] == 3
    assert result["warning"] == ""
    assert terminal.record.exists()


def test_two_clients_acknowledge_same_error_without_false_failure(terminal: _Terminal) -> None:
    terminal.fail(terminal.value(_SESSION, "#{pane_id}"))
    clients = [terminal.attach(), terminal.attach()]
    for client in clients:
        client.send(b"q")
    assert [
        (item.finish()["rc"], json.loads(item.result.read_text())["warning"]) for item in clients
    ] == [(0, ""), (0, "")]
    assert not terminal.record.exists()


@pytest.mark.parametrize(
    "key", [b"\r", b"\x1b", b"q", b"a", b";"], ids=["Enter", "Escape", "q", "a", "semicolon"]
)
def test_first_custom_key_after_attached_respawn_is_preserved(
    terminal: _Terminal, key: bytes
) -> None:
    pane = terminal.value(_SESSION, "#{pane_id}")
    table = "custom live"
    native_key = {b"\r": "Enter", b"\x1b": "Escape", b"q": "q", b"a": "a", b";": r"\;"}[key]
    terminal.command(
        "bind-key",
        "-r",
        "-T",
        table,
        native_key,
        "set-option -p @first 'literal \\; text' ; if-shell -F 1 { set-option -p @second works }",
    )
    terminal.command("set-option", "-t", _SESSION, "key-table", table)
    client = terminal.attach()
    terminal.fail(pane)
    _wait(
        lambda: terminal.command("list-clients", "-F", "#{client_key_table}").startswith(
            "uxon-diagnostics-"
        )
    )
    terminal.command("respawn-pane", "-t", pane, "sleep", "600")
    client.send(key)
    _wait(lambda: terminal.value(pane, "#{@second}") == "works")
    assert terminal.value(pane, "#{@first}") == r"literal \; text"
    assert terminal.value(pane, "#{pane_dead}") == "0"
    assert not client.result.exists()
    assert "bind-key -r" in terminal.command("list-keys", "-T", table)
    terminal.command("detach-client")
    assert client.finish()["warning"] == ""
    assert terminal.record.exists()


def test_custom_any_binding_handles_first_live_key_after_respawn(terminal: _Terminal) -> None:
    pane = terminal.value(_SESSION, "#{pane_id}")
    terminal.command("bind-key", "-T", "custom-any", "Any", "set-option", "-p", "@any-key", "works")
    terminal.command("set-option", "-t", _SESSION, "key-table", "custom-any")
    client = terminal.attach()
    terminal.fail(pane)
    _wait(
        lambda: terminal.command("list-clients", "-F", "#{client_key_table}").startswith(
            "uxon-diagnostics-"
        )
    )
    terminal.command("respawn-pane", "-t", pane, "sleep", "600")
    client.send(b"q")
    _wait(lambda: terminal.value(pane, "#{@any-key}") == "works")
    terminal.command("detach-client")
    assert client.finish()["warning"] == ""


def test_empty_original_key_table_is_valid(terminal: _Terminal) -> None:
    terminal.command("set-option", "-t", _SESSION, "key-table", "empty-original")
    client = terminal.attach()
    pane = terminal.value(_SESSION, "#{pane_id}")
    terminal.fail(pane)
    _wait(
        lambda: terminal.command("list-clients", "-F", "#{client_key_table}").startswith(
            "uxon-diagnostics-"
        )
    )
    client.send(b"q")
    result = client.finish()
    assert (result["rc"], result["warning"]) == (0, "")
    assert not terminal.record.exists()


def test_respawned_pane_is_not_deleted_by_stale_acknowledgement(
    terminal: _Terminal, monkeypatch: pytest.MonkeyPatch
) -> None:
    pane = terminal.value(_SESSION, "#{pane_id}")
    terminal.fail(pane)
    terminal.command("set-option", "-pF", "-t", pane, "@uxon-dismissed", "#{pane_pid}")
    respawned = False

    def query(argv, **kwargs):
        nonlocal respawned
        if "if-shell" in argv and not respawned:
            respawned = True
            terminal.command("respawn-pane", "-t", pane, "sleep", "600")
        return run_query(argv, **kwargs)

    monkeypatch.setattr(process, "run_query", query)
    request = LaunchRequest(
        ("sh", "-c", "exit 125"), handoff=TmuxHandoff(terminal.cfg, terminal.user)
    )
    result = run_launch_request(request)
    assert respawned
    assert (result.rc, result.warning) == (0, "")
    assert terminal.value(pane, "#{pane_dead}") == "0"
    assert terminal.record.exists()
    terminal.fail(pane)
    assert terminal.snapshot().sessions[0].dismissed_panes == ()


def test_refresh_adds_no_operations_for_other_windows_or_dead_pids(terminal: _Terminal) -> None:
    terminal.snapshot()
    with _operations() as before:
        terminal.snapshot()
    first = terminal.value(_SESSION, "#{pane_id}")
    second = terminal.command(
        "new-window", "-d", "-P", "-F", "#{pane_id}", "-t", _SESSION, "sleep", "600"
    )
    third = terminal.command(
        "split-window", "-d", "-P", "-F", "#{pane_id}", "-t", second, "sleep", "600"
    )
    terminal.fail(first)
    with _operations() as after:
        rows = terminal.snapshot().sessions
    assert before == after
    assert after["subprocess.Popen"] == 4
    assert len(rows) == 1 and not rows[0].exited and rows[0].active_pane_dead
    assert rows[0].active_pid is None
    assert set(rows[0].pane_pids) == {
        int(terminal.value(pane, "#{pane_pid}")) for pane in (second, third)
    }

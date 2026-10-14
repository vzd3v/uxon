# SPDX-License-Identifier: MIT
"""Real terminal regressions for session titles and failed-screen handoff."""

from __future__ import annotations

import json
import os
import pty
import re
import select
import signal
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
from helpers import make_config

from uxon.app.kill import cleanup_launch_record
from uxon.app.launch_profile import resolve_launch_profile
from uxon.app.session_handoff import run_launch_request
from uxon.domain.args import ParsedArgs
from uxon.domain.config import Config
from uxon.domain.launch_request import LaunchRequest, TmuxHandoff
from uxon.infra import launch_records, process, sessions_probe, tmux
from uxon.infra.identity import process_user
from uxon.infra.run import run_query
from uxon.infra.tmux_diagnostics import diagnostics_script

pytestmark = [pytest.mark.slow, pytest.mark.usefixtures("real_execution", "real_host_probes")]
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
        status = self.reap()
        assert os.waitstatus_to_exitcode(status) == 0
        return json.loads(self.result.read_text())

    def reap(self) -> int:
        def exited():
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            return (status,) if pid else None

        (status,) = _wait(exited)
        self.reaped = True
        return status

    def close(self) -> None:
        if not self.reaped:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.reap()
            except AssertionError:
                os.kill(self.pid, signal.SIGKILL)
                self.reap()
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

    def fresh_session(self) -> None:
        previous = self.snapshot().sessions if self.record is not None else ()
        for row in previous:
            self.command("kill-session", "-t", row.tmux_session_id)
            cleanup_launch_record(self.cfg, row)
        running = bool(self.command("list-sessions", check=False))
        resolved = resolve_launch_profile(
            self.cfg, self.user, "claude", str(self.directory), "normal"
        )
        request, pending = tmux.build_managed_tmux_launch_request(
            str(self.directory),
            _SESSION,
            ParsedArgs(action="run", agent_args=[]),
            self.cfg,
            None,
            resolved_profile=resolved,
            server_running=running,
        )
        tmux.prepare_managed_launch(request)
        metadata = self.value(_SESSION, "#{session_id}\t#{session_created}").split("\t")
        self.record = launch_records.record_path(pending)
        assert (
            launch_records.read_verified_record(
                pending.socket_path,
                launch_records.TmuxSessionMetadata(
                    metadata[0], metadata[1], _SESSION, pending.launch_nonce
                ),
            )
            is not None
        )
        try:
            _wait(lambda: self.value(_SESSION, "#{pane_current_command}") == "sleep")
        except AssertionError:
            raise AssertionError(self.command("capture-pane", "-p", "-t", _SESSION)) from None
        self.command("set-option", "-t", _SESSION, "status", "off")
        self.install("root")

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
        stub = directory / "claude"
        stub.write_text("#!/bin/sh\nexec sleep 600\n")
        stub.chmod(0o755)
        cfg = make_config(
            allowed_roots=[str(directory)],
            tmux_socket_template=str(directory / "tmux.sock"),
            tmux_manage_options=True,
        )
        cfg.agents["claude"] = replace(cfg.agents["claude"], binary=str(stub))
        cfg.tmux_append_server_options = {"terminal-overrides": ",*:Tc"}
        user = process_user()
        terminal = _Terminal(directory, cfg, user, None)
        try:
            terminal.command(
                *tmux._tmux_set_chain(cfg), "new-session", "-d", "-s", "anchor", "sleep", "600"
            )
            terminal.fresh_session()
            yield terminal
        finally:
            terminal.command("kill-server", check=False)
            for client in terminal.clients:
                client.close()


def test_session_titles_reach_the_terminal_and_can_be_disabled(
    terminal: _Terminal, monkeypatch: pytest.MonkeyPatch
) -> None:
    def receive(client: _Client, marker: bytes) -> bytes:
        output = bytearray()

        def arrived() -> bool:
            if select.select([client.fd], [], [], 0)[0]:
                output.extend(os.read(client.fd, 65536))
            return marker in output

        _wait(arrived)
        return bytes(output)

    target = terminal.snapshot().sessions[0]
    assert terminal.command("show-options", "-gv", "mouse") == "on"
    assert terminal.command("show-options", "-sv", "extended-keys") == "on"
    assert terminal.command("show-options", "-sv", "terminal-overrides").count("*:Tc") == 1
    # A session-local workload title must not replace the stable session name.
    terminal.command("select-pane", "-t", _SESSION, "-T", "WORKLOAD_TITLE")
    terminal.command("set-option", "-g", "set-titles-string", "GLOBAL_TITLE")
    client = terminal.attach(tmux._build_tmux_attach_request(target, terminal.cfg, terminal.user))
    output = receive(client, b"\x1b]0;" + _SESSION.encode() + b"\x07")
    assert b"\x1b]0;WORKLOAD_TITLE\x07" not in output
    assert terminal.command("show-options", "-gv", "set-titles-string") == "GLOBAL_TITLE"

    sibling = "uxon-other@claude"
    terminal.command("new-session", "-d", "-s", sibling, "sleep", "600")
    sibling_target = next(
        session for session in terminal.snapshot().sessions if session.name == sibling
    )
    with monkeypatch.context() as nested:
        nested.setenv("TMUX", f"{terminal.cfg.tmux_socket_template},0,0")
        terminal.cfg.tmux_options["mouse"] = "off"
        for session in (sibling_target, target):
            request = tmux._build_tmux_attach_request(session, terminal.cfg, terminal.user)
            assert "switch-client" in request.cmd
            assert run_launch_request(request).rc == 0
            receive(client, b"\x1b]0;" + session.name.encode() + b"\x07")
        assert terminal.command("show-options", "-gv", "mouse") == "off"
        assert terminal.command("show-options", "-sv", "terminal-overrides").count("*:Tc") == 1
    terminal.command("kill-session", "-t", sibling)

    renamed = "uxon-renamed@claude"
    terminal.command("rename-session", "-t", _SESSION, renamed)
    receive(client, b"\x1b]0;" + renamed.encode() + b"\x07")
    terminal.command("rename-session", "-t", renamed, _SESSION)
    receive(client, b"\x1b]0;" + _SESSION.encode() + b"\x07")
    terminal.command("detach-client")
    assert client.finish()["rc"] == 0

    # Off must override a previously enabled session on re-entry.
    terminal.cfg.tmux_set_terminal_title = False
    terminal.cfg.tmux_manage_options = False
    client = terminal.attach(tmux._build_tmux_attach_request(target, terminal.cfg, terminal.user))
    terminal.command(
        "respawn-pane",
        "-k",
        "-t",
        _SESSION,
        "sh",
        "-c",
        "printf TITLE_DISABLED_READY; sleep 600",
    )
    output = receive(client, b"TITLE_DISABLED_READY")
    assert b"\x1b]0;" not in output
    assert terminal.command("show-options", "-t", _SESSION, "-v", "set-titles") == "off"
    terminal.command("detach-client")
    assert client.finish()["rc"] == 0


def test_dashboard_title_returns_after_real_tmux_detach(terminal: _Terminal) -> None:
    from harness.pty_tui import run_python_snippet

    code = f"""
import os
from dataclasses import replace
from types import SimpleNamespace
from helpers import make_config
from uxon.infra import probes, sessions_probe, tmux
from uxon.infra.run import run_query
from uxon.tui.context import CallbackError, LaunchProfileOption, TuiContext
from uxon.tui.runner import run

os.environ.pop('TMUX', None)
os.environ['TERM'] = 'xterm-256color'
cfg = make_config(tmux_socket_template={terminal.cfg.tmux_socket_template!r})
user = {terminal.user!r}
target = sessions_probe.collect_sessions([user], cfg)[0]
request = tmux._build_tmux_attach_request(target, cfg, user)
probes.probe_host = lambda *_args: SimpleNamespace(
    tmux=SimpleNamespace(path='/usr/bin/tmux'),
    agents={{'claude': SimpleNamespace(path='/usr/bin/claude')}},
)
rounds = 0
def menu_agents(index):
    spec = cfg.agents['claude']
    mode = replace(spec.permission_modes[0], label=f'title-mode-{{index}}')
    return {{'claude': replace(spec, permission_modes=(mode,))}}
launches = 0
def attach(*_args):
    global launches
    launches += 1
    result = run_query(tmux.configured_tmux_base(cfg, user) + [
        'respawn-pane', '-k', '-t', target.name, 'sh', '-c',
        f'printf TITLE_WORKLOAD_READY-{{launches}}; sleep 600',
    ], timeout=5)
    assert result.returncode == 0, result.stderr
    return request
def refresh():
    global rounds
    rounds += 1
    if rounds == 3:
        raise CallbackError('title-refresh-failed')
    return replace(ctx, cwd_short=f'menu-after-detach-{{rounds}}',
                   tmux_set_terminal_title=rounds == 1, agents=menu_agents(rounds + 1))
ctx = TuiContext(
    sessions=[], total_cpu='0', total_ram='0', version='test',
    cwd='/srv/projects/demo', cwd_short='menu-before-attach',
    new_project_root='/srv/projects', existing_projects=[], cwd_writable=True,
    current_user=user, agents=menu_agents(1), enabled_profiles=('claude',),
    default_profile='claude',
    launch_profiles={{'claude': LaunchProfileOption('claude', 'Claude', 'claude', user)}},
    on_launch_cwd=attach, on_refresh=refresh,
)
rc = run(ctx)
print('TITLE_RUNNER_EXITED', flush=True)
raise SystemExit(rc)
"""
    keys = []
    markers = ("menu-after-detach-1", "menu-after-detach-2", "title-refresh-failed")
    for index, marker in enumerate(markers, start=1):
        keys += [
            (15.0, b"\r", f"1 title-mode-{index}"),
            (10.0, b"\r", f"TITLE_WORKLOAD_READY-{index}"),
        ]
        keys += [(15.0, b"\x02d", marker)]
    keys.append((10.0, b"q", "TITLE_RUNNER_EXITED"))
    trace = run_python_snippet(
        code,
        keys,
        extra_path=[str(Path(__file__).parent)],
        initial_drain=20,
        initial_wait_for="No active sessions",
        timeout=120,
    )
    assert trace.exit_code == 0, trace.plain[-6000:]
    assert "title-refresh-failed" in trace.plain
    titles = re.findall(rb"\x1b\]0;([^\x07]*)\x07", trace.raw)
    assert titles[:3] == [b"uxon", _SESSION.encode(), b"uxon"]
    assert titles.count(b"uxon") == 2  # refreshed opt-out survives refresh failure


def test_two_clients_acknowledge_same_error_without_false_failure(terminal: _Terminal) -> None:
    terminal.fail(terminal.value(_SESSION, "#{pane_id}"))
    clients = [terminal.attach(), terminal.attach()]
    for client in clients:
        client.send(b"q")
    assert [
        (item.finish()["rc"], json.loads(item.result.read_text())["warning"]) for item in clients
    ] == [(0, ""), (0, "")]
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


def test_native_controls_execute_large_bindings_respawn_keys_and_preserve_live_windows(
    terminal: _Terminal, subtests
) -> None:
    pane = terminal.value(_SESSION, "#{pane_id}")
    table = "custom live"
    definitions = []
    for number in range(50):
        key = chr(ord("a") + number) if number < 26 else chr(ord("A") + number - 26)
        definitions.append(f"bind-key -T '{table}' {key} set-option -p @large-value {'x' * 1024}")
    result = process.run_query(
        tmux.configured_tmux_base(terminal.cfg, terminal.user) + ["source-file", "-"],
        input="\n".join(definitions),
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    terminal.command("set-option", "-t", _SESSION, "key-table", table)
    client = terminal.attach()
    diagnostics = terminal.value(_SESSION, "#{@uxon-diagnostics-table}")
    listing = terminal.command("list-keys", "-T", diagnostics)
    assert len(listing) > 32768
    client.send(b"a")
    _wait(lambda: terminal.value(pane, "#{@large-value}") == "x" * 1024)
    terminal.command("set-option", "-pu", "-t", pane, "@large-value")
    tail_binding = [line for line in listing.splitlines() if "@large-value" in line][-1]
    tail_key = tail_binding.split()[3]
    assert listing.index(tail_binding) > 32768 and len(tail_key) == 1
    client.send(tail_key.encode())
    _wait(lambda: terminal.value(pane, "#{@large-value}") == "x" * 1024)
    terminal.command("detach-client")
    assert client.finish()["warning"] == ""

    for key, native in (
        (b"\r", "Enter"),
        (b"\x1b", "Escape"),
        (b"q", "q"),
        (b"a", "a"),
        (b";", r"\;"),
        (b"0", "Any"),
    ):
        with subtests.test(key=native):
            terminal.command("respawn-pane", "-k", "-t", pane, "sleep", "600")
            terminal.command("set-option", "-pu", "-t", pane, "@first")
            terminal.command("set-option", "-pu", "-t", pane, "@second")
            terminal.command("set-option", "-t", _SESSION, "key-table", table)
            terminal.command(
                "bind-key",
                "-r",
                "-T",
                table,
                native,
                "set-option -p @first 'literal \\; text' ; if-shell -F 1 { set-option -p @second works }",
            )
            client = terminal.attach()
            terminal.fail(pane)
            failed_pid = terminal.value(pane, "#{pane_pid}")
            _wait(
                lambda: terminal.command("list-clients", "-F", "#{client_key_table}").startswith(
                    "uxon-diagnostics-"
                )
            )
            terminal.command("respawn-pane", "-t", pane, "sleep", "600")
            assert terminal.value(pane, "#{pane_pid}") != failed_pid
            client.send(key)
            _wait(lambda: terminal.value(pane, "#{@second}") == "works")
            assert terminal.value(pane, "#{@first}") == r"literal \; text"
            assert terminal.value(pane, "#{pane_dead}") == "0" and not client.result.exists()
            assert "bind-key -r" in terminal.command("list-keys", "-T", table)
            terminal.command("detach-client")
            assert client.finish()["warning"] == "" and terminal.record.exists()

    terminal.command("bind-key", "-T", table, "q", "set-option", "-p", "@custom-q", "works")
    terminal.command("set-option", "-t", _SESSION, "key-table", table)
    terminal.command("set-option", "-u", "-t", _SESSION, "@uxon-live-key-table")
    terminal.install(table)
    before_rows = terminal.snapshot().sessions
    with _operations() as before:
        terminal.snapshot()
    unseen = terminal.command(
        "new-window", "-d", "-P", "-F", "#{pane_id}", "-t", _SESSION, "sleep", "600"
    )
    other = terminal.command(
        "split-window", "-d", "-P", "-F", "#{pane_id}", "-t", unseen, "sleep", "600"
    )
    terminal.command("set-window-option", "-t", unseen, "remain-on-exit", "failed")
    terminal.command(
        "set-window-option", "-t", unseen, "pane-border-format", "Custom #{pane_title}"
    )
    terminal.fail(unseen)
    assert terminal.value(pane, "#{pane_active}") == "1"
    assert "Enter / Esc / q" in terminal.value(unseen, "#{E:pane-border-format}")
    with _operations() as after:
        rows = terminal.snapshot().sessions
    assert before == after
    assert len(before_rows) == len(rows) == 1 and rows[0].active_pid is not None
    assert set(rows[0].pane_pids) == {int(terminal.value(p, "#{pane_pid}")) for p in (pane, other)}
    client = terminal.attach()
    client.send(b"q")
    _wait(lambda: terminal.value(pane, "#{@custom-q}") == "works")
    terminal.command("select-window", "-t", unseen)
    _wait(
        lambda: terminal.command("list-clients", "-F", "#{client_key_table}").startswith(
            "uxon-diagnostics-"
        )
    )
    terminal.command("respawn-pane", "-t", unseen, "sleep", "600")
    client.send(b"q")
    _wait(lambda: terminal.value(unseen, "#{@custom-q}") == "works")
    assert terminal.value(unseen, "#{@uxon-border-format}") == "Custom #{pane_title}"
    assert terminal.value(unseen, "#{E:pane-border-format}") == "Custom " + terminal.value(
        unseen, "#{pane_title}"
    )
    terminal.command("select-window", "-t", pane)
    terminal.command("select-window", "-t", unseen)
    _wait(
        lambda: (
            terminal.command("show-window-options", "-t", unseen, "-v", "pane-border-format")
            == "Custom #{pane_title}"
        )
    )
    assert terminal.value(unseen, "#{@uxon-border-format}") == ""
    terminal.fail(unseen)
    terminal.command("copy-mode", "-t", unseen)
    client.send(b"q")
    _wait(lambda: terminal.value(unseen, "#{pane_in_mode}") == "0")
    assert terminal.value(unseen, "#{@uxon-dismissed}") == "" and not client.result.exists()
    sibling_pid = terminal.value(pane, "#{pane_pid}")
    sibling_start = Path(f"/proc/{sibling_pid}/stat").read_text().split()[21]
    client.send(b"q")
    assert client.finish()["warning"] == ""
    assert terminal.value(pane, "#{pane_pid}") == sibling_pid
    assert Path(f"/proc/{sibling_pid}/stat").read_text().split()[21] == sibling_start
    assert terminal.record.exists()
    terminal.command("select-window", "-t", pane)
    terminal.fail(pane)
    target = terminal.snapshot().sessions[0]
    assert not target.exited and target.active_pane_dead
    client = terminal.attach(tmux._build_tmux_attach_request(target, terminal.cfg, terminal.user))
    assert terminal.value(_SESSION, "#{pane_dead}") == "0"
    terminal.command("detach-client")
    assert client.finish()["warning"] == ""
    assert terminal.snapshot().sessions[0].pane_pids


def test_acknowledgement_closes_fresh_failed_sessions_and_records(
    terminal: _Terminal, subtests
) -> None:
    for key, table in ((b"\r", "root"), (b"\x1b", "root"), (b"q", "empty-original")):
        with subtests.test(key=key, table=table):
            terminal.fresh_session()
            record = terminal.record
            terminal.command("set-option", "-t", _SESSION, "key-table", table)
            pane = terminal.value(_SESSION, "#{pane_id}")
            terminal.fail(pane)
            _wait(
                lambda *, pane=pane: (
                    "Enter / Esc / q" in terminal.value(pane, "#{E:pane-border-format}")
                )
            )
            client = terminal.attach()
            client.send(key)
            result = client.finish()
            assert (result["rc"], result["warning"]) == (0, "")
            assert terminal.snapshot().sessions == () and not record.exists()

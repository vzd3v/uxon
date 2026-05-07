# Remote attach Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add remote attach to a configured `[[remote_hosts]]` peer's tmux session, both via TUI Enter on a remote-sessions row and via CLI `uxon attach <id> --host <alias> --user <u>`.

**Architecture:** Aggregator dispatches; peer owns authorisation. One `build_peer_ssh_argv` helper centralises ssh-argv construction so fetch / kill / attach all go through the same template-aware path — fixes the existing kill-remote bug (ignores `command_template`) as a side effect.

**Tech Stack:** Python 3.11+, `unittest`, OpenSSH ControlMaster multiplexing, tmux, sudo for cross-user.

**Spec:** `docs/superpowers/specs/2026-05-03-uxon-remote-attach-design.md`

---

## File structure

| File | Role | Status |
|---|---|---|
| `src/uxon/remote_collector.py` | Add `build_peer_ssh_argv`; refactor `_build_fetch_argv` to use it | modify |
| `src/uxon/cli.py` | Peer-side `do_attach --user`; `attach` parser extension; `_do_attach_remote`; switch `_do_kill_remote` to helper; `on_remote_attach` TUI callback | modify |
| `src/uxon/tui/state.py` | Add `MainIntent.host` field + kind `"attach-remote"` + `remote_session_intent` factory | modify |
| `src/uxon/tui/context.py` | Add `on_remote_attach` callback field | modify |
| `src/uxon/tui/config.py` | Propagate `on_remote_attach` through `TuiConfig.from_context` | modify |
| `src/uxon/tui/screens/main.py` | Branch in `on_data_table_row_selected` for `RemoteSessionTable`; branch in `_run_intent` for `attach-remote`; `_attach_remote_session` method | modify |
| `src/uxon/tui/widgets/remote_session_table.py` | Update docstring (no longer "remote SSH gesture not yet wired") | modify |
| `tests/test_remote_collector.py` | Tests for `build_peer_ssh_argv` and refactor regression | modify |
| `tests/test_uxon_kill_multi.py` | Update `ServerAliveInterval=5` assertion to `=15` after refactor | modify |
| `tests/test_uxon_attach_multi.py` | Tests for `attach --host`/`--user` parser, peer cross-user, `_do_attach_remote` dry-run | create |
| `tests/test_uxon_tui.py` | Tests for `remote_session_intent` factory | modify |
| `tests/test_uxon_tui_remote.py` | Tests for TUI Enter on remote row → callback dispatch | modify |
| `tests/test_uxon_tui_config.py` | Test `on_remote_attach` propagated through `TuiConfig` | modify |

---

## Task 1: `build_peer_ssh_argv` helper

**Files:**
- Modify: `src/uxon/remote_collector.py` (add helper before `_build_fetch_argv`)
- Test: `tests/test_remote_collector.py` (new test class `BuildPeerSshArgvTests`)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_remote_collector.py` after `BuildFetchArgvTests`:

```python
class BuildPeerSshArgvTests(unittest.TestCase):
    def test_default_template_no_tty(self) -> None:
        from uxon.remote_collector import build_peer_ssh_argv

        argv = build_peer_ssh_argv(
            _host(ssh_alias="edge-eu"),
            remote_command="uxon list --json",
            allocate_tty=False,
            connect_timeout=7,
            ssh_multiplex="auto",
        )
        self.assertEqual(argv[0], "ssh")
        self.assertNotIn("-tt", argv)
        self.assertIn("ControlMaster=auto", argv)
        self.assertEqual(argv[-2], "edge-eu")
        self.assertEqual(argv[-1], "uxon list --json")

    def test_allocate_tty_inserts_dash_tt_after_ssh(self) -> None:
        from uxon.remote_collector import build_peer_ssh_argv

        argv = build_peer_ssh_argv(
            _host(),
            remote_command="uxon attach --user alice abc",
            allocate_tty=True,
            connect_timeout=5,
            ssh_multiplex="auto",
        )
        self.assertEqual(argv[0], "ssh")
        self.assertEqual(argv[1], "-tt")

    def test_allocate_tty_skipped_for_non_ssh_first_token(self) -> None:
        # Custom templates that don't start with "ssh" do NOT receive
        # -tt — operator owns interactive-tty plumbing in their argv.
        from uxon.remote_collector import build_peer_ssh_argv

        argv = build_peer_ssh_argv(
            _host(command_template=("kubectl", "exec", "uxon-pod", "--", "/bin/sh", "-c", "{remote_command}")),
            remote_command="uxon attach foo",
            allocate_tty=True,
            connect_timeout=5,
            ssh_multiplex="auto",
        )
        self.assertEqual(argv[0], "kubectl")
        self.assertNotIn("-tt", argv)

    def test_ssh_multiplex_off_strips_control_options(self) -> None:
        from uxon.remote_collector import build_peer_ssh_argv

        argv = build_peer_ssh_argv(
            _host(),
            remote_command="uxon attach abc",
            allocate_tty=True,
            connect_timeout=5,
            ssh_multiplex="off",
        )
        joined = " ".join(argv)
        self.assertNotIn("ControlMaster", joined)
        self.assertNotIn("ControlPath", joined)
        self.assertIn("-tt", argv)  # tty insertion still happens

    def test_custom_command_template_honoured(self) -> None:
        from uxon.remote_collector import build_peer_ssh_argv

        argv = build_peer_ssh_argv(
            _host(command_template=("ssh", "-J", "bastion", "{ssh_alias}", "{remote_command}")),
            remote_command="uxon attach --user alice abc",
            allocate_tty=True,
            connect_timeout=5,
            ssh_multiplex="auto",
        )
        # Operator's jumphost preserved; -tt inserted right after ssh
        # (before -J), so it applies to the outermost ssh.
        self.assertEqual(argv[0], "ssh")
        self.assertEqual(argv[1], "-tt")
        self.assertIn("-J", argv)
        self.assertIn("bastion", argv)
        self.assertEqual(argv[-1], "uxon attach --user alice abc")

    def test_extra_ssh_options_inserted_before_alias(self) -> None:
        from uxon.remote_collector import build_peer_ssh_argv

        argv = build_peer_ssh_argv(
            _host(extra_ssh_options=("-o", "ProxyJump=bastion")),
            remote_command="uxon kill --force abc",
            allocate_tty=False,
            connect_timeout=5,
            ssh_multiplex="auto",
        )
        alias_idx = argv.index("vz-prod1")
        self.assertEqual(argv[alias_idx - 2 : alias_idx], ["-o", "ProxyJump=bastion"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_remote_collector.py::BuildPeerSshArgvTests -v`
Expected: 6 errors with `ImportError: cannot import name 'build_peer_ssh_argv'`

- [ ] **Step 3: Implement helper**

Add to `src/uxon/remote_collector.py` immediately before `def _build_fetch_argv`:

```python
def build_peer_ssh_argv(
    host: RemoteHost,
    *,
    remote_command: str,
    allocate_tty: bool,
    connect_timeout: int,
    ssh_multiplex: str,
) -> list[str]:
    """Single source of truth for ssh-argv to one peer.

    Used by fetch (poller), kill, and attach paths so all three
    honour ``host.command_template`` / ``host.extra_ssh_options`` and
    share the multiplexed ControlMaster started by the poller.

    ``allocate_tty=True`` inserts ``-tt`` immediately after the first
    token when the first token is ``"ssh"`` — interactive sessions
    (attach) need a forced PTY. Custom non-ssh templates (kubectl
    exec etc.) are left alone; the operator owns tty plumbing in
    their argv.

    Selection of template mirrors :func:`_build_fetch_argv`:
      - ``host.command_template`` set → render that directly. Operator
        owns the argv; ``extra_ssh_options`` and ``ssh_multiplex`` are
        ignored because both target the default ssh template.
      - Otherwise → start from :func:`_default_template`, optionally
        strip multiplex options, insert ``host.extra_ssh_options``
        before ``{ssh_alias}``.
    """
    if host.command_template:
        template: list[str] = list(host.command_template)
    else:
        template = _default_template()
        if ssh_multiplex == "off":
            template = _strip_multiplex(template)
        if host.extra_ssh_options:
            try:
                idx = template.index("{ssh_alias}")
            except ValueError:
                idx = len(template)
            template = template[:idx] + list(host.extra_ssh_options) + template[idx:]
    if allocate_tty and template and template[0] == "ssh":
        template = [template[0], "-tt", *template[1:]]
    return _render_argv(
        template,
        ssh_alias=host.ssh_alias,
        remote_uxon=host.remote_uxon,
        connect_timeout=connect_timeout,
        xdg_cache=_xdg_cache_home(),
        remote_command=remote_command,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_remote_collector.py::BuildPeerSshArgvTests -v`
Expected: 6 passing.

- [ ] **Step 5: Commit**

```bash
git add src/uxon/remote_collector.py tests/test_remote_collector.py
git commit -m "feat(remote_collector): add build_peer_ssh_argv helper

Single source of truth for ssh-argv to one peer. Fetch / kill /
attach call sites will be migrated in subsequent commits so all
three honour host.command_template and share the warm
ControlMaster started by the poller."
```

---

## Task 2: Refactor `_build_fetch_argv` to call the helper

**Files:**
- Modify: `src/uxon/remote_collector.py` (`_build_fetch_argv` body)
- Tests: existing `BuildFetchArgvTests` must keep passing.

- [ ] **Step 1: Run existing fetch-argv tests to baseline**

Run: `python -m pytest tests/test_remote_collector.py::BuildFetchArgvTests -v`
Expected: all passing (baseline).

- [ ] **Step 2: Refactor `_build_fetch_argv`**

Replace the body of `_build_fetch_argv` in `src/uxon/remote_collector.py` (currently `cli.py` lines that build template + render) with:

```python
def _build_fetch_argv(
    host: RemoteHost,
    *,
    connect_timeout: int,
    all_users: bool,
    ssh_multiplex: str,
) -> list[str]:
    """Assemble the fetch argv for one host.

    Thin wrapper over :func:`build_peer_ssh_argv` with
    ``allocate_tty=False``. The remote command is the standard
    ``<remote_uxon> list [--all-users] --json`` invocation; the
    ``ALL_USERS_DISABLED_MARKER`` fallback path still works because
    ``all_users=False`` rebuilds the argv with the legacy form.
    """
    remote_command = (
        f"{shlex.quote(host.remote_uxon)} list --all-users --json"
        if all_users
        else f"{shlex.quote(host.remote_uxon)} list --json"
    )
    return build_peer_ssh_argv(
        host,
        remote_command=remote_command,
        allocate_tty=False,
        connect_timeout=connect_timeout,
        ssh_multiplex=ssh_multiplex,
    )
```

- [ ] **Step 3: Run all remote_collector tests**

Run: `python -m pytest tests/test_remote_collector.py -v`
Expected: all passing — fetch tests pin semantic invariants (alias position, BatchMode=yes, etc.), helper produces the same argv shape.

- [ ] **Step 4: Commit**

```bash
git add src/uxon/remote_collector.py
git commit -m "refactor(remote_collector): _build_fetch_argv via build_peer_ssh_argv

Behaviour-preserving thin wrapper. Defended by existing
BuildFetchArgvTests (semantic argv invariants)."
```

---

## Task 3: Switch `_do_kill_remote` to the helper (bug fix)

**Files:**
- Modify: `src/uxon/cli.py` (`_do_kill_remote` ssh-argv construction, ~lines 3730-3740 and matching block in `on_remote_kill` ~3703)
- Tests: `tests/test_uxon_kill_multi.py` — update `ServerAliveInterval=5` assertion.

- [ ] **Step 1: Update kill-remote tests for the new argv shape**

In `tests/test_uxon_kill_multi.py` `KillHostRemoteTests.test_host_executes_ssh_with_expected_argv` (around line 298), change:

```python
        self.assertIn("ServerAliveInterval=5", argv)
```

to:

```python
        # After unification onto build_peer_ssh_argv kill-remote shares
        # the default fetch template, which sets ServerAliveInterval=15.
        self.assertIn("ServerAliveInterval=15", argv)
        # ControlMaster=auto comes for free now — kill reuses the
        # warm master started by the poller.
        self.assertIn("ControlMaster=auto", argv)
```

Add a new test in the same class:

```python
    def test_host_honours_command_template(self) -> None:
        cfg = _make_config(
            remote_hosts=[
                RemoteHost(
                    name="box-b",
                    ssh_alias="ssh-b",
                    description="",
                    remote_uxon="uxon",
                    command_template=("ssh", "-J", "bastion", "{ssh_alias}", "{remote_command}"),
                )
            ]
        )
        args = uxon.ParsedArgs(action="kill", target_id="demo@claude", host="box-b", force=True)
        cp = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(uxon.subprocess, "run", return_value=cp) as srun:
            uxon.do_kill(args, cfg, "u-vz")
        argv = srun.call_args[0][0]
        # Bug fix: kill-remote now honours command_template.
        self.assertIn("-J", argv)
        self.assertIn("bastion", argv)
```

- [ ] **Step 2: Run kill-remote tests to confirm new behaviour fails**

Run: `python -m pytest tests/test_uxon_kill_multi.py::KillHostRemoteTests -v`
Expected: `test_host_executes_ssh_with_expected_argv` fails (ServerAliveInterval=15 not present in current hardcoded argv); `test_host_honours_command_template` fails (jumphost ignored today — this is the bug).

- [ ] **Step 3: Refactor `_do_kill_remote` and `on_remote_kill`**

In `src/uxon/cli.py`, locate `_do_kill_remote` (search for `def _do_kill_remote`) and the `on_remote_kill` callback (search for `def on_remote_kill(host_name`). In each, replace the hardcoded ssh argv construction:

```python
        ssh_argv = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={DEFAULT_CONNECT_TIMEOUT_SEC}",
            "-o",
            "ServerAliveInterval=5",
            peer.ssh_alias,
            remote_cmd,
        ]
```

with a helper call:

```python
        from uxon.remote_collector import build_peer_ssh_argv
        ssh_argv = build_peer_ssh_argv(
            peer,
            remote_command=remote_cmd,
            allocate_tty=False,
            connect_timeout=DEFAULT_CONNECT_TIMEOUT_SEC,
            ssh_multiplex=cfg.ssh_multiplex,
        )
```

(Both `_do_kill_remote` and `on_remote_kill` have a `cfg` in scope — the outer `cfg` from `do_kill`/`tui_context_from_cli`. Use it.)

- [ ] **Step 4: Run kill tests**

Run: `python -m pytest tests/test_uxon_kill_multi.py -v`
Expected: all passing including the new `test_host_honours_command_template`.

- [ ] **Step 5: Commit**

```bash
git add src/uxon/cli.py tests/test_uxon_kill_multi.py
git commit -m "fix(cli): kill-remote now honours command_template

_do_kill_remote and on_remote_kill route through
build_peer_ssh_argv instead of hardcoding ssh argv. Operators
with a custom command_template (e.g. ProxyJump via bastion) now
see kill-remote use the same wrapping as the poller. Side
effect: kill-remote reuses the warm ControlMaster from the
poller, dropping per-call connect cost from ~300ms to <20ms."
```

---

## Task 4: Peer-side `attach --user` parser

**Files:**
- Modify: `src/uxon/cli.py` (`parse_subcommand` attach branch, ~lines 2149-2158)
- Test: `tests/test_uxon_attach_multi.py` (new file)

- [ ] **Step 1: Write failing parser tests**

Create `tests/test_uxon_attach_multi.py`:

```python
"""Tests for ``uxon attach --user`` and ``uxon attach --host`` (multi-host).

Symmetric to ``test_uxon_kill_multi.py``. Parser-level tests live
here; behaviour tests for cross-user / cross-host execution paths
live in companion classes below.
"""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from uxon import cli as uxon
from uxon.remote_hosts import RemoteHost


class AttachParserTests(unittest.TestCase):
    def test_attach_with_user(self) -> None:
        a = uxon.parse_args(["attach", "demo@claude", "--user", "alice"])
        self.assertEqual(a.action, "attach")
        self.assertEqual(a.target_id, "demo@claude")
        self.assertEqual(a.user, "alice")
        self.assertIsNone(a.host)

    def test_attach_with_host_and_user(self) -> None:
        a = uxon.parse_args(["attach", "demo@claude", "--host", "box-b", "--user", "alice"])
        self.assertEqual(a.host, "box-b")
        self.assertEqual(a.user, "alice")

    def test_attach_with_host_without_user_fails(self) -> None:
        # --host without --user is rejected at parse time: implicit
        # peer-login-user defaults invite "where did this attach
        # actually go?" surprises.
        with self.assertRaises(SystemExit):
            uxon.parse_args(["attach", "demo@claude", "--host", "box-b"])

    def test_attach_dry_run(self) -> None:
        a = uxon.parse_args(
            ["attach", "demo@claude", "--host", "box-b", "--user", "alice", "--dry-run"]
        )
        self.assertTrue(a.dry_run)

    def test_attach_unknown_flag(self) -> None:
        with self.assertRaises(SystemExit):
            uxon.parse_args(["attach", "demo@claude", "--unknown"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_uxon_attach_multi.py::AttachParserTests -v`
Expected: 5 failures — current parser rejects any extras after target_id.

- [ ] **Step 3: Extend the attach parser**

In `src/uxon/cli.py`, locate the `parse_subcommand` block handling `cmd in ("attach", "kill")` (~line 2149). Replace the `attach` branch with a dedicated extras parser that mirrors `_parse_kill_extras` but without `--all-users`, `--json`, `--force`. Add a new helper `_parse_attach_extras` near `_parse_kill_extras`:

```python
def _parse_attach_extras(rest: list[str], target_id: str) -> ParsedArgs:
    dry = False
    user: str | None = None
    host: str | None = None
    extras: list[str] = []
    i = 0
    while i < len(rest):
        token = rest[i]
        if token == "--dry-run":
            dry = True
        elif token == "--user":
            i += 1
            if i >= len(rest):
                fail("--user requires a name")
            user = rest[i]
        elif token == "--host":
            i += 1
            if i >= len(rest):
                fail("--host requires a host name")
            host = rest[i]
        else:
            extras.append(token)
        i += 1
    if extras:
        fail(f"unknown args for attach: {' '.join(extras)}")
    if host is not None and user is None:
        fail("attach --host requires --user (peer owns authorisation; pass the target user explicitly)")
    return ParsedArgs(
        action="attach",
        target_id=target_id,
        dry_run=dry,
        user=user,
        host=host,
    )
```

Then in `parse_subcommand`, change the `attach`/`kill` branch:

```python
    if cmd in ("attach", "kill"):
        if len(argv) < 2:
            fail(f"{cmd} requires an identifier")
        target = argv[1]
        if cmd == "kill":
            return _parse_kill_extras(argv[2:], target)
        return _parse_attach_extras(argv[2:], target)
```

Also update the short form `-a` / `--attach` (~line 2185) to allow the same extras — same call to `_parse_attach_extras(argv[2:], argv[1])` instead of the current "no extras" rejection.

- [ ] **Step 4: Run parser tests**

Run: `python -m pytest tests/test_uxon_attach_multi.py::AttachParserTests -v`
Expected: 5 passing.

- [ ] **Step 5: Run full CLI test suite to catch regressions**

Run: `python -m pytest tests/test_uxon.py tests/test_uxon_json.py tests/test_uxon_kill_multi.py tests/test_uxon_attach_multi.py -v`
Expected: all passing.

- [ ] **Step 6: Commit**

```bash
git add src/uxon/cli.py tests/test_uxon_attach_multi.py
git commit -m "feat(cli): attach --host / --user / --dry-run parser

Symmetric to kill --host. --host requires --user (no implicit
peer-login-user default). Behaviour for plain 'uxon attach <id>'
unchanged."
```

---

## Task 5: Peer-side cross-user attach (sudo-gated)

**Files:**
- Modify: `src/uxon/cli.py` (`do_attach`, ~line 2216)
- Test: `tests/test_uxon_attach_multi.py` (add `AttachUserCrossUserTests`)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_uxon_attach_multi.py`:

```python
class AttachCrossUserTests(unittest.TestCase):
    """Peer-side ``uxon attach --user`` cross-user gating.

    Mirror of ``KillUserCrossUserTests`` in test_uxon_kill_multi.py.
    """

    def _cfg(self) -> uxon.Config:
        from tests.test_uxon_kill_multi import _make_config
        return _make_config()

    def test_same_user_no_sudo_path(self) -> None:
        cfg = self._cfg()
        args = uxon.ParsedArgs(action="attach", target_id="demo@claude", user="u-vz", dry_run=True)
        with mock.patch.object(uxon, "collect_sessions") as cs, \
             mock.patch.object(uxon, "resolve_session") as rs, \
             mock.patch.object(uxon, "attach_session", return_value=0) as att:
            cs.return_value = []
            rs.return_value = mock.Mock(name="demo@claude")
            rc = uxon.do_attach(args, cfg, "u-vz")
        self.assertEqual(rc, 0)
        # No probe needed when target == launch_user
        att.assert_called_once()

    def test_cross_user_unreachable_emits_stable_tag(self) -> None:
        cfg = self._cfg()
        args = uxon.ParsedArgs(action="attach", target_id="demo@claude", user="alice")
        from uxon.sudo_probe import SudoCapability
        caps = SudoCapability(reachable_users=frozenset(), can_root=False)
        buf = io.StringIO()
        with mock.patch("uxon.sudo_probe.probe_sudo_capability", return_value=caps), \
             redirect_stdout(buf), \
             self.assertRaises(SystemExit) as exc:
            with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                uxon.do_attach(args, cfg, "u-vz")
        # Stable error tag — aggregator's UI surfaces it via
        # pause_on_launch_failure.
        self.assertIn("uxon-error: not-reachable", err.getvalue())

    def test_cross_user_reachable_dry_run_shows_sudo_prefix(self) -> None:
        cfg = self._cfg()
        args = uxon.ParsedArgs(
            action="attach", target_id="demo@claude", user="alice", dry_run=True
        )
        from uxon.sudo_probe import SudoCapability
        caps = SudoCapability(reachable_users=frozenset({"alice"}), can_root=False)
        buf = io.StringIO()
        with mock.patch("uxon.sudo_probe.probe_sudo_capability", return_value=caps), \
             mock.patch.object(uxon, "collect_sessions", return_value=[]), \
             mock.patch.object(uxon, "resolve_session") as rs:
            rs.return_value = mock.Mock(name="demo@claude")
            rs.return_value.name = "demo@claude"
            with redirect_stdout(buf):
                rc = uxon.do_attach(args, cfg, "u-vz")
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("sudo", out)
        self.assertIn("alice", out)
        self.assertIn("tmux", out)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_uxon_attach_multi.py::AttachCrossUserTests -v`
Expected: 3 failures — current `do_attach` ignores `--user`.

- [ ] **Step 3: Extend `do_attach`**

In `src/uxon/cli.py`, replace the body of `do_attach` (~line 2216) with:

```python
def do_attach(args: ParsedArgs, cfg: Config, launch_user: str) -> int:
    if not args.target_id:
        fail("attach requires an identifier")

    # Remote dispatch: --host routes to a configured peer over SSH.
    # Per-target sudo gating happens on the peer (peer's own
    # 'uxon attach' runs the probe), so the local side does not need
    # to know the peer's user table. Mirrors do_kill --host.
    if args.host is not None:
        return _do_attach_remote(args, cfg)

    target_user = args.user or launch_user
    if target_user != launch_user:
        from uxon.sudo_probe import probe_sudo_capability

        caps = probe_sudo_capability([target_user])
        if target_user not in caps.reachable_users:
            eprint(
                f"uxon-error: not-reachable (cannot sudo -niu {target_user}; "
                "check /etc/sudoers.d for a NOPASSWD rule for this target)"
            )
            return 1
        sessions = collect_sessions([target_user], cfg)
        target = resolve_session(
            args.target_id,
            sessions,
            cfg.session_prefix,
            legacy_prefixes=cfg.legacy_session_prefixes,
        )
        base = configured_tmux_base(cfg, target_user) + ["attach-session", "-t", target.name]
        full = ["sudo", "-niu", target_user, "--", *base]
        if args.dry_run:
            print(f"attach_user={shlex.quote(target_user)}")
            print(f"socket={shlex.quote(tmux_socket_path(cfg, target_user))}")
            print(f"session={shlex.quote(target.name)}")
            print(f"exec {shlex.join(full)}")
            return 0
        os.execvp(full[0], full)
        return 0

    # Same-user path — unchanged.
    sessions = collect_sessions([launch_user], cfg)
    if not sessions:
        legacy = collect_sessions_for_user(
            launch_user,
            cfg.session_prefix,
            socket_path=None,
            legacy_prefixes=cfg.legacy_session_prefixes,
        )
        if legacy:
            fail(
                f"no sessions found on dedicated socket {tmux_socket_path(cfg, launch_user)}, "
                f"but legacy default-socket sessions still exist. Use 'uxon doctor' for details."
            )
    target = resolve_session(
        args.target_id, sessions, cfg.session_prefix, legacy_prefixes=cfg.legacy_session_prefixes
    )
    return attach_session(target, cfg, launch_user, args.dry_run)
```

(`_do_attach_remote` is added in Task 7. For now, leave a forward-declared stub at module top level so `do_attach` imports cleanly:

```python
def _do_attach_remote(args: ParsedArgs, cfg: Config) -> int:
    fail("not yet implemented (Task 7)")
    return 1
```

The same-user same-launch_user path stays an `os.execvp`. The cross-user path also `os.execvp`s the sudo+tmux argv — once handed off to sudo+tmux, the uxon process is gone; we trust peer's existing tmux nesting check to be irrelevant on a fresh ssh session.)

- [ ] **Step 4: Run cross-user tests**

Run: `python -m pytest tests/test_uxon_attach_multi.py -v`
Expected: parser tests still passing; 3 new cross-user tests passing.

- [ ] **Step 5: Run local same-user attach regression tests**

Run: `python -m pytest tests/test_uxon.py -k attach -v`
Expected: existing same-user attach tests pass unchanged.

- [ ] **Step 6: Commit**

```bash
git add src/uxon/cli.py tests/test_uxon_attach_multi.py
git commit -m "feat(cli): peer-side 'uxon attach --user' cross-user gating

Mirror of do_kill cross-user logic. Probes per-target sudo;
emits stable 'uxon-error: not-reachable' tag on miss. On success
execvp's sudo -niu <user> tmux ... attach-session.
Same-user path unchanged. _do_attach_remote stub fails until
Task 7."
```

---

## Task 6: Aggregator-side `_do_attach_remote`

**Files:**
- Modify: `src/uxon/cli.py` (replace `_do_attach_remote` stub with real impl)
- Test: `tests/test_uxon_attach_multi.py` (add `AttachHostRemoteTests`)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_uxon_attach_multi.py`:

```python
class AttachHostRemoteTests(unittest.TestCase):
    """``uxon attach --host <alias> --user <u>`` SSH-routed dispatch."""

    def _cfg_with_host(self, **host_kwargs) -> uxon.Config:
        from tests.test_uxon_kill_multi import _make_config
        return _make_config(
            remote_hosts=[
                RemoteHost(
                    name="box-b",
                    ssh_alias="ssh-b",
                    description="",
                    remote_uxon="uxon",
                    **host_kwargs,
                )
            ]
        )

    def test_host_dry_run_prints_ssh_attach_command(self) -> None:
        cfg = self._cfg_with_host()
        args = uxon.ParsedArgs(
            action="attach",
            target_id="demo@claude",
            host="box-b",
            user="alice",
            dry_run=True,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = uxon.do_attach(args, cfg, "u-vz")
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("ssh", out)
        self.assertIn("-tt", out)  # interactive PTY
        self.assertIn("ssh-b", out)
        self.assertIn("uxon attach", out)
        self.assertIn("--user", out)
        self.assertIn("alice", out)
        self.assertIn("demo@claude", out)

    def test_host_honours_command_template(self) -> None:
        cfg = self._cfg_with_host(
            command_template=("ssh", "-J", "bastion", "{ssh_alias}", "{remote_command}"),
        )
        args = uxon.ParsedArgs(
            action="attach",
            target_id="demo@claude",
            host="box-b",
            user="alice",
            dry_run=True,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            uxon.do_attach(args, cfg, "u-vz")
        out = buf.getvalue()
        self.assertIn("-J", out)
        self.assertIn("bastion", out)
        # -tt still injected after the outermost ssh.
        first_ssh = out.find("ssh")
        first_tt = out.find("-tt")
        self.assertGreater(first_tt, first_ssh)

    def test_host_unknown_alias_fails(self) -> None:
        cfg = self._cfg_with_host()
        args = uxon.ParsedArgs(
            action="attach",
            target_id="demo@claude",
            host="unknown",
            user="alice",
        )
        with self.assertRaises(SystemExit):
            uxon.do_attach(args, cfg, "u-vz")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_uxon_attach_multi.py::AttachHostRemoteTests -v`
Expected: 3 failures — `_do_attach_remote` is still the stub.

- [ ] **Step 3: Implement `_do_attach_remote`**

In `src/uxon/cli.py`, replace the stub `_do_attach_remote` with:

```python
def _do_attach_remote(args: ParsedArgs, cfg: Config) -> int:
    """Handle ``uxon attach <id> --host <alias> --user <u>``.

    Looks up the configured peer, builds an interactive ssh argv via
    :func:`build_peer_ssh_argv`, and execvp's it. Peer's own
    ``uxon attach --user`` runs the per-target sudo probe, so the
    local side does not need to know the peer's user table.

    The wire command always passes ``--user`` (even when it equals
    the ssh-login-user on the peer): peer is the sole authority on
    'who can attach to what', and we route that decision through
    its own gating. ``--user`` was made required at parse time
    (:func:`_parse_attach_extras`).
    """
    from uxon.remote_collector import (
        DEFAULT_CONNECT_TIMEOUT_SEC,
        build_peer_ssh_argv,
    )
    from uxon.remote_hosts import find_host

    peer = find_host(cfg.remote_hosts, args.host or "")
    if peer is None:
        names = ", ".join(h.name for h in cfg.remote_hosts) or "(none)"
        fail(f"unknown --host {args.host!r}; configured: {names}")
    assert args.user is not None  # parser-enforced
    remote_cmd = (
        f"{shlex.quote(peer.remote_uxon)} attach "
        f"--user {shlex.quote(args.user)} {shlex.quote(args.target_id or '')}"
    )
    ssh_argv = build_peer_ssh_argv(
        peer,
        remote_command=remote_cmd,
        allocate_tty=True,
        connect_timeout=DEFAULT_CONNECT_TIMEOUT_SEC,
        ssh_multiplex=cfg.ssh_multiplex,
    )
    if args.dry_run:
        print(shlex.join(ssh_argv))
        return 0
    os.execvp(ssh_argv[0], ssh_argv)
    return 0  # unreachable
```

- [ ] **Step 4: Run remote tests**

Run: `python -m pytest tests/test_uxon_attach_multi.py -v`
Expected: all passing.

- [ ] **Step 5: Commit**

```bash
git add src/uxon/cli.py tests/test_uxon_attach_multi.py
git commit -m "feat(cli): _do_attach_remote dispatches via build_peer_ssh_argv

uxon attach <id> --host <alias> --user <u> renders peer command
'uxon attach --user <u> <id>' and execvp's an interactive ssh
(-tt). Honours peer's command_template / extra_ssh_options.
Reuses warm ControlMaster from the poller — channel-open in
warm case is sub-50ms."
```

---

## Task 7: TUI `MainIntent.host` + `remote_session_intent` factory

**Files:**
- Modify: `src/uxon/tui/state.py` (`MainIntent` dataclass + new factory)
- Test: `tests/test_uxon_tui.py` (new test class)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_uxon_tui.py` after `MainScreenIntentStateTests`:

```python
class RemoteSessionIntentTests(unittest.TestCase):
    def test_basic(self) -> None:
        from uxon.tui.state import remote_session_intent, MainIntent

        intent = remote_session_intent(
            "vz-prod1",
            {"user": "alice", "name": "demo@claude", "short_id": "abc"},
            current_user="vasily",
        )
        self.assertEqual(
            intent,
            MainIntent(
                kind="attach-remote",
                host="vz-prod1",
                user="alice",
                session_name="demo@claude",
            ),
        )

    def test_strips_own_only_suffix(self) -> None:
        from uxon.tui.state import remote_session_intent

        intent = remote_session_intent(
            "vz-prod1 (own only)",
            {"user": "alice", "name": "x"},
            current_user="vasily",
        )
        self.assertEqual(intent.host, "vz-prod1")

    def test_falls_back_to_current_user_when_record_missing_user(self) -> None:
        from uxon.tui.state import remote_session_intent

        intent = remote_session_intent(
            "vz-prod1",
            {"name": "x"},
            current_user="vasily",
        )
        self.assertEqual(intent.user, "vasily")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_uxon_tui.py::RemoteSessionIntentTests -v`
Expected: 3 errors — `remote_session_intent` not importable.

- [ ] **Step 3: Extend `MainIntent` and add factory**

In `src/uxon/tui/state.py`, change `MainIntent`:

```python
@dataclass(frozen=True)
class MainIntent:
    kind: str
    index: int | None = None
    user: str = ""
    session_name: str = ""
    host: str = ""
```

Add factory immediately after `session_intent`:

```python
def remote_session_intent(
    host_name: str,
    rec: dict,
    current_user: str,
) -> MainIntent:
    """Build a MainIntent for activating one row of the RemoteSessionTable.

    ``host_name`` is the display name as carried on the row tuple —
    may include a trailing " (own only)" suffix; we strip it.
    ``rec`` is the wire-schema session record (dict). Falls back to
    ``current_user`` when the record lacks a ``user`` field.
    """
    bare_host = host_name.split(" ", 1)[0]
    return MainIntent(
        kind="attach-remote",
        host=bare_host,
        user=str(rec.get("user") or current_user),
        session_name=str(rec.get("name") or ""),
    )
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_uxon_tui.py::RemoteSessionIntentTests -v`
Expected: 3 passing.

- [ ] **Step 5: Run all TUI state tests**

Run: `python -m pytest tests/test_uxon_tui.py -v`
Expected: all passing — `host` field defaults to "" so existing intents are unaffected.

- [ ] **Step 6: Commit**

```bash
git add src/uxon/tui/state.py tests/test_uxon_tui.py
git commit -m "feat(tui/state): MainIntent.host + remote_session_intent factory

New kind 'attach-remote' for activating a remote-row. Pure-data
factory; testable without Textual."
```

---

## Task 8: TUI `on_remote_attach` callback wiring

**Files:**
- Modify: `src/uxon/tui/context.py` (add field, ~near `on_remote_kill` line 203)
- Modify: `src/uxon/tui/config.py` (add field, ~lines 90-145)
- Test: `tests/test_uxon_tui_config.py` (extend an existing passthrough test)

- [ ] **Step 1: Write failing test**

In `tests/test_uxon_tui_config.py`, locate the test that asserts `cfg.on_remote_kill is ctx.on_remote_kill` (around line 110) and add a sibling immediately after it:

```python
    def test_on_remote_attach_propagated(self) -> None:
        from uxon.tui.config import TuiConfig
        from uxon.tui.context import LaunchRequest

        attach_calls: list[tuple[str, str, str]] = []

        def fake_attach(host: str, user: str, name: str) -> LaunchRequest:
            attach_calls.append((host, user, name))
            return LaunchRequest(cmd=("true",), label="t")

        ctx = self._minimal_ctx(on_remote_attach=fake_attach)
        cfg = TuiConfig.from_context(ctx)
        self.assertIs(cfg.on_remote_attach, ctx.on_remote_attach)
        cfg.on_remote_attach("h", "u", "n")
        self.assertEqual(attach_calls, [("h", "u", "n")])
```

(If `_minimal_ctx` doesn't already accept `on_remote_attach` — check the helper in this test file — extend its signature symmetrically with `on_remote_kill`.)

- [ ] **Step 2: Run test**

Run: `python -m pytest tests/test_uxon_tui_config.py -v`
Expected: failure — `on_remote_attach` not a field on `TuiContext` or `TuiConfig`.

- [ ] **Step 3: Add field to `TuiContext`**

In `src/uxon/tui/context.py`, immediately after `on_remote_kill` (~line 203-205), add:

```python
    # Multi-host per-session attach (parallel to on_remote_kill).
    # Args: (host_name, user, session). Implementation builds an
    # interactive ssh LaunchRequest via build_peer_ssh_argv; the TUI
    # hands it to request_launch (fork-and-wait, returns to TUI on
    # tmux detach).
    on_remote_attach: Callable[[str, str, str], LaunchRequest] = (
        lambda host, user, name: LaunchRequest(cmd=("true",), label="noop-remote-attach")
    )
```

- [ ] **Step 4: Add field to `TuiConfig`**

In `src/uxon/tui/config.py`, in the `TuiConfig` dataclass (~line 90), add immediately after `on_remote_kill`:

```python
    on_remote_attach: Callable[[str, str, str], LaunchRequest]
```

In `TuiConfig.from_context` (~line 144), add immediately after `on_remote_kill=ctx.on_remote_kill,`:

```python
            on_remote_attach=ctx.on_remote_attach,
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_uxon_tui_config.py -v`
Expected: passing.

- [ ] **Step 6: Commit**

```bash
git add src/uxon/tui/context.py src/uxon/tui/config.py tests/test_uxon_tui_config.py
git commit -m "feat(tui): on_remote_attach callback field

Symmetric to on_remote_kill. Default is a no-op LaunchRequest;
real implementation wired in cli.py in a follow-up commit."
```

---

## Task 9: TUI screen handler — Enter on remote row

**Files:**
- Modify: `src/uxon/tui/screens/main.py` (`on_data_table_row_selected` ~line 453, `_run_intent` ~line 463, new `_attach_remote_session` method)
- Test: `tests/test_uxon_tui_remote.py` (new test class)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_uxon_tui_remote.py`:

```python
class RemoteRowActivationTests(unittest.TestCase):
    """Enter on a RemoteSessionTable row dispatches on_remote_attach.

    Pure-state test — drives MainScreen._run_intent with a synthesised
    intent and asserts the callback was invoked with the right
    (host, user, name) triple. No Textual app loop required.
    """

    def test_run_intent_attach_remote_calls_callback(self) -> None:
        from uxon.tui.screens.main import MainScreen
        from uxon.tui.context import LaunchRequest
        from uxon.tui.state import MainIntent

        attach_calls: list[tuple[str, str, str]] = []

        def fake_attach(host: str, user: str, name: str) -> LaunchRequest:
            attach_calls.append((host, user, name))
            return LaunchRequest(cmd=("true",), label="t")

        # Reuse the test harness from this file's existing
        # action_kill_remote tests (search for `on_remote_kill=lambda...`)
        # to build a MainScreen wired with a recording on_remote_attach.
        screen = _make_screen_for_remote_test(on_remote_attach=fake_attach)
        # Stub request_launch to capture the LaunchRequest without
        # actually exiting the app.
        captured: list[LaunchRequest] = []
        screen.app.request_launch = captured.append  # type: ignore[assignment]

        intent = MainIntent(
            kind="attach-remote",
            host="vz-prod1",
            user="alice",
            session_name="demo@claude",
        )
        screen._run_intent(intent)

        self.assertEqual(attach_calls, [("vz-prod1", "alice", "demo@claude")])
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].label, "t")
```

You will need a `_make_screen_for_remote_test` helper in this test file. Check the existing `test_action_kill_remote_dispatches_on_confirm` (around line 357) for the screen-construction pattern; lift that into a helper at the top of the test class:

```python
def _make_screen_for_remote_test(*, on_remote_attach=None, on_remote_kill=None):
    """Build a MainScreen wired with the given remote callbacks.

    Lifted from the action_kill_remote test setup; one place to
    parameterise so both kill-remote and attach-remote tests share
    the construction.
    """
    # ... (use the same TuiContext / app construction the existing
    # tests use; default on_remote_attach to a no-op if not provided.)
```

(Mechanical lift — don't reinvent. If the helper would require pulling in too much app machinery, write the test inline by copying the existing kill-remote test setup.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_uxon_tui_remote.py::RemoteRowActivationTests -v`
Expected: failures — neither `_run_intent` for `attach-remote` nor the helper exists.

- [ ] **Step 3: Add the screen handler branches**

In `src/uxon/tui/screens/main.py`:

(a) In `on_data_table_row_selected` (~line 453), replace:

```python
    def on_data_table_row_selected(self, event) -> None:  # type: ignore[no-untyped-def]
        """Enter/click on a session row attaches to that session."""
        table = event.data_table
        if not isinstance(table, SessionTable):
            return
        session = table.session_at(event.cursor_row)
        if session is None:
            return
        self._run_intent(session_intent(session, self.ctx.current_user))
```

with:

```python
    def on_data_table_row_selected(self, event) -> None:  # type: ignore[no-untyped-def]
        """Enter/click on a session row attaches to that session.

        Local SessionTable rows fire the existing session_intent path.
        RemoteSessionTable rows fire the remote_session_intent path,
        which dispatches via ctx.on_remote_attach over SSH.
        """
        table = event.data_table
        if isinstance(table, SessionTable):
            session = table.session_at(event.cursor_row)
            if session is None:
                return
            self._run_intent(session_intent(session, self.ctx.current_user))
            return
        if isinstance(table, RemoteSessionTable):
            entry = table.row_at(event.cursor_row)
            if entry is None:
                return
            host_name, rec = entry
            self._run_intent(
                remote_session_intent(host_name, rec, self.ctx.current_user)
            )
            return
```

Add the import of `remote_session_intent` near the top of `main.py` next to `session_intent`.

(b) In `_run_intent` (~line 463), add a new branch immediately after the `attach` branch:

```python
        elif intent.kind == "attach-remote":
            self._attach_remote_session(intent.host, intent.user, intent.session_name)
```

(c) Add the new method after `_attach_session` (~line 489):

```python
    def _attach_remote_session(self, host: str, user: str, name: str) -> None:
        """TUI dispatch: attach to ``name`` belonging to ``user`` on peer ``host``.

        Mirrors :meth:`_attach_session` (local). Calls
        ``ctx.on_remote_attach`` to obtain a LaunchRequest carrying
        the ssh+remote-uxon argv, then hands it to
        ``app.request_launch`` (fork-and-wait, re-enters TUI on
        tmux detach). Failures from the callback surface as red
        toasts via ``CallbackError``; ssh-time failures surface
        through ``pause_on_launch_failure`` after fork-and-wait.
        """
        try:
            req = self.ctx.on_remote_attach(host, user, name)
        except CallbackError as exc:
            self.app.notify(
                f"Remote attach failed: {exc}", severity="error", timeout=6
            )
            return
        self.app.request_launch(req)  # type: ignore[attr-defined]
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_uxon_tui_remote.py::RemoteRowActivationTests -v`
Expected: passing.

- [ ] **Step 5: Run full TUI remote test suite**

Run: `python -m pytest tests/test_uxon_tui_remote.py tests/test_uxon_tui.py -v`
Expected: all passing — kill-remote tests untouched.

- [ ] **Step 6: Commit**

```bash
git add src/uxon/tui/screens/main.py tests/test_uxon_tui_remote.py
git commit -m "feat(tui/screens/main): wire Enter on RemoteSessionTable

Branch in on_data_table_row_selected dispatches
remote_session_intent. _run_intent gains 'attach-remote' branch.
_attach_remote_session calls ctx.on_remote_attach and hands the
LaunchRequest to request_launch (fork-and-wait)."
```

---

## Task 10: CLI `on_remote_attach` callback implementation

**Files:**
- Modify: `src/uxon/cli.py` (define `on_remote_attach` near `on_remote_kill` ~line 3703; pass to `tui_context_from_cli`)
- Test: `tests/test_uxon_attach_multi.py` (new class testing the LaunchRequest shape)

- [ ] **Step 1: Write failing test**

Append to `tests/test_uxon_attach_multi.py`:

```python
class OnRemoteAttachCallbackTests(unittest.TestCase):
    """The TUI-side on_remote_attach callback builds the right LaunchRequest."""

    def test_builds_interactive_ssh_launch_request(self) -> None:
        from tests.test_uxon_kill_multi import _make_config

        cfg = _make_config(
            remote_hosts=[
                RemoteHost(
                    name="box-b",
                    ssh_alias="ssh-b",
                    description="",
                    remote_uxon="uxon",
                )
            ]
        )
        cb = uxon._make_on_remote_attack_for_tests  # type: ignore[attr-defined]
        # Builders are constructed inside tui_context_from_cli; expose
        # a thin factory for tests.
        callback = uxon._build_on_remote_attach_callback(cfg)
        req = callback("box-b", "alice", "demo@claude")
        argv = list(req.cmd)
        self.assertEqual(argv[0], "ssh")
        self.assertIn("-tt", argv)
        self.assertIn("ssh-b", argv)
        # Remote command should carry --user alice and the target id.
        remote_cmd = argv[-1]
        self.assertIn("uxon attach", remote_cmd)
        self.assertIn("--user", remote_cmd)
        self.assertIn("alice", remote_cmd)
        self.assertIn("demo@claude", remote_cmd)
        # Label is descriptive for pause_on_launch_failure.
        self.assertIn("attach", req.label)
        self.assertIn("box-b", req.label)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_uxon_attach_multi.py::OnRemoteAttachCallbackTests -v`
Expected: failure — `_build_on_remote_attach_callback` doesn't exist.

- [ ] **Step 3: Add the callback factory**

In `src/uxon/cli.py`, near `on_remote_kill` (search for `def on_remote_kill(host_name`), add a module-level factory and use it from `tui_context_from_cli`:

```python
def _build_on_remote_attach_callback(cfg):
    """Return the TUI on_remote_attach callback for the given cfg.

    Pulled out as a module-level factory so tests can construct it
    with a synthetic Config without spinning up the full
    tui_context_from_cli closure.
    """
    from uxon.remote_collector import (
        DEFAULT_CONNECT_TIMEOUT_SEC,
        build_peer_ssh_argv,
    )
    from uxon.remote_hosts import find_host
    from uxon.tui.context import LaunchRequest

    def on_remote_attach(host_name: str, user: str, name: str) -> LaunchRequest:
        peer = find_host(cfg.remote_hosts, host_name)
        if peer is None:
            from uxon.tui.context import CallbackError
            raise CallbackError(f"unknown remote host: {host_name}")
        remote_cmd = (
            f"{shlex.quote(peer.remote_uxon)} attach "
            f"--user {shlex.quote(user)} {shlex.quote(name)}"
        )
        argv = build_peer_ssh_argv(
            peer,
            remote_command=remote_cmd,
            allocate_tty=True,
            connect_timeout=DEFAULT_CONNECT_TIMEOUT_SEC,
            ssh_multiplex=cfg.ssh_multiplex,
        )
        return LaunchRequest(cmd=tuple(argv), label=f"attach {name}@{host_name}")

    return on_remote_attach
```

In `tui_context_from_cli` (search for `on_remote_kill = _wrap_tui_callback`), wire the callback:

```python
    on_remote_attach = _build_on_remote_attach_callback(cfg)
```

And pass it to the `TuiContext(...)` constructor immediately after `on_remote_kill=on_remote_kill,`:

```python
        on_remote_attach=on_remote_attach,
```

(The `_wrap_tui_callback` shim is for callbacks that may `fail()` / SystemExit. Our `on_remote_attach` already raises `CallbackError` directly, so wrapping is unnecessary.)

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_uxon_attach_multi.py::OnRemoteAttachCallbackTests -v`
Expected: passing.

- [ ] **Step 5: Run full attach + remote suites**

Run: `python -m pytest tests/test_uxon_attach_multi.py tests/test_uxon_tui_remote.py tests/test_uxon_tui_config.py -v`
Expected: all passing.

- [ ] **Step 6: Commit**

```bash
git add src/uxon/cli.py tests/test_uxon_attach_multi.py
git commit -m "feat(cli): _build_on_remote_attach_callback for the TUI

Renders peer command 'uxon attach --user <u> <id>' and packages
an interactive ssh argv (-tt) into a LaunchRequest. Wired into
tui_context_from_cli so Enter on a remote row in the TUI drives
the same path as 'uxon attach <id> --host <h> --user <u>'."
```

---

## Task 11: Update `RemoteSessionTable` docstring

**Files:**
- Modify: `src/uxon/tui/widgets/remote_session_table.py` (docstring)

- [ ] **Step 1: Update the docstring**

In `src/uxon/tui/widgets/remote_session_table.py`, replace:

```python
This widget is read-only: it does not drive attach/kill — those
need a remote SSH gesture not yet wired (deferred to a later
commit). For now the rows surface the data; activation is a no-op.
```

with:

```python
This widget surfaces wire-schema rows; activation is driven by
MainScreen.on_data_table_row_selected (Enter → remote attach
via ctx.on_remote_attach over SSH) and MainScreen.action_kill_remote
(``k`` → remote kill via ctx.on_remote_kill over SSH).
```

Also update the inline comment near `_row_index` (around line 68-70):

```python
        # Each row in the table maps back to a (host_name, record) tuple
        # so a future remote-attach handler can identify what was clicked.
```

to:

```python
        # Each row in the table maps back to a (host_name, record) tuple;
        # the on_data_table_row_selected handler reads it via row_at().
```

- [ ] **Step 2: Run nothing — docstring only**

(No tests pin the docstring.)

- [ ] **Step 3: Commit**

```bash
git add src/uxon/tui/widgets/remote_session_table.py
git commit -m "docs(tui): RemoteSessionTable activation is now wired"
```

---

## Task 12: End-to-end smoke (manual)

**Files:** none — manual verification on a multi-host setup.

- [ ] **Step 1: Set up two hosts with `[[remote_hosts]]` config pointing each at the other.**

(Spec assumes the developer has a multi-host fixture; the existing `docs/configuration.md` § Multi-host is the reference.)

- [ ] **Step 2: Start a session on peer**

```bash
ssh peer-b uxon new smoke-attach --no-launch  # or whatever creates a tmux session
```

- [ ] **Step 3: From host A, dry-run the CLI path**

```bash
uxon attach smoke-attach --host peer-b --user $(whoami) --dry-run
```

Expected stdout: a `ssh -tt … peer-b 'uxon attach --user … smoke-attach'` argv.

- [ ] **Step 4: Run uxon TUI on host A and Enter on the remote row**

Expected: tmux client appears for the smoke-attach session. `Ctrl-B d` detaches. Control returns to TUI, focus is back on the remote row.

- [ ] **Step 5: Check warm-master timing**

After the TUI has been running for ~10s (one poll cycle), repeat the Enter. The connect should be visually instant (sub-50ms). To confirm, look at `~/.cache/uxon/ssh-*` — there should be a unix socket file for the peer.

- [ ] **Step 6: Cross-user smoke (only if a second peer-side user has NOPASSWD sudo set up)**

Repeat steps 2-4 with `--user <other>`. Expected: peer's `do_attach` probes sudo, sudoes into `<other>`, attaches to their tmux. On a host where `<other>` is NOT reachable via NOPASSWD, expect a `uxon-error: not-reachable` message in the terminal.

- [ ] **Step 7: No commit needed — manual verification only.**

---

## Self-review (run by the plan-writer, fix inline)

Coverage check (each spec section maps to a task):

- ✅ Goal (TUI Enter + CLI `attach --host`) → Tasks 4-6, 8-10
- ✅ Cross-user from v1 → Task 5
- ✅ One ssh-argv builder → Tasks 1-3
- ✅ Bug fix on `_do_kill_remote` → Task 3
- ✅ MainIntent extension → Task 7
- ✅ on_remote_attach callback → Tasks 8, 10
- ✅ TUI screen handler → Task 9
- ✅ Latency property (warm master) → Task 12 step 5
- ✅ Error handling (peer unreachable, sudo not granted, old peer) → Task 5 stable tag, Task 6 unknown alias, Task 12 manual
- ✅ Testing strategy → Tasks 1-10 each ship tests; Task 12 manual
- ✅ Docstring update → Task 11
- ✅ CLI `--dry-run` shape → Tasks 4 parser, 6 dry-run output

Type/signature consistency check:

- `build_peer_ssh_argv(host, *, remote_command, allocate_tty, connect_timeout, ssh_multiplex)` — used identically in Tasks 1, 2, 3, 6, 10. ✓
- `MainIntent(kind, index, user, session_name, host)` — `host` defaults to "" so existing intents (kind="attach", "launch-cwd", etc.) aren't broken. ✓
- `on_remote_attach(host, user, name) -> LaunchRequest` — same signature in context.py field default, config.py field, cli.py factory, screen handler call. ✓

Placeholder scan: no TBD / TODO / "implement later" / "similar to Task N" in any step. All test code complete; all command lines concrete; all expected outputs stated.

Known plan-time uncertainty (not a placeholder):

- Task 9 mentions `_make_screen_for_remote_test` helper that "should be liftable from the existing kill-remote test setup". Implementor may discover the lift is non-trivial; the fallback (inline test) is documented. Not a placeholder — it's an explicit choice point with both branches specified.
- Task 8's `_minimal_ctx` helper in `test_uxon_tui_config.py` may or may not already accept arbitrary callback overrides; the plan says "extend symmetrically with on_remote_kill" — implementor should verify the existing helper shape. Not a blocker; one-line extension if needed.

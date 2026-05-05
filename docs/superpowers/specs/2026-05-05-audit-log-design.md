# Audit log — design

## Goal

Per-host application-level audit trail of who used `uxon`, when, and
to what end. Answers "kto когда куда подключился", "кто сделал
kill-all", "кто открывал supervision-UI", "какие сессии запускались
и сколько прожили". Single channel, structured, written to the
platform log (systemd journal natively when available, syslog
fallback). Designed so it does **not** measurably slow the tool.

## Non-goals

- Tamper-resistance via runtime self-checks. Code-tamper boundary is
  the OS install permissions; log-tamper boundary is journald/syslog.
  Application-level integrity tricks (`-I` shebang, marker files,
  `__file__.startswith`, `audit.tampered` events) — out.
- Rotation, delivery, shipping. logrotate / journald / journal-upload
  / rsyslog / Vector own that.
- Cryptographic signing or hash-chains. Compliance-territory; README's
  Boundaries section explicitly disclaims "audit infrastructure".
- Cost/token accounting. Disclaimed in README.
- Defending against direct invocation of `claude` / `tmux` outside
  `uxon`. Operator's responsibility (PATH, package install policy,
  sudoers).
- Cross-host aggregation on uxon's side. Each peer logs locally;
  fan-in is the operator's log-shipping concern.

## Threat model and security boundary

`uxon` runs on shared Linux hosts where developers `sudo -iu
<user>_agent`-into low-privilege launch users. The audit channel
records what those launch users did **via uxon**. Boundary:

- **Closed**: tampering with past log records (journald/syslog files
  are root-owned; the launch-user process can append, never edit).
- **Closed (operator-mode)**: tampering with `uxon` code — when
  installed via `sudo install_uxon.py` into `/opt/uxon/venv`, the
  package files are root-owned and not writable by launch users.
- **Not engineered around**: a launch user with shell access running
  their own `uxon` copy. We do not insert runtime self-checks. The
  operationally-meaningful privileged operations (`sudo -iu …`) appear
  in `sudo`'s own audit trail (`auth.log` / journald), which is the
  source of truth for who-did-what at the OS level. uxon's audit is
  application-level **value-add** (which session, which agent, which
  project, correlation across hosts) — not a privilege ledger.

## Architecture

One module: `src/uxon/audit.py`. One public entrypoint:

```python
def audit(event: str, *, outcome: str = "ok", **fields: Any) -> None
```

Never raises. Best-effort: any sink failure is swallowed.

### Adaptive sink (decided once, cached for process lifetime)

At first `audit()` call, pick exactly one sink:

1. `/run/systemd/journal/socket` exists and is `AF_UNIX` → **native
   journal protocol**. Wire format: `KEY=value\n` repeated, datagram
   on `SOCK_DGRAM`. Documented in `man sd_journal_send` and
   `man systemd.journal-fields`. No `python-systemd` dependency:
   ~30 lines of stdlib. Native fields land first-class in journald
   and are queryable via `journalctl FIELD=value`.
2. Else `/dev/log` exists → **syslog RFC 5424 + CEE-JSON** body
   (`@cee: {"v":1,...}`). Goes through any syslog daemon; on systemd
   hosts that's still journald via `systemd-journald-dev-log.socket`.
3. Else → **no-op**. Audit silently disabled (no demos, no CI noise).

Detection runs once at first call; the chosen socket is held open.
No re-detection if the socket later breaks (we don't reconnect — drop
events on `EAGAIN`/`ENOBUFS`/`EPIPE`).

### Hot path

```
1. enabled (bool)?         ~0.1µs
2. merge cached prefix +
   per-call fields:        ~3µs
3. serialize:              ~5–10µs
4. socket.send (NONBLOCK): ~10µs
                           -------
                           ~15–25µs / event
```

Optimizations baked in:

- **Lazy connect once.** Open socket on first audit; reuse forever.
- **Cached caller-context prefix.** `pwd.getpwuid()`,
  `socket.gethostname()`, env reads, `__version__` — computed once
  and held as a pre-built dict (native sink) or pre-serialized
  bytes prefix (syslog sink).
- **`SOCK_NONBLOCK | SOCK_CLOEXEC`.** Audit never blocks the agent
  launch path. On `BlockingIOError`/`OSError` we drop and continue.
- **Bypass stdlib `logging` framework.** Direct `socket.send`. No
  `LogRecord`, no `Formatter`, no `Handler` chain. Saves ~50µs per
  call vs `SysLogHandler`.
- **No background thread, no in-process queue, no batching.** journald
  itself is the asynchronous demultiplexer; we hand off to the kernel
  and return. Durability matters: a crash mid-launch must not lose the
  preceding `session.new` event.
- **stdlib only.** No `orjson`, no `python-systemd`. (`json.dumps` on
  10 fields ≈10µs — not worth a dependency.)

### Disable kill-switch

Single config knob in `config.toml`:

```toml
[audit]
enabled = true              # default
syslog_facility = "user"    # only consulted on /dev/log fallback
```

Resolution:

- Default `enabled = true` for both solo and shared-host installs.
  On a solo machine the records land in the user's own journal
  (queryable via `journalctl --user-unit` or `_UID=$(id -u)
  SYSLOG_IDENTIFIER=uxon`); harmless and useful as personal history.
- `syslog_facility` is consulted only when the syslog fallback is
  active. journald native protocol uses its own metadata fields, not
  syslog facility.
- **No environment-variable override.** The whole point in shared-host
  mode is that the launch user can't disable audit by exporting a
  variable. Configuration lives in `config.toml`, which on a
  shared-host install is in `/etc/uxon/config.toml` (root-owned).

## Caller-context prefix (every event)

Computed once per process at first `audit()` call:

| Field            | Source                                                         |
|------------------|----------------------------------------------------------------|
| `host`           | `socket.gethostname()`                                         |
| `uxon_version`   | `uxon.__version__`                                             |
| `caller_user`    | `os.environ.get("SUDO_USER")` or `os.environ.get("USER")`      |
| `caller_uid`     | `int(os.environ.get("SUDO_UID"))` or `os.getuid()` (real UID)  |
| `launch_user`    | `pwd.getpwuid(os.geteuid()).pw_name`                           |
| `pid`, `ppid`    | `os.getpid()`, `os.getppid()`                                  |
| `ssh_client`     | `os.environ.get("SSH_CONNECTION")` if present, else omitted    |
| `subcmd`         | first positional CLI arg (`attach` / `kill` / `new` / …)       |

Inbound detection: presence of `SSH_CONNECTION` in environment +
`subcmd ∈ {list, attach, kill}` is what flips a local event into its
`*.remote.in` form. No "remote-mode" flag — the env var is the
ground truth.

Native-journal additionally carries fields journald stamps for free
(`_PID`, `_UID`, `_AUDIT_LOGINUID`, `_CMDLINE`, `_HOSTNAME`,
`_SYSTEMD_UNIT`); we don't duplicate them.

## Event alphabet (v=1)

Common envelope on every event:

```json
{
  "v": 1,
  "event": "session.attach",
  "outcome": "ok",
  "ts": "2026-05-05T10:11:12.345Z",
  "host": "vz-prod1",
  "uxon_version": "4.0.0",
  "caller_user": "lead",
  "caller_uid": 1004,
  "launch_user": "alice_agent",
  "pid": 14077,
  "ppid": 14076,
  "subcmd": "attach",
  "ssh_client": "10.0.0.7 51234 192.168.1.5 22",
  "session": "uxon-projectX",
  "target_user": "alice_agent",
  "correlation_id": "8f3c…",
  "dry_run": false,
  "rc": 0
}
```

`outcome ∈ {"ok", "denied", "error", "not_found"}`. State-changing
events emit on **both** the success and failure paths — a refused
attach is more interesting to an auditor than a successful one.

| `event`                | When                                                                                                  | Extra fields beyond envelope                                                                                                  |
|------------------------|-------------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------|
| `cli.start`            | top of `main()` after argv parse, skipping pure `--help`/`--version`                                  | `flags` (sanitised list), `agents_enabled` (list), `enable_all_users_list` (bool), `audit_enabled` (bool, always `true` here, used by readers as a continuity marker), `allowed_roots_count` (int), `remote_hosts_count` (int) |
| `tui.open`             | `tui/app.py::run` start                                                                                | (envelope only)                                                                                                              |
| `session.new`          | `do_new` / `do_run` create+launch; TUI launch-new                                                     | `agent` (`claude` \| `codex` \| `cursor`), `project` (abs path), `branch` (or empty), `session`, `dry_run`                  |
| `session.attach`       | `do_attach` (local), TUI Enter on local row                                                            | `session`, `target_user`                                                                                                     |
| `session.ended`        | wrapped subprocess returned (after `_run_launch_request`)                                              | `session`, `rc`, `wall_seconds`                                                                                              |
| `session.kill`         | `do_kill` (local), TUI `k` on local row                                                                | `session`, `target_user`, `force` (bool), `dry_run`                                                                          |
| `session.kill_all`     | `do_kill_all`                                                                                          | `target_users` (list), `killed_count` (int), `dry_run`                                                                       |
| `attach.remote.out`    | local TUI/CLI dispatching peer attach over SSH                                                         | `peer_name`, `ssh_alias`, `target_user`, `target_session`, `correlation_id`                                                  |
| `attach.remote.in`     | peer's `uxon attach` invoked over SSH (`SSH_CONNECTION` present, subcmd `attach`)                      | `target_user`, `target_session`, `correlation_id` (propagated via internal flag, see below)                                  |
| `kill.remote.out`      | local `uxon kill --host <peer>` or TUI `k` on remote row dispatching peer kill over SSH                | `peer_name`, `ssh_alias`, `target_user`, `target_session`, `force`, `dry_run`, `correlation_id`                              |
| `list.peek`            | local `uxon list --all-users` or TUI with `enable_all_users_list=true` actually enumerating others    | `scope_users` (list), `scope_skipped` (list)                                                                                 |
| `list.remote.in`       | peer's `uxon list --json` invoked over SSH                                                             | `scope` (`own` \| `all-users`), `correlation_id`                                                                              |
| `kill.remote.in`       | peer's `uxon kill` invoked over SSH                                                                    | `session`, `target_user`, `force`, `correlation_id`                                                                          |
| `git.remote.create`    | `do_new --git-remote <profile>` finished (success or error) the external repo creation                | `profile`, `repo`, `creds_user`, `rc`                                                                                        |
| `config.error`         | startup config load failed and `main()` is about to exit non-zero                                     | `path`, `error` (first 256 chars)                                                                                            |

### Correlation across hosts

For the three remote-inbound subcommands (`list`, `attach`, `kill`),
the caller generates a UUIDv4 and passes it to the peer via a single
internal CLI flag:

```
--audit-correlation-id <uuid>
```

- Hidden from `uxon --help` / subcommand help (it is internal protocol,
  not a public knob).
- Documented in this spec and in `docs/deployment.md`'s wire-protocol
  section.
- Older peers without flag support → flag is rejected, the SSH
  invocation fails. **Resolution**: this is a hard incompatibility for
  the rolling-upgrade window of the same major version. Same posture
  as the existing wire-schema rule (peers must run the same major).
  No silent fallback, because a silent fallback would lose the
  correlation property exactly when an operator is debugging across
  hosts.

The flag is parsed and stored in `audit.py` module state at startup;
events that include `correlation_id` read it from there. If the env
detection says we're a peer-inbound but the flag is absent (e.g.
peer was invoked manually for testing), `correlation_id` is omitted
rather than synthesized.

## What is removed in this change

The current "TUI event log" channel
(`~/.local/state/uxon/tui-{user}-{date}.log`) is removed in full:

| Was                                       | Becomes                                                  |
|-------------------------------------------|----------------------------------------------------------|
| `_log_event("tui_start", …)`              | `audit("tui.open", …)`                                   |
| `_log_event("tui_quit", …)`               | `debug("tui", reason=…)` under `UXON_DEBUG=tui`          |
| `_log_event("launch", …)`                 | `audit("session.new", …)`                                |
| `_log_event("launch_completed", …)`       | `audit("session.ended", …)` plus `debug("launch", stage=…, cmd=…)` for dev-only fields |
| `from uxon.tui.events import _log_event`  | deleted                                                  |
| `from uxon.tui.events import LOG_DIR`     | deleted                                                  |
| `from uxon.tui import LOG_DIR`            | deleted (`tui/__init__.py` re-export removed)            |
| `tests/test_uxon_tui_logging.py`          | deleted                                                  |
| `tests/test_uxon_tui_events.py`           | replaced by audit-channel tests                          |

The `debug()` channel (off by default, `UXON_DEBUG`-gated) and the
`metrics` channel (off by default, `UXON_METRICS=1`-gated) are kept
unchanged — they are developer-facing, opt-in, and orthogonal to
audit.

After this change there are exactly three logging channels with
non-overlapping responsibilities:

| Channel  | Sink                          | Default | Audience            | Fields          |
|----------|-------------------------------|---------|---------------------|-----------------|
| `audit`  | journald native / `/dev/log`  | on      | operator / lead     | action-level    |
| `debug`  | `~/.local/state/uxon/...`     | off     | developer (us)      | UI / launch debug |
| `metrics`| `~/.local/state/uxon/...`     | off     | developer (us)      | per-fetch latency|

`UXON_LOG_DIR` controls only `debug` and `metrics` paths after this
change. Documented as such.

## Call-site placement (where `audit()` is invoked)

| File                               | Site                                 | Event(s)                                    |
|------------------------------------|--------------------------------------|--------------------------------------------|
| `src/uxon/cli.py::main`            | after argv parse, before subcmd dispatch | `cli.start`                            |
| `src/uxon/cli.py::do_attach`       | success path; sudo/not-found error paths | `session.attach` (outcome ok/denied/not_found) |
| `src/uxon/cli.py::_do_attach_remote` | before `execvp`                    | `attach.remote.out`                          |
| `src/uxon/cli.py::do_attach`       | inbound branch (`SSH_CONNECTION` present) | `attach.remote.in`                       |
| `src/uxon/cli.py::do_kill`         | local kill (no `--host`)             | `session.kill` (outcome ok/denied/not_found) |
| `src/uxon/cli.py::do_kill` (caller, with `--host`) | before SSH dispatch  | `kill.remote.out`                           |
| `src/uxon/cli.py::do_kill` (peer, `SSH_CONNECTION` present) | inbound branch | `kill.remote.in`                          |
| `src/uxon/cli.py::do_kill_all`     | end of routine                       | `session.kill_all`                          |
| `src/uxon/cli.py::do_new`          | success path; failure                | `session.new` (outcome)                      |
| `src/uxon/cli.py::do_run`          | success path; failure                | `session.new`                                |
| `src/uxon/cli.py::do_list`         | branch where `--all-users` actually enumerates (local), or where invoked over SSH | `list.peek` (local cross-user) / `list.remote.in` (inbound) |
| `src/uxon/git_create.py`           | end of `create_or_attach_remote`     | `git.remote.create`                          |
| `src/uxon/tui/app.py::run`         | before `app.run()` first call        | `tui.open`                                   |
| `src/uxon/tui/app.py::run`         | after `_run_launch_request` returns  | `session.ended`                              |

The TUI's `tui_start` / `launch` / `launch_completed` call sites
collapse into the same lines that previously called `_log_event`,
now calling `audit` instead.

## Observed bugs and their resolutions

These are real issues found during design exploration. **Policy: fix
in this same change, not later.**

### Bug 1 — `tui/__init__.py:48` re-exports a name that's about to be deleted

`from .events import LOG_DIR` and `__all__ = [..., "LOG_DIR", ...]`.
After removing `_log_event` and `LOG_DIR` we must also drop the
re-export and the `__all__` entry. Otherwise `import uxon.tui` raises.
**Resolution**: delete both lines as part of step "remove TUI event
log".

### Bug 2 — `do_doctor` would not report audit-channel status

A user running `uxon doctor` would have no way to see whether audit is
reaching journald. **Resolution**: extend `do_doctor` with one
boolean check — does `audit.enabled` resolve to `true`, and does sink
detection at first-call return a non-no-op socket. Print one line:

```
audit:    enabled, sink=journald-native    (or sink=syslog / disabled / no-sink)
```

This is part of this change; it's the only way an operator validates
the deploy.

### Bug 3 — `--audit-correlation-id` parsing surface

Peer's CLI must accept the new flag for `list`, `attach`, `kill`. If
the existing parser is positional/manual (it is — `cli.py` does its
own argv walk), inserting one new keyword flag in three subcommand
branches is mechanical but easy to miss. **Resolution**: factor a
single helper `pop_correlation_id(rest: list[str]) -> str` that all
three subcommand parsers call before their own option walk. Stored
in `audit` module state.

### Bug 4 — sanitised flags in `cli.start`

Including raw `flags` may leak operator-supplied secrets (e.g.
`--token-file=/path/to/secret`). **Resolution**: sanitise before
emitting — drop any `--token-*`, `--password*`, mask values of
keyword flags whose names match a small denylist. Document the
denylist alongside `cli.start` in `docs/configuration.md`.

### Bug 5 — `git.remote.create` event needs an emission point that doesn't currently exist

`git_create.py` raises `CreationError` on failure but does not have a
single end-of-routine point that knows both the outcome and the
profile/repo/creds_user values. **Resolution**: in
`git_create.py::create_or_attach_remote` (or its caller in
`do_new`), wrap the call in a `try/finally` so the audit fires on
both paths, with `outcome="ok"` on success, `outcome="error"`
otherwise. The `rc` field carries the `CreationError`'s exit code if
present, else 0/1.

## Testability

`audit.py` exposes a private `_send_raw(payload: bytes) -> None` as
the only IO point. Tests inject a `_send_raw` recorder via
`monkeypatch.setattr`. No test ever opens `/dev/log` or
`/run/systemd/journal/socket`.

Test layers:

1. **Unit**: `test_uxon_audit.py`
   - prefix construction (caller_uid from SUDO_UID, ssh_client from
     SSH_CONNECTION, hostname/version stamping)
   - per-event field set required vs optional
   - sink detection ordering (mock `os.path.exists`)
   - native-journal serialization (`KEY=value\n` lines)
   - syslog-CEE serialization (`<PRI>VERSION TIMESTAMP HOST APP-NAME PROCID MSGID - @cee: {…}`)
   - flags sanitiser (Bug 4)
   - `audit.enabled = false` short-circuit (no `_send_raw` call)

2. **Integration over CLI handlers**: extend existing `tests/test_uxon_cli_*.py`
   with `_send_raw` recorder fixture; assert each `do_*` produces
   the right event(s).

3. **Doctor output** (`test_uxon_doctor.py`): asserts the audit line
   is present and reports the right sink.

## Performance verification

A small benchmark (committed under `tests/perf/test_audit_overhead.py`,
gated by `UXON_PERF=1` so it doesn't run on CI by default) measures:

- Cold first-call cost (sink detection + connect): expected < 200µs.
- Steady-state per-event cost: expected < 30µs median, < 100µs p99.
- 10 000 events back-to-back, ensuring no allocator-thrash regression.

Numbers go into the spec follow-up if they materially deviate.

## Documentation changes (in this same change)

- `README.md` — add one line in the relevant section: "uxon emits
  audit events to the platform log channel; see `docs/deployment.md`."
  No threat-model paragraph.
- `docs/deployment.md` — short paragraph (5–7 lines) under install
  section: where audit lands, why log files are root-owned, that uxon
  does not engineer a runtime defence against a launch user running
  their own copy (sudo's own audit covers privileged operations).
  Plus the wire-protocol note that `--audit-correlation-id` is part
  of the peer-protocol contract.
- `docs/configuration.md` — new entry for `[audit]` table; update
  `UXON_LOG_DIR` description to "controls debug + metrics paths only;
  audit goes to journald/syslog regardless".
- `docs/architecture.md` — replace single-channel description with
  three-channel table.
- `AGENTS.md` — add `src/uxon/audit.py` to code layout list.
- `CHANGELOG.md` — 4.0.0 entry: "TUI event log
  (`tui-{user}-{date}.log`) removed; audit events now go to the
  platform log (journald native / syslog) via the new `audit` channel.
  Solo-mode users querying via `journalctl --user-unit ... -t uxon`."

## Migration

Single major bump: 3.x → 4.0.

CHANGELOG calls out:

- TUI event-log file no longer written. Anyone with shell scripts
  depending on `~/.local/state/uxon/tui-*.log` (we know of none in the
  repo and the file is documented as best-effort telemetry) must
  switch to `journalctl SYSLOG_IDENTIFIER=uxon`.
- `uxon.tui.LOG_DIR` import removed.
- New peer-protocol flag `--audit-correlation-id`. Peers within a
  rolling-upgrade window must run the same major (existing posture).

No data conversion needed; the old log directory is left in place
(operator removes manually if desired).

## What this design deliberately does not do

- No `-I` Python isolated-mode shebang in installed entrypoint.
- No marker file `/etc/uxon/.installed`.
- No `__file__.startswith(install_prefix)` runtime guard.
- No `audit.tampered` event.
- No env-var override for `audit.enabled` (only config).
- No `python-systemd` dependency.
- No background writer thread; no in-process queue; no batching.
- No second sink in parallel; no fanout to a file.
- No reconnection on socket failure; events drop silently.
- No cross-host audit aggregation in uxon code.

Each is an explicit choice; the rationale is in the corresponding
section above.

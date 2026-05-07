# Audit-log implementation plan

## Overview

**Spec implemented:** `docs/superpowers/specs/2026-05-05-audit-log-design.md` (git rev b3b050d)
**Version bump:** `3.3.0.dev0` → `4.0.0` (major: `uxon.tui.LOG_DIR` public name removed; TUI
event-log file gone; new peer-protocol flag `--audit-correlation-id`)
**Branch:** `feat/audit-log` (branch off `feat/multi-host` once that lands, or off `main` if
already merged)
**Workflow mode:** Mode B (multi-commit feature branch; no PR opened until user says ship)

---

## Commit sequence

### Commit 1 — feat(audit): add `src/uxon/audit.py` core module

**Prerequisites:** none

**Scope:**
- `src/uxon/audit.py` (new file)
- `src/uxon/__init__.py` (version bump to `4.0.0`)
- `AGENTS.md` (code-layout entry for `audit.py`)

**What is added/edited:**

- `src/uxon/audit.py`:
  - Module-level `threading.Lock` guarding lazy-init; booleans `_initialized`, `enabled`; string
    `sink` (`"journal"` / `"syslog"` / `"none"`); cached socket object; cached prefix dict;
    cached `_correlation_id: str | None`.
  - **Module-level default**: `enabled = True` before `configure()` is called. This is what
    makes the `config.error` path (Bug 5) functional — it fires *before* `main()` reaches
    `configure()`. If `configure()` later sets `enabled=False`, subsequent calls short-circuit.
  - `configure(*, enabled: bool, syslog_facility: str, subcmd: str) -> None` — sets module-level
    config; called once by `main()` after `load_config` succeeds. `subcmd` is `args.action`
    (canonical name from `ParsedArgs`), NOT `sys.argv[1]` — alias-safe per spec §Caller-context.
  - `_detect_sink() -> str` — checks `/run/systemd/journal/socket` via `socket.AF_UNIX` stat,
    then `/dev/log`; returns `"journal"`, `"syslog"`, or `"none"`.
  - `_build_prefix() -> dict[str, Any]` — computes `host`, `uxon_version`, `caller_user`,
    `caller_uid`, `launch_user`, `pid`, `ppid`, `ssh_client` from env / `pwd.getpwuid()`.
  - `_send_raw(payload: bytes) -> None` — sole IO point; opens/caches `SOCK_DGRAM | SOCK_NONBLOCK`
    socket; swallows all `OSError` (including `EAGAIN`, `ENOBUFS`, `EPIPE`, `EMSGSIZE`); this is
    the test seam that tests monkeypatch.
  - `_serialize_journal(fields: dict[str, Any]) -> bytes` — `UPPERCASED_KEY=value\n` wire format
    per sd_journal_send protocol.
  - `_serialize_syslog(fields: dict[str, Any]) -> bytes` — RFC 5424 + CEE-JSON `@cee: {…}` body;
    uses `syslog_facility` set by `configure()`.
  - `_sanitize_flags(flags: list[str]) -> list[str]` — masks values of keyword flags whose names
    match prefix denylist `{"--token", "--password", "--secret"}` (replaces value with `REDACTED`).
    See spec §Bug 8.
  - `extract_correlation_id(argv: list[str]) -> tuple[str | None, list[str]]` — pops
    `--audit-correlation-id <uuid>` if present; returns `(uuid_or_None, filtered_argv)`.
    Bounds-checks `i+1 < len(argv)` before consuming the value.
  - `set_correlation_id(uid: str | None) -> None` — sets module-level `_correlation_id`.
  - `audit(event: str, *, outcome: str = "ok", **fields: Any) -> None` — public entrypoint:
    checks `enabled`, acquires lock only on first call for lazy-init, merges cached prefix +
    `ts` (ISO-8601 UTC) + per-call fields + `v=1`, serializes, calls `_send_raw`. Never raises.
    Steady-state (after init) takes no lock.

- `src/uxon/__init__.py`: `__version__ = "4.0.0"`

- `AGENTS.md`: add `src/uxon/audit.py` bullet after `settings.py` in the code-layout section.

**How to verify:**
```bash
python3 -m py_compile src/uxon/audit.py
python -c "from uxon.audit import audit, extract_correlation_id, set_correlation_id, configure; print('ok')"
python -c "import uxon.cli"   # must not import textual
ruff check src/uxon/audit.py && ruff format --check src/uxon/audit.py
pyright src/uxon/audit.py
```

---

### Commit 2 — feat(audit): `[audit]` config schema in `DEFAULT_CONFIG`, `Config`, `load_config`; fix Bug 5 part 1

**Prerequisites:** Commit 1

**Scope:**
- `src/uxon/cli.py`: `DEFAULT_CONFIG`, `Config` dataclass, `load_config`, `load_toml`
- `docs/configuration.md`

**What is added/edited:**

- `src/uxon/cli.py::DEFAULT_CONFIG`: add `"audit": {"enabled": True, "syslog_facility": "user"}`.

- `src/uxon/cli.py::Config` dataclass: add two fields with defaults so existing construction
  sites compile unchanged:
  ```python
  audit_enabled: bool = True
  audit_syslog_facility: str = "user"
  ```

- `src/uxon/cli.py::load_config`: after the `remote_hosts` block, read:
  ```python
  audit_tbl = merged.get("audit", DEFAULT_CONFIG["audit"])
  if not isinstance(audit_tbl, dict):
      fail("'audit' must be a TOML table")
  audit_enabled = bool(audit_tbl.get("enabled", True))
  audit_syslog_facility = str(audit_tbl.get("syslog_facility", "user"))
  ```
  Add `audit_enabled=audit_enabled, audit_syslog_facility=audit_syslog_facility` to
  `return Config(...)`.

- `src/uxon/cli.py::load_toml` (around line 279): wrap `tomllib.load(f)` in:
  ```python
  try:
      data = tomllib.load(f)
  except tomllib.TOMLDecodeError as exc:
      fail(f"invalid TOML in {path}: {exc}", 1)
  ```
  This is Bug 5 part 1 — converts the malformed-TOML escape path from an unhandled traceback
  into the same `SystemExit` shape every other config error takes. Bug 5 part 2 (the
  `try/except SystemExit` wrapper in `main()` that emits `config.error`) lands in Commit 5.

- `docs/configuration.md`: new `[audit]` table entry documenting `enabled` (bool, default `true`)
  and `syslog_facility` (string, default `"user"`, consulted only on `/dev/log` fallback path).
  Update `UXON_LOG_DIR` description: "controls debug and metrics paths only; audit events go to
  journald/syslog regardless".

**How to verify:**
```bash
python3 -m py_compile src/uxon/cli.py
python -c "from uxon.cli import DEFAULT_CONFIG; print(DEFAULT_CONFIG['audit'])"
pytest tests/test_uxon.py -n auto -k "config"
ruff check src/uxon/cli.py && ruff format --check src/uxon/cli.py
pyright src/uxon/cli.py
```
Manually confirm `load_config` round-trips `[audit]\nenabled = false\nsyslog_facility = "daemon"`
from a temp TOML without error.

---

### Commit 3 — test(audit): `tests/test_uxon_audit.py` + perf scaffold

**Prerequisites:** Commits 1, 2

**Scope:**
- `tests/test_uxon_audit.py` (new file)
- `tests/perf/test_audit_overhead.py` (new file)

**What is added:**

- `tests/test_uxon_audit.py` — `unittest.TestCase` classes covering all spec §Testability items:
  - `PrefixConstructionTests`: patches `SUDO_USER`, `SUDO_UID`, `SSH_CONNECTION`, `USER`,
    `socket.gethostname`; asserts `_build_prefix()` returns correct field values including
    `caller_uid` from `SUDO_UID` and omission of `ssh_client` when `SSH_CONNECTION` absent.
  - `SinkDetectionTests`: uses `unittest.mock.patch("os.path.exists", ...)` or `mock.patch("os.stat")`
    to simulate journal socket present/absent, `/dev/log` present/absent; asserts `_detect_sink()`
    returns `"journal"`, `"syslog"`, or `"none"` in the right order.
  - `JournalSerializationTests`: calls `_serialize_journal({"event": "x.y", "num": 3})`; asserts
    decoded bytes contain `EVENT=x.y\n` and `NUM=3\n` (keys uppercased).
  - `SyslogSerializationTests`: asserts output starts with a valid `<PRI>1 ` syslog header and
    contains `@cee: {` followed by valid JSON.
  - `FlagSanitizerTests`: `_sanitize_flags(["--token-file=/s", "--project=x"])` returns a list
    where `--token-file` value is `REDACTED`; `--project` is unchanged.
  - `AuditDisabledTests`: patches `_send_raw` to a list-appender; calls `configure(enabled=False,
    syslog_facility="user")`, resets `_initialized` to False, then calls `audit("x.y")`; asserts
    recorder is never called.
  - `CorrelationIdTests`: `extract_correlation_id(["--audit-correlation-id", "u4", "--json"])`
    returns `("u4", ["--json"])`; absent flag returns `(None, original_list)`.
  - `AuditSendTests`: patches `_send_raw` to a list-appender; calls `audit("cli.start",
    flags=[], agents_enabled=["claude"], ...)` with `configure(enabled=True, ...)`; asserts one
    entry recorded and the deserialized event dict contains `v=1`, `event="cli.start"`, `ts` field.

- `tests/perf/test_audit_overhead.py`: gated by `UXON_PERF=1` env var (`unittest.skipUnless`);
  patches `_send_raw` to a no-op; measures cold first-call latency (< 200 µs), steady-state median
  (< 30 µs) and p99 (< 100 µs) over 10 000 iterations; asserts thresholds.

**How to verify:**
```bash
pytest tests/test_uxon_audit.py -v
# All tests must pass; no real socket access.
pytest tests/ -n auto   # full suite still green
```

---

### Commit 4 — feat(audit): `--audit-correlation-id` flag — parsers + `ParsedArgs`

**Prerequisites:** Commit 1

**Scope:**
- `src/uxon/cli.py`: `ParsedArgs`, `parse_list_args`, `_parse_kill_extras`, `_parse_attach_extras`

**What is added/edited:**

- `src/uxon/cli.py::ParsedArgs`: add field `audit_correlation_id: str | None = None`.

- `src/uxon/cli.py::parse_list_args`: at the top of the function body, before `i = 0`, add:
  ```python
  from uxon.audit import extract_correlation_id, set_correlation_id
  corr_id, argv = extract_correlation_id(argv)
  if corr_id:
      set_correlation_id(corr_id)
  ```
  Pass `audit_correlation_id=corr_id` into the returned `ParsedArgs`.

- `src/uxon/cli.py::_parse_kill_extras`: same pattern applied to `rest` before the `i = 0` walk.
  Pass `audit_correlation_id=corr_id` into returned `ParsedArgs`.

- `src/uxon/cli.py::_parse_attach_extras`: same pattern.

The flag never surfaces in `--help` because all three parsers use manual `while i < len(...)` argv
walks; `extract_correlation_id` pops the flag before the walk sees it.

**How to verify:**
```bash
python3 -m py_compile src/uxon/cli.py
python -c "
from uxon.cli import parse_list_args
a = parse_list_args(['--audit-correlation-id', 'abc-123', '--json'])
assert a.audit_correlation_id == 'abc-123' and a.json_output, repr(a)
print('ok')
"
pytest tests/test_uxon_cli_*.py -n auto
ruff check src/uxon/cli.py
```
Confirm `uxon list --audit-correlation-id` (missing value) fails with the existing
`extract_correlation_id` bounds-check error, not a crash.

---

### Commit 5 — feat(audit): `cli.start`, `config.error`, and `tui.open` call sites; Bug 5 part 2

**Prerequisites:** Commits 1, 2, 4

**Scope:**
- `src/uxon/cli.py::main` (around line 4295)
- `src/uxon/tui/app.py::run` (around line 1276)

**What is added/edited:**

- `src/uxon/cli.py::main`: wrap the `load_config` call in `try/except SystemExit`:
  ```python
  from uxon import audit as _audit
  try:
      cfg = load_config(os.getcwd())
  except SystemExit as ex:
      _audit.audit("config.error", outcome="error",
                   path=str(repo_config_path()),
                   error=str(ex)[:256])
      raise
  ```
  Immediately after successful `load_config`, configure the audit module:
  ```python
  _audit.configure(enabled=cfg.audit_enabled, syslog_facility=cfg.audit_syslog_facility)
  ```
  Then, after the `if args.action == "version":` and `if args.action == "doctor":` early-returns
  but before the `run` / `new` / `attach` / `kill` / `list` dispatch, emit `cli.start`. Skip for
  pure `--help`/`--version` — those raise `SystemExit(0)` inside `parse_args` before `main`
  resumes, so the placement after `load_config` already excludes them:
  ```python
  _audit.audit(
      "cli.start",
      flags=_audit._sanitize_flags(list(argv or [])),
      agents_enabled=list(cfg.enabled_agents),
      enable_all_users_list=cfg.enable_all_users_list,
      audit_enabled=True,
      allowed_roots_count=len(cfg.allowed_roots),
      remote_hosts_count=len(cfg.remote_hosts),
  )
  ```
  Place this emit after `configure()` and after the `version`/`doctor` early-returns so
  `uxon version` and `uxon --help` do not emit `cli.start`. The `doctor` early-return path should
  emit `cli.start` — `uxon doctor` is a substantive invocation that should appear in the audit
  trail.

- `src/uxon/tui/app.py::run`: replace the `_log_event("tui_start", ...)` block at line 1276 with:
  ```python
  from uxon import audit as _audit
  _audit.audit("tui.open")
  ```
  Do NOT remove any other `_log_event` calls in this file yet — they stay until Commit 9.

**How to verify:**
```bash
python3 -m py_compile src/uxon/cli.py src/uxon/tui/app.py
python -c "import uxon.cli"   # no textual
pytest tests/ -n auto
```
Manual: `uxon version` emits no `cli.start` (exits before the audit block). `uxon doctor`
does emit `cli.start` (does not early-return before the audit block under the current dispatch
order — verify by inspection).

---

### Commit 6 — feat(audit): `session.new`, `session.ended` call sites

**Prerequisites:** Commit 5

**Scope:**
- `src/uxon/cli.py::do_new`, `src/uxon/cli.py::do_run`
- `src/uxon/cli.py::on_launch_cwd` / `on_launch_new` / `on_launch_existing` callbacks
  (lines around 4002–4009 — the TUI "new session" / "open session" / "open existing"
  gestures route through these)
- `src/uxon/tui/app.py::run` (the `launch` / `launch_completed` block around line 1311)

**What is added/edited:**

- `src/uxon/cli.py::do_new`: on the primary launch path, just before
  `return launch_in_tmux(target_dir, session, args, cfg, branch, launch_user)`:
  ```python
  from uxon import audit as _audit
  _audit.audit("session.new", agent=args.agent or cfg.default_agent,
               project=target_dir, branch=branch or "",
               session=session, dry_run=args.dry_run)
  ```
  Also emit `session.new` on the worktree path just before its corresponding `launch_in_tmux`
  call (same function, same structure).

- `src/uxon/cli.py::do_run`: same emit before `return launch_in_tmux(...)`.

- `src/uxon/cli.py::on_launch_cwd` / `on_launch_new` / `on_launch_existing`
  (lines 4002–4009 area — these are the three TUI "launch" callbacks installed on
  `TuiContext`): emit a `session.*` audit event right after the `_plan_tui_*` call
  returns. **Two subtleties** verified against current code (`cli.py:3547–3623,
  3626–3662`):

  1. `_plan_tui_existing_session_or_launch` (the tail both `_plan_tui_create_new_agent`
     and `_plan_tui_open_existing_agent` go through) returns **either** an attach
     `LaunchRequest` (label starts with `"attach"` or `"switch-client"`) **or** a launch
     `LaunchRequest` (label `"launch <session>"`). The callback must discriminate to emit
     the correct event type. `_plan_tui_run_agent` (used by `on_launch_cwd`) only ever
     returns a launch request, so `on_launch_cwd` can hardcode `session.new`.

  2. `on_launch_new(name, ...)` and `on_launch_existing(name, ...)` receive a bare
     project name, not an absolute path. The absolute path is computed inside
     `_resolve_tui_project_dir` (`cli.py:3611`) as
     `canonical(os.path.join(cfg.new_project_root, name))`. The callback must
     reproduce that derivation to populate `project=` per spec §Event alphabet
     (which requires "abs path"). Do **not** call `_resolve_tui_project_dir` again —
     it has a `mkdir -p` side effect; just reuse the path expression.

  Concretely:
  ```python
  # in on_launch_cwd — only ever a new session
  req = _plan_tui_run_agent(cfg, launch_user, cwd, agent_id, mode_id)
  from uxon import audit as _audit
  _audit.audit("session.new", agent=agent_id, project=cwd, branch="",
               session=req.label, dry_run=False)
  return req

  # in on_launch_new — may be either, discriminate by req.label
  req = _plan_tui_create_new_agent(cfg, launch_user, name, agent_id, mode_id, git_profile)
  project = canonical(os.path.join(cfg.new_project_root, name))
  from uxon import audit as _audit
  if req.label.startswith(("attach", "switch-client")):
      _audit.audit("session.attach", session=req.label,
                   target_user=launch_user, project=project)
  else:
      _audit.audit("session.new", agent=agent_id, project=project,
                   branch="", session=req.label, dry_run=False)
  return req

  # in on_launch_existing — same discrimination; absolute path same way
  req = _plan_tui_open_existing_agent(cfg, launch_user, name, agent_id, mode_id)
  project = canonical(os.path.join(cfg.new_project_root, name))
  from uxon import audit as _audit
  if req.label.startswith(("attach", "switch-client")):
      _audit.audit("session.attach", session=req.label,
                   target_user=launch_user, project=project)
  else:
      _audit.audit("session.new", agent=agent_id, project=project,
                   branch="", session=req.label, dry_run=False)
  return req
  ```
  **No changes to `LaunchRequest` are needed** — emitting from the callbacks
  side-steps the `LaunchRequest` field-addition and keeps `context.py` unchanged.
  (Supersedes the original D2/A1 proposal; see resolution at the end of this plan.)

- `src/uxon/tui/app.py::run`: replace `_log_event("launch", ...)` (line 1311) with a
  pure `debug("launch", …)` call carrying the dev-only fields (`stage`, `cmd[:2]`,
  `label`). Do **not** emit `session.new` from here — the three callback sites above
  already cover every TUI launch gesture with full structured fields. After
  `rc, stage, wall_seconds = _run_launch_request(req)` (line 1318), add:
  ```python
  from uxon import audit as _audit
  _audit.audit("session.ended",
               session=req.label, rc=rc,
               wall_seconds=round(wall_seconds, 3))
  ```
  The `_log_event("launch_completed", ...)` call at line 1319 stays in place for now;
  it is removed in Commit 9 alongside the rest of `_log_event`. Keep them dual-emitted
  for that single commit window so test harnesses see no gap.

**How to verify:**
```bash
python3 -m py_compile src/uxon/cli.py src/uxon/tui/app.py src/uxon/tui/context.py
pytest tests/ -n auto
```
With `_send_raw` recorder fixture: `do_run` dry-run emits `session.new` with `dry_run=True`.

---

### Commit 7 — feat(audit): attach/kill call sites + inbound-detection branches (Bugs 6, 7)

**Prerequisites:** Commit 5

**Scope:**
- `src/uxon/cli.py::_do_attach_remote` (around line 2280)
- `src/uxon/cli.py::do_attach` (starting at line 2299)
- `src/uxon/cli.py::do_kill` (starting at line 2526)
- `src/uxon/cli.py::_do_kill_remote` (starting at line 2433)
- `src/uxon/cli.py::do_kill_all` (starting at line 2651)
- `src/uxon/cli.py::on_kill_all_reachable` (starting at line 3905) — **multi-user** kill
  path used by the TUI's "kill all reachable" gesture; iterates over
  `{launch_user, *sudo_caps.reachable_users}`. Missing in earlier draft. This is the path
  with the most operationally significant audit value (cross-user bulk kill).

**What is added/edited:**

- `src/uxon/cli.py::_do_attach_remote`: before `os.execvp(ssh_argv[0], ssh_argv)` at line 2295:
  ```python
  import uuid as _uuid
  from uxon import audit as _audit
  corr_id = str(_uuid.uuid4())
  _audit.set_correlation_id(corr_id)
  _audit.audit("attach.remote.out",
               peer_name=peer.name, ssh_alias=peer.ssh_alias,
               target_user=args.user, target_session=args.target_id,
               correlation_id=corr_id)
  ```
  Inject `corr_id` into the peer-side invocation by appending
  `f" --audit-correlation-id {corr_id}"` to the `remote_cmd` string before `build_peer_ssh_argv`
  is called (line 2281–2291).

- `src/uxon/cli.py::do_attach`: at the top of the function, before `if args.host is not None:`:
  ```python
  from uxon import audit as _audit
  if os.environ.get("SSH_CONNECTION"):
      _audit.audit("attach.remote.in",
                   target_user=args.user or launch_user,
                   target_session=args.target_id)
      # _correlation_id already set by extract_correlation_id in parser
  ```
  On the local cross-user path (before `os.execvp` at line 2336):
  ```python
  _audit.audit("session.attach", session=target.name, target_user=target_user, outcome="ok")
  ```
  On the `not reachable` path (after the `eprint` at line 2317):
  ```python
  _audit.audit("session.attach", session=args.target_id or "", target_user=target_user, outcome="denied")
  ```
  On the same-user path, in `do_attach` itself before
  `return attach_session(target, cfg, launch_user, args.dry_run)` at line 2356:
  ```python
  _audit.audit("session.attach", session=target.name, target_user=launch_user)
  ```
  **Do NOT add the audit call inside `attach_session()` (the helper at lines 2388–2399
  whose `os.execvp` is at line 2398).** That helper is shared by both the CLI execvp path
  AND the TUI's `attach_session_blocking` (line 2402). Emitting from `do_attach` keeps
  the call exactly once per CLI invocation and preserves the SSH_CONNECTION-before-sudo
  ordering captured in spec §Bug 7. All audit calls in this commit precede their
  corresponding `os.execvp` per spec §Bug 7.

- `src/uxon/cli.py::do_kill`: at the top of the function, before `if args.host is not None:`:
  ```python
  from uxon import audit as _audit
  if os.environ.get("SSH_CONNECTION"):
      _audit.audit("kill.remote.in",
                   session=args.target_id, target_user=args.user or launch_user,
                   force=args.force, correlation_id=_audit._correlation_id)
  ```
  On the `--host` dispatch path (before `return _do_kill_remote(args, cfg)`):
  ```python
  import uuid as _uuid
  corr_id = str(_uuid.uuid4())
  _audit.set_correlation_id(corr_id)
  _audit.audit("kill.remote.out",
               peer_name=..., ssh_alias=...,
               target_user=args.user, target_session=args.target_id,
               force=args.force, dry_run=args.dry_run,
               correlation_id=corr_id)
  ```
  Inject the correlation-id flag into the peer command inside `_do_kill_remote`. **Authoritative
  injection site (resolves A2):** `_do_kill_remote` builds the peer command as a parts-list
  at lines 2469–2479 (verified against current tree):
  ```python
  remote_cmd_parts = [
      shlex.quote(target_host.remote_uxon),
      "kill",
      "--force",
      shlex.quote(str(args.target_id)),
  ]
  if args.user:
      remote_cmd_parts.extend(["--user", shlex.quote(args.user)])
  if args.json_output:
      remote_cmd_parts.append("--json")
  ```
  Append `["--audit-correlation-id", shlex.quote(corr_id)]` to `remote_cmd_parts` BEFORE
  the `remote_cmd = " ".join(remote_cmd_parts)` line. (Unlike `_do_attach_remote`, this
  function uses `subprocess.run`, not `os.execvp`, so there is no process-replacement
  ordering concern — the audit emit is correct anywhere before `subprocess.run`.)
  On local success paths (after `run_cmd(full, check=True)` at lines 2595 and 2634):
  ```python
  _audit.audit("session.kill", session=target.name, target_user=target_user,
               force=args.force, dry_run=args.dry_run)
  ```
  On the `not reachable` path for cross-user kill:
  ```python
  _audit.audit("session.kill", session=args.target_id or "", target_user=target_user,
               force=args.force, dry_run=args.dry_run, outcome="denied")
  ```

- `src/uxon/cli.py::do_kill_all`: at the end of the routine, after the loop completes
  but before `return 0`:
  ```python
  killed = sum(1 for r in results if r["action"] == "killed")
  _audit.audit("session.kill_all",
               target_users=[launch_user], killed_count=killed, dry_run=args.dry_run)
  ```
  (`do_kill_all` itself only operates on `launch_user`'s own sessions — single-user
  case. Multi-user kill comes from the TUI path below.)

- `src/uxon/cli.py::on_kill_all_reachable` (line 3905, the TUI's
  multi-user "kill all reachable" gesture): after the outer `for u in users:` /
  inner `for s in fresh:` double-loop completes, emit:
  ```python
  from uxon import audit as _audit
  _audit.audit("session.kill_all",
               target_users=users, killed_count=killed_count, dry_run=False)
  ```
  Where `killed_count` is computed by counting successful `run_cmd` calls inside
  the loop (track with a local counter; `run_cmd(..., check=False)` returns rc).
  This is the operationally most-significant kill_all path (cross-user) and was
  missing from the earlier draft.

**How to verify:**
```bash
python3 -m py_compile src/uxon/cli.py
pytest tests/test_uxon_cli_*.py -n auto -v -k "kill or attach"
```
With `_send_raw` recorder: assert `do_kill` (own-user, dry-run) emits `session.kill` with
`dry_run=True`. Assert `SSH_CONNECTION` in env causes `kill.remote.in` (not `session.kill`).
Assert `do_kill_all` dry-run emits `session.kill_all` with `killed_count=0`.

---

### Commit 8 — feat(audit): `list` and `git.remote.create` call sites (Bugs 3, 4)

**Prerequisites:** Commits 4 (correlation-id parser), 5

**Scope:**
- `src/uxon/cli.py::main` (list block, lines 4345–4379)
- `src/uxon/cli.py::_do_create_git_remote` (lines 2932–2950)

**What is added/edited:**

- `src/uxon/cli.py::main`, list block: immediately after the `if args.host is not None:` and
  `if args.all_hosts:` early-returns (which handle those paths without further local processing),
  add:
  ```python
  from uxon import audit as _audit
  if os.environ.get("SSH_CONNECTION"):
      _audit.audit("list.remote.in",
                   scope="all-users" if args.all_users else "own",
                   correlation_id=_audit._correlation_id)
  ```
  Then, inside `if args.all_users:`, after the `enable_all_users_list` gate passes and after
  `scope_users, scope_skipped = _resolve_all_users_scope(cfg, launch_user)`:
  ```python
  _audit.audit("list.peek", scope_users=scope_users, scope_skipped=list(scope_skipped))
  ```
  `list.peek` must NOT fire if `cfg.enable_all_users_list` is false (the `fail()` at line 4356
  exits before we reach it — correct by placement).

- `src/uxon/cli.py::_do_create_git_remote`: wrap `create_project_remote(...)` call:
  ```python
  from uxon import audit as _audit
  _git_ok = False
  try:
      result = uxon_git_create.create_project_remote(
          profile, repo_name, project_dir,
          launch_user=launch_user, current_user=current_user, dry_run=args.dry_run,
      )
      _git_ok = True
  except uxon_git_create.CreationError as exc:
      _audit.audit("git.remote.create", outcome="error",
                   profile=profile.name, repo=repo_name,
                   creds_user=profile.creds_user or launch_user, rc=1)
      fail(f"git remote creation failed at stage {exc.stage!r}: {exc}")
  if _git_ok:
      _audit.audit("git.remote.create", outcome="ok",
                   profile=profile.name, repo=repo_name,
                   creds_user=profile.creds_user or launch_user, rc=0)
  ```

**How to verify:**
```bash
python3 -m py_compile src/uxon/cli.py
pytest tests/ -n auto
```
With recorder: `list.peek` fires with `enable_all_users_list=True` + `--all-users`. `list.remote.in`
fires with `SSH_CONNECTION` set. `git.remote.create` fires on both success and mocked
`CreationError` paths.

---

### Commit 9 — feat(audit): doctor extension; TUI event-log removal (Bugs 1, 2, 9)

**Prerequisites:** Commits 5, 6, 7, 8 (all audit call sites live; bridge overlap is complete)

**Scope:**
- `src/uxon/cli.py::do_doctor` (+ `doctor_issues`)
- `src/uxon/tui/app.py::run`
- `src/uxon/tui/__init__.py`
- `src/uxon/tui/events.py` (delete `_log_event`)
- `tests/test_uxon_tui_logging.py` (delete `LogEventTests` class)
- `tests/test_uxon_doctor.py` (extend with audit-line assertions)

**What is added/edited:**

**Doctor extension (Bug 2):**

- `src/uxon/cli.py::do_doctor`, human-readable output block (after the per-agent status lines):
  ```python
  from uxon import audit as _audit
  _sink = {"journal": "journald-native", "syslog": "syslog", "none": "no-sink"}.get(
      _audit.sink, "unknown"
  )
  print(f"audit:    {'enabled' if _audit.enabled else 'disabled'}, sink={_sink}")
  ```
  JSON output block (inside `if json_output:`, add to `data`):
  ```python
  data["audit"] = {"enabled": _audit.enabled, "sink": _audit.sink}
  ```

**TUI event-log removal (Bugs 1, 9):**

- `src/uxon/tui/app.py::run`:
  - Remove `_log_event("tui_quit", ...)` at line 1299; replace with:
    `debug("tui", reason=f"rc={app.quit_rc}")`
  - Remove `_log_event("launch_completed", ...)` at line 1319 (the `session.ended` audit call
    already lands here from Commit 6; remove the `_log_event` call).
  - Remove `from uxon.tui.events import _log_event` import from `app.py`.
  - Confirm the `_log_event("tui_start", ...)` was already replaced in Commit 5.

- `src/uxon/tui/events.py`: delete `_log_event` function (lines 266–328 per current file). Keep
  `LOG_DIR`, `_log_dir`, `debug`, `metrics_record`, and all helpers above line 266.

- `src/uxon/tui/__init__.py`:
  - Remove `from .events import LOG_DIR` (line 37).
  - Remove `"LOG_DIR"` from `__all__` (line 48).
  - Update module docstring line referring to `events — JSONL event log (LOG_DIR, _log_event)` to
    `events — debug and metrics channels`.

- `tests/test_uxon_tui_logging.py`: delete the `LogEventTests` class (lines 15–58 per current
  file). Update the module docstring from "Event-log tests for `_log_event`" to "Tests for the
  debug and metrics logging channels". Keep `StartupChannelTests` and `MetricsJsonlTests` intact.

- `tests/test_uxon_doctor.py`: add:
  - `audit:` line present in `do_doctor` stdout (human-readable path).
  - `data["audit"]` key present in `do_doctor` JSON output.
  - `data["audit"]["sink"]` is one of `{"journal", "syslog", "none"}`.

**How to verify:**
```bash
python3 -m py_compile src/uxon/tui/app.py src/uxon/tui/__init__.py src/uxon/tui/events.py
python -c "from uxon.tui import TuiContext; print('ok')"
python -c "from uxon.tui import LOG_DIR"   # must raise AttributeError
python -c "import uxon.cli"   # no textual
pytest tests/ -n auto
```

---

### Commit 10 — docs: README, deployment.md, architecture.md, CHANGELOG (Bug 10)

**Prerequisites:** Commit 9 (all behavior locked in)

**Scope:**
- `README.md`
- `docs/deployment.md`
- `docs/architecture.md`
- `CHANGELOG.md`

**What is edited:**

- `README.md`: add one sentence in the relevant logging/monitoring paragraph: "uxon emits audit
  events to the platform log channel; see `docs/deployment.md`."

- `docs/deployment.md`:
  - Under the install section: add a 5–7 line paragraph describing where audit events land (journald
    native on systemd hosts, syslog fallback otherwise), that log files are root-owned under the
    prescribed install path, that uxon does not defend at runtime against a launch user running their
    own copy (sudo's own audit covers privileged operations), and that `uxon doctor` reports the
    active sink.
  - Wire-protocol note: `--audit-correlation-id` is part of the peer-protocol contract for `list`,
    `attach`, `kill`; peers must run the same major version (consistent with existing wire-schema
    policy).
  - **1.x → 2.0 migration note** (lines 352–355): change `UXON_LOG_DIR` description from "TUI
    events" to "debug and metrics paths only". (Bug 10)
  - **New 4.0 migration entry**: "TUI event log (`~/.local/state/uxon/tui-{user}-{date}.log`) no
    longer written. Query events via `journalctl SYSLOG_IDENTIFIER=uxon`. The `uxon.tui.LOG_DIR`
    public import has been removed." (Bug 10)

- `docs/architecture.md`: replace the single-channel log description with the three-channel table:

  | Channel  | Sink                          | Default | Audience            |
  |----------|-------------------------------|---------|---------------------|
  | `audit`  | journald native / `/dev/log`  | on      | operator / lead     |
  | `debug`  | `~/.local/state/uxon/…`       | off     | developer           |
  | `metrics`| `~/.local/state/uxon/…`       | off     | developer           |

- `CHANGELOG.md`: under `## [Unreleased]` (rename to `## [4.0.0]` at release time):
  ```
  ### Changed (breaking)
  - Audit events now go to the platform log (journald / syslog) instead of
    `~/.local/state/uxon/tui-{user}-{date}.log`. That file is no longer written.
  - `uxon.tui.LOG_DIR` public import removed.
  - Peer protocol: `list`, `attach`, `kill` now accept `--audit-correlation-id` (internal flag).
    All peers in a fleet must run the same major version (existing upgrade posture).

  ### Added
  - `uxon doctor` reports audit channel status (`enabled`, `sink=journald-native|syslog|no-sink`).
  - `[audit]` config table: `enabled` (default `true`) and `syslog_facility` (default `"user"`).
  - Events emitted to journald/syslog: `cli.start`, `tui.open`, `session.new`, `session.attach`,
    `session.ended`, `session.kill`, `session.kill_all`, `attach.remote.out`, `attach.remote.in`,
    `kill.remote.out`, `kill.remote.in`, `list.peek`, `list.remote.in`, `git.remote.create`,
    `config.error`.
  ```

**How to verify:**
```bash
pytest tests/ -n auto
python -m build && twine check dist/*
```
Read `docs/deployment.md` and confirm both the 2.0 update and the 4.0 migration entry are present.

---

## Parallelization map

The commits are designed for a single-implementer linear pass. The recommended order is
**1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10**.

Commits that are independent of each other and can be drafted in a single implementation pass
then split at `git add -p` time:
- Commits 1 and 3 (module + tests) can be written together.
- Commits 6, 7, 8 (CLI call sites) can be drafted in one pass over `cli.py` and `app.py`, then
  split into three commits.

Commits that must serialize:
- Commit 9 (TUI event-log removal) requires Commits 5–8 to be complete first. The bridge that
  keeps the tree green is the overlap period in Commits 5–8 where both `_log_event` calls and
  `audit()` calls coexist. Removing `_log_event` before its replacements are wired would drop
  events from the audit trail.

---

## Rollout smoke-test checklist (operator, post-merge)

1. `uxon doctor` on each host — confirm `audit:` line is present and shows the expected sink.
2. On a systemd host: `journalctl -t uxon --since "5 minutes ago"` — confirm the `cli.start`
   event from the doctor invocation appears as structured fields.
3. On a syslog-only host: `grep uxon /var/log/syslog | tail -5` — confirm CEE-JSON records
   (`@cee: {…}`).
4. Trigger `config.error`: temporarily insert invalid TOML in `config/config.toml`, run any
   `uxon` subcommand, confirm the `config.error` event appears in the journal before exit.
5. Cross-host attach: `uxon attach --host <peer> --user <u> <id>`. Check local host journal for
   `attach.remote.out` and peer journal for `attach.remote.in`. Both should carry the same
   `correlation_id`.
6. `uxon list --all-users` (with `enable_all_users_list=true`): check for `list.peek` event.
7. Old `~/.local/state/uxon/tui-*.log` files are left in place — no automatic cleanup. Notify
   operators with scripts reading those files to switch to `journalctl SYSLOG_IDENTIFIER=uxon`.

---

## Decision notes (ambiguities resolved during planning)

**D1 — `syslog_facility` at config-error time.**

`config.error` fires before `audit.configure()` is called (Bug 5 part 2: the `try/except` wraps
`load_config`, which precedes `configure()`). On that code path the module uses its compile-time
defaults (`enabled=True`, `syslog_facility="user"`). The spec at §Bug 5 says "The first `audit()`
call here triggers lazy sink detection — fine", explicitly accepting this ordering. No code change
needed beyond what is described in Commit 5.

**D2 — TUI `session.new` emission point: `on_launch_*` callbacks, not `LaunchRequest` mutation.**

The spec calls for `session.new` from the TUI path to carry `agent`, `project`, `branch`,
`session`. The first-pass plan proposed adding fields to `LaunchRequest` in `context.py` and
plumbing them through `screens/main.py`. **Superseded** by the cleaner approach: emit
`session.new` directly from the three TUI launch callbacks
(`on_launch_cwd`, `on_launch_new`, `on_launch_existing`) in `cli.py` (lines 4002–4009),
where `agent_id`, `cwd`/`name`, and `launch_user` are already in scope and the returned
`req.label` carries the session name. **No changes to `LaunchRequest` or `context.py`
are required.** This resolves former ambiguity A1 authoritatively.

**D3 — `LOG_DIR` constant retained in `events.py`.**

Spec §Bug 1 says to delete the `tui/__init__.py` re-export and `__all__` entry. `LOG_DIR` itself
lives in `events.py` and is referenced internally by the snapshot comment ("kept for
backward-compat with code that imports the constant directly"). The plan removes the public
re-export only. `LOG_DIR` in `events.py` remains as an internal constant; no external code
(confirmed by grep) imports it directly from `events.py` in the current tree.

**D4 — `subcmd` field in caller-context prefix.**

Spec §Caller-context prefix says `subcmd` is `args.action` from `ParsedArgs`. At the time the
prefix is built (first `audit()` call in `main()`), `args` is available in `main()`'s scope.
`audit.configure()` does not receive `args.action`. Resolution: pass `subcmd` as a per-call field
on the `cli.start` emit (not as part of the cached prefix), OR pass it to `configure()`. Since the
spec says `subcmd` is "cached caller-context prefix" but is different per invocation, it should be
computed at configure time. The plan passes it to `configure()` as `subcmd: str`. The module
stores it in the prefix dict so every subsequent `audit()` call includes it automatically.

---

## Open ambiguities

None remaining. The first-pass plan listed two (A1, A2); both were resolved in plan
review and now appear authoritatively in Commit 6 (D2: emit `session.new` from
`on_launch_*` callbacks; no `LaunchRequest` change) and Commit 7 (correlation-id
injection appends to `remote_cmd_parts` at lines 2469–2479 of `_do_kill_remote`).

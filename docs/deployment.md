# Deployment

This document is for operators running `uxon` on one or more Linux
hosts. For a solo developer on a single host, the
[Install](../README.md#install) section in `README.md` is enough.

## Single-host install

```bash
git clone https://github.com/vzd3v/uxon.git
cd uxon
sudo python3 install/install_uxon.py \
  --repo-dir "$(pwd)" \
  --install-path /usr/local/bin/uxon
# (uses /opt/uxon/venv by default; override with --venv-dir; --dry-run
# to preview)

cp config/config.example.toml config/config.toml
$EDITOR config/config.toml         # set allowed_roots, session_users, agents
uxon doctor                         # verify
```

### Audit channel

`uxon` emits one structured audit event per substantive operator
gesture.  The channel auto-detects its sink at first call: journald
native protocol via `/run/systemd/journal/socket` if present,
`/dev/log` syslog otherwise.  No `python-systemd` dependency; the
wire layer is stdlib-only.

Per-event schema (envelope, event alphabet, outcome semantics) lives
in [`docs/audit-events.md`](audit-events.md).  This section covers
the operational topology — where events land, who can read them,
and how to query.

Under the prescribed install path (`sudo install/install_uxon.py`
into `/opt/uxon/venv`) the package files are root-owned and journald
/ syslog files are root-owned — a launch user can append events but
cannot edit the trail.  `uxon` does **not** try to defend at runtime
against a launch user running their own copy; privileged operations
(`sudo -iu …`) appear in `sudo`'s own audit trail (`auth.log` /
journald), which is the source of truth for who-did-what at the OS
level.  `uxon`'s audit is application-level value-add — which
session, which agent, which project, correlation across hosts.

`uxon doctor` reports the channel state on its own line:

```
audit:    enabled, sink=journald-native    (or sink=syslog / disabled / no-sink)
```

Disable per-host with `[audit]\nenabled = false` in `config.toml`;
there is no environment-variable override.  Query via
`journalctl SYSLOG_IDENTIFIER=uxon`.

#### Common queries

On the journald-native sink every envelope field is a first-class
`FIELD=value` selector (uppercased — that's the journald wire
convention).  On the `/dev/log` syslog fallback the body lands as
`@cee: {…}` JSON — the same fields are reachable via `-o json | jq`.

```bash
# Everything uxon emitted today
journalctl SYSLOG_IDENTIFIER=uxon --since today

# Everything one operator did today (caller_user is the human's
# login, before any sudo -iu)
journalctl SYSLOG_IDENTIFIER=uxon CALLER_USER=alice --since today

# All denied / errored / not-found gestures across the fleet
journalctl SYSLOG_IDENTIFIER=uxon -o json | \
  jq -c 'select(.OUTCOME != "ok") | {ts:.TS, event:.EVENT, outcome:.OUTCOME, caller:.CALLER_USER, target:.TARGET_USER, session:.SESSION}'

# Trace one cross-host operation end-to-end by correlation_id (the
# UUID on the caller's *.remote.out matches the peer's *.remote.in)
journalctl SYSLOG_IDENTIFIER=uxon CORRELATION_ID=8f3c2d4e-1a6b-4c5e-9f7d-0a1b2c3d4e5f

# Kill-all gestures (and what they hit) for the last week
journalctl SYSLOG_IDENTIFIER=uxon EVENT=session.kill_all --since "7 days ago" -o json | \
  jq -c '{ts:.TS, caller:.CALLER_USER, users:.TARGET_USERS, killed:.KILLED_COUNT, dry_run:.DRY_RUN}'

# Live tail (follow new events as they arrive)
journalctl SYSLOG_IDENTIFIER=uxon -f
```

All recipes hit the **system** journal: ``audit.py`` connects to
``/run/systemd/journal/socket`` regardless of whether the caller is
root or a regular user.  Do **not** add ``--user`` — that flag scopes
to the per-user systemd-journal namespace, which uxon never writes to,
and it would silently return zero rows.  On hosts where users can't
read the system journal, add the caller to the ``systemd-journal``
group (or query as root).

`install/install_uxon.py` creates a dedicated venv at `--venv-dir`
(default `/opt/uxon/venv`), `pip install`s the package into it, and
symlinks `/opt/uxon/venv/bin/uxon` to `--install-path`. Dependencies
(`textual`, `tomlkit`) are pulled into the venv automatically — no
system-Python pollution.

If `uv` is available you can skip the script and use it directly:

```bash
sudo uv tool install --force git+https://github.com/vzd3v/uxon.git@<tag>
# uv places the entrypoint in /root/.local/bin or similar; symlink as needed
```

## Multi-host topology

When `uxon` runs on more than one host, decide up front:

- **Canonical install location.** Pick one venv path, e.g.
  `/opt/uxon/venv`, and use it on every host. `/usr/local/bin/uxon`
  stays a symlink into that venv. The `install_uxon.py` defaults match
  this convention.
- **One source of config truth per host.** The repo ships
  `config/config.example.toml` as a starting point; host-local
  `config/config.toml` is gitignored and operator-owned.
- **Pinned ref.** Deploy a tag or commit, not `main`, when you want
  determinism. Verify with `uxon --version`.

The infra repo / Ansible / Salt / whatever you use may:
- clone or update this repo on each host;
- pick a target ref;
- hand a host-specific JSON payload to
  `install/render_uxon_config.py` to generate `config.toml`;
- own ACLs on the editable checkout (group-writable for admins,
  read-only for everyone else).

The infra repo **must not** become a second canonical location for:
- the `uxon` executable;
- the config schema;
- config-rendering logic.

## Runtime dependencies

- **Python ≥ 3.11.** Stdlib `tomllib` is used for config reads.
- **`textual >= 0.80, < 9`** and **`tomlkit`** — hard runtime
  dependencies, pulled in automatically by `uv tool install` /
  `pipx install` / `pip install` / `install/install_uxon.py`. No
  manual setup needed. `textual` is lazy-imported inside the TUI
  entrypoint, so non-TUI subcommands (`list`, `doctor`, `run`, `new`,
  `attach`, `kill`) run even on a stripped Python without `textual`
  importable.
- **`gh` CLI.** Required on hosts that use `auth = "gh"` git-remote
  profiles. Run `gh auth login` once as the configured `creds_user`.

## Config contract

These keys steer rollout behaviour and deserve explicit values per
host:

- `repeat_noninteractive_mode` — `fail` (default), `attach`, or
  `new`. Keep `fail` unless the host explicitly wants unattended
  attach/new.
- `tmux_socket_template` — absolute per-user socket template
  (default `/tmp/uxon-{user}.sock`; supports `{user}` and `{uid}`).
  Keep the default unless a different absolute path is required.
- `allowed_roots` — when empty, `uxon run` and the TUI's
  "new session in current folder" gate on write access alone (any
  writable folder works). When non-empty, switches both to strict
  whitelist — only paths under one of the listed directories are
  accepted, with no `$HOME`-implicit allowance. `uxon new`
  (creating a project) always requires a non-empty whitelist that
  covers `new_project_root`.
- `new_project_root` — base directory for `uxon new <name>` (default
  `~/projects`). Must be inside an `allowed_roots` entry.

### Git-remote profiles

`git_create_enabled`, `default_git_remote_profile`, and
`[[git_remote_profiles]]` are **hand-edited** in `config.toml` —
they are intentionally not part of the
`install/render_uxon_config.py` JSON-to-TOML flow because profiles
reference `creds_user` and `token_file`, and infra shouldn't
hard-code those across hosts. The TUI shows them read-only. See
[`docs/configuration.md` § GitHub repo creation on new project](configuration.md#use-case-github-repo-creation-on-new-project)
for field reference and examples.

## Verification checklist

Run after each rollout:

1. `uxon --version` — matches the deployed ref.
2. `uxon doctor` — clean (includes per-profile status for any
   configured `[[git_remote_profiles]]`, read-only probe).
3. Plain `uxon -n <throwaway>` — creates project, attaches.
4. Worktree `uxon -n <throwaway> -w <branch>` — succeeds when the
   directory already contains a git repo with that branch.
5. `uxon kill-all --dry-run` — prints the plan; `uxon kill-all` (with
   confirmation) actually kills.
6. Reported dedicated socket path matches the deployed config.
7. If git-remote profiles are enabled:
   `uxon -n <throwaway> --git-remote <profile> --dry-run` prints the
   full command plan without executing.

## Multi-host

`uxon` can poll peer machines over SSH and surface their session
lists alongside the local ones — both in the TUI's "Remote
sessions" block and via the CLI (`uxon list --host`,
`uxon list --all-hosts`).

The model is **read-mostly aggregation**. Each peer runs its own
`uxon` install with its own users, sockets, allowed-roots, and
agents; the local machine only reads what `uxon list --json`
returns over SSH. There is no shared state, no cluster
coordinator, no remote auth handshake — just `ssh <alias> uxon
list --json` parsed locally.

### Configuration

Add one `[[remote_hosts]]` block per peer to `config/config.toml`:

```toml
[[remote_hosts]]
name = "vz-prod1"            # required, ASCII; cache filename + UI label
ssh_alias = "vz-prod1"       # required, passed verbatim to ssh
description = "primary EU"   # optional, shown in TUI tooltips
remote_uxon = "uxon"         # optional, default "uxon"
```

Required fields: `name` and `ssh_alias`. `name` must be unique
across the array and match `[A-Za-z0-9_.-]+` — it ends up in a
filename (see Cache below). `description` is free-form; an unknown
key in the block is rejected at config load with a clear error so
typos like `ssh_alaias` fail loud rather than silently disabling
the host.

`uxon doctor` does not currently probe remote hosts; that block is
intentionally read-only here so a TUI startup can surface peer
data without `doctor` triggering an SSH wave.

### SSH config is the source of truth

`uxon` deliberately does not accept `ssh_user`, `port`,
`identity_file`, or `proxy_command` in `[[remote_hosts]]`. Put
those in `~/.ssh/config` for the launch user instead:

```
Host vz-prod1
    HostName 10.0.0.42
    User uxonops
    IdentityFile ~/.ssh/id_ed25519_uxon
    ProxyJump bastion.example.org
    # Reuse one TCP connection per peer for the periodic poller:
    ControlMaster auto
    ControlPath  ~/.ssh/cm-%r@%h:%p
    ControlPersist 10m
```

`ControlMaster` / `ControlPersist` are recommended whenever more
than two or three peers are configured. Without connection
multiplexing every refresh tick opens a fresh TCP + auth handshake
to every peer, which is slow on first paint, noisy in the peer's
`auth.log`, and a measurable battery drain on the operator's
laptop. With multiplexing each peer keeps one warm SSH socket and
the per-tick command is dispatched over it.

The collector runs the literal command:

```
ssh -o BatchMode=yes -o ConnectTimeout=5 -o ServerAliveInterval=5 \
    <ssh_alias> '<remote_uxon>' list --all-users --json
```

`BatchMode=yes` forbids password / TOFU prompts — the operator's
agent must already hold the relevant key, and the host key must
already be known. `StrictHostKeyChecking` is left at the user's
configured default; if you want first-connect auto-accept, set
`StrictHostKeyChecking accept-new` in the per-host `ssh_config`
stanza.

### Operator view across hosts

`--all-users` makes the peer enumerate *its own* reachable users for
the SSH user — same per-target sudo gate as the local TUI. Two
config requirements must hold on the peer for cross-user sessions
to come back over the wire:

1. The peer's `config.toml` sets `enable_all_users_list = true`.
2. The SSH user has passwordless sudo (per-target NOPASSWD or root
   NOPASSWD) to the launch users in the peer's `session_users`.

**Supervision without impersonation, on each peer.** Requirement (2)
targets the peer's `*_agent` launch users, not the developers' shell
accounts on that peer. An operator account `opsuser` with
`opsuser ALL=(alice_agent,bob_agent) NOPASSWD: ALL` on the peer can
list, attach to, and `kill --host <peer> --user alice_agent` — but
cannot become `alice` on that machine. Each peer evaluates its own
sudoers independently; there is no implicit trust delegation across
hosts. See [`docs/configuration.md` § Operator view](configuration.md#operator-view-who-sees-whose-sessions)
for the same property locally.

If the peer's config has `enable_all_users_list = false`, the peer
exits with code 1 and the stable stderr tag
`uxon-error: all-users-disabled`. The collector detects that tag
and retries once with the legacy `list --json` (own-only) command,
stamping the snapshot with `scope_limited = True`. The TUI labels
that peer `(own only)` in the remote-sessions block — single-host
case appends to the section header, multi-host case appends to the
peer's name in the HOST column. No silent partial data: the badge
is always shown when a peer's view is degraded.

Anything other than the documented marker is a hard error → cache
fallback path. Operators fix peer config and retry.

### Cache

The last successful payload per peer is cached at:

```
${XDG_STATE_HOME:-~/.local/state}/uxon/remote/<name>.json
```

The directory is created with mode `0o700`. The file is rewritten
atomically (temp + rename); a write failure is best-effort
cleaned up so the directory does not accumulate `.tmp` orphans.

When a live fetch fails, the collector falls back to the cache and
returns the last-good sessions with a `from_cache=True` marker so
the TUI can show "(stale)" hints. The disk file is **only**
written by a fresh successful fetch — a failed poll never
overwrites the last good data.

The cache file's `mtime` is the snapshot age. The TUI currently
surfaces only `(stale)` when serving from cache — the absolute
age (e.g. "snapshot 14m old") is not yet rendered in the UI; an
operator who needs it reads `stat ~/.local/state/uxon/remote/<name>.json`
directly. Surfacing snapshot age in the TUI is a known follow-up:
the difference between "the runaway is gone" and "the SSH path
is broken and we are looking at half-hour-old data" matters for
incident response.

### Wire schema

The collector consumes the same wire envelope `uxon list --json`
emits locally:

```json
{
  "schema_version": "1",
  "uxon_version": "<peer's uxon version>",
  "kind": "list",
  "data": {
    "all_users": true,
    "scope_users": ["alice_agent", "bob_agent"],
    "scope_skipped": ["carol_agent"],
    "session_prefix": "uxon-",
    "sessions": [...]
  }
}
```

A `schema_version` mismatch between peers fails the parse loud
rather than silently dropping fields — bump the local install
when peers are upgraded. `data.scope_skipped` is optional — older
peers without per-target sudo support omit it; the collector
treats missing/null as `[]`.

**Internal flag — `--audit-correlation-id`.**  The peer-protocol
contract for `list`, `attach`, and `kill` accepts an internal
`--audit-correlation-id <uuid>` flag.  The local side generates a
UUIDv4 and passes it to the peer; the peer stamps it into its own
audit trail (`attach.remote.in`, `kill.remote.in`, `list.remote.in`)
so an operator chasing a cross-host event can join both records by
ID.  The flag is hidden from `--help` because it is not a public
knob.  Peers within a rolling-upgrade window must run the same
version (existing wire-schema posture); peers that don't recognise
the flag reject it as an unknown argument.

### CLI

```bash
uxon list --host vz-prod1            # human table for one peer
uxon list --host vz-prod1 --json     # machine envelope for one peer
uxon list --all-hosts                # local + every configured peer
uxon list --all-hosts --json         # JSON Lines: one envelope per source
```

Exit codes:

- `0` — fresh fetch succeeded, OR cache fallback (peer briefly
  unreachable but cache populated).
- `1` — live fetch failed AND no cache available; stderr carries
  the SSH error.
- `2` — config error: unknown `--host <name>`, mutually exclusive
  flags, no `[[remote_hosts]]` configured.

**Bulk** destructive ops are strictly local — there is no
`uxon kill-all --host <name>` and there will not be one. Reaping
every session on a peer is the operator's deliberate SSH gesture,
not something `uxon` schedules over a fan-out.

**Per-session** kill, however, *can* target a peer with `uxon kill
--host <alias> [--user <name>] <id>`. The local CLI runs `uxon
kill --force --user <name> <id>` on the peer over SSH; the peer's
own per-target sudo gating applies (NOPASSWD for `<name>` on the
peer). Requires the same SSH plumbing as `list --host`: BatchMode
is mandatory (no interactive prompts) and the peer must have
NOPASSWD `sudo -niu <name>` configured. `kill --json` is
non-interactive and refuses to run without `--force` or
`--dry-run`. The TUI mirrors this from the remote-sessions table:
pressing `k` on a row prompts for confirmation and dispatches the
same `--host`/`--user` SSH call.

### TUI

When at least one `[[remote_hosts]]` entry is configured, the
main screen renders a "── remote sessions ──" header below the
local block, with a `RemoteSessionTable` filled from the latest
snapshots. The `HOST` column appears only when more than one
peer is configured; the single-host case puts the host name in
the section header and skips the column.

Per-host pollers run on the existing pluggable refresh registry —
each peer in its own worker group, so a slow or dead peer never
stalls the local-sessions stream or another peer's poll. Cadence
is `tui_ssh_refresh_interval_seconds` (default 10s), separate
from the local-tmux cadence.

Activating a remote row (Enter) attaches to that peer's session
over SSH. Pressing `k` on a remote row dispatches the per-session
kill described above (`uxon kill --host ... --user ... <id>` over
SSH); bulk kill across hosts remains intentionally out of scope.

## Migration notes

### Audit channel (3.3.0)

The 3.3.0 release introduces a dedicated audit channel and removes
the legacy TUI event log.  Treat the items below as upgrade notes
on top of the 2.x topology already documented above.

- **TUI event log removed.** The per-day JSONL file at
  `${XDG_STATE_HOME:-~/.local/state}/uxon/tui-{user}-{date}.log` is
  no longer written.  Application-level audit now goes to the
  platform log channel (journald native, `/dev/log` syslog
  fallback).  Query via `journalctl SYSLOG_IDENTIFIER=uxon` on
  systemd hosts; on syslog-only hosts `grep '@cee:' /var/log/syslog`.
- **`uxon.tui.LOG_DIR` import removed.** Out-of-tree consumers that
  imported `from uxon.tui import LOG_DIR` will fail at import.  The
  constant still lives in `uxon.tui.events.LOG_DIR` as an internal
  detail of the developer-facing `debug` / `metrics` channels.
- **Peer protocol — `--audit-correlation-id`.** `list`, `attach`,
  `kill` now accept an internal `--audit-correlation-id <uuid>`
  flag.  Peers in a rolling-upgrade window must run the same
  version; silent fallback would lose the correlation property
  exactly when an operator is debugging across hosts.  Hidden from
  `--help`.
- **Old `tui-*.log` files left in place.** No automatic cleanup —
  operators remove the old directory manually if desired.

### 1.x → 2.0

- **Defaults moved.** `allowed_roots` defaults to `[]` and
  `new_project_root` defaults to `~/projects`. Existing deployments
  override both — no action required if your `config.toml` already
  sets them.
- **Log directory default.** The developer-facing `debug` and
  `metrics` channels default to `${XDG_STATE_HOME:-~/.local/state}/uxon`.
  Set `UXON_LOG_DIR=/old/path/here` in the launch user's environment
  to preserve the previous location.  (Audit events go to journald /
  syslog regardless — they have always honoured the OS log channel
  on hosts that have one; `UXON_LOG_DIR` only ever scoped the
  developer channels.)
- **Internal agent material untracked.** `AGENTS.md`, `CLAUDE.md`,
  `.claude/`, `docs/plans/`, `docs/superpowers/`, `docs/prototypes/`
  are no longer tracked. Operators do not need to do anything.

### Multi-agent config schema (1.3)

The flat `default_claude_args` key is removed. Config uses nested
tables:

```toml
[agents]
enabled = ["claude", "cursor"]
default = "claude"

[agents.claude]
default_args = []

[agents.codex]
default_args = []

[agents.cursor]
default_args = []
```

Manual migration per host: replace the flat
`default_claude_args = [...]` line with the nested `[agents]`
tables, include only agents installed on that host in `enabled`,
then run `uxon doctor` to verify.

If the legacy flat key is present on load, `uxon` fails with a clear
error pointing here.

# uxon

[![PyPI](https://img.shields.io/pypi/v/uxon)](https://pypi.org/project/uxon/)
[![CI](https://github.com/vzd3v/uxon/actions/workflows/ci.yml/badge.svg)](https://github.com/vzd3v/uxon/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![Platform: Linux](https://img.shields.io/badge/platform-Linux-lightgrey)

Session manager for development teams using terminal AI coding
agents (Claude Code, Codex, Cursor CLI) on one or more Linux
servers. Team visibility via OS accounts, cross-host visibility via
SSH, supervision via sudoers.

<!-- screenshot goes here -->

## When to use uxon

Use `uxon` when terminal AI coding agents are a team runtime. The
team may share one Linux server, spread work across several separate
servers, or do both. The requirement is the same: developers and
leads need one consistent way to see, attach to, start, and stop live
agent sessions without managing raw `tmux` sockets by hand on every
host.

The core workflow:

- Developers see their own running sessions, attach to one, kill
  stale or runaway sessions, start an agent in the current directory,
  create a new project under the configured project root, or pick an
  existing project from the TUI.
- Agents can run as low-privilege launch users such as
  `alice_agent`, not as the human shell users `alice` / `bob`. That
  gives each agent a managed runtime identity and keeps yolo-mode
  writes away from human dotfiles, SSH keys, shell history, and
  credentials in `$HOME`.
- Leads, seniors, instructors, and operators can see and attach to
  colleagues' live sessions where sudoers grants allow it. This is
  useful for reviewing the workflow, teaching a team workflow in
  place, helping a teammate debug a stuck session, or stopping a
  session that started doing the wrong thing.
- One TUI can show local sessions and sessions from configured remote
  hosts. Remote access uses SSH to the peer and that peer's own
  sudoers rules, so a central dashboard is not required and each host
  keeps its OS-level authority boundary.

Example: a developer connects to the host, sees eight agent sessions,
kills the five dead ones before they keep consuming CPU and budget,
reattaches to the active session that matters, then starts another
agent by choosing a project from the configured project folder. A lead
opens the same TUI and, according to their sudoers grants, sees their
own sessions plus reachable teammates' sessions on the local host and
on remote peers. They attach to a new developer's session to
demonstrate the team workflow, then jump into a colleague's session
to show how to use a new shared skill.

Solo use still works: `uxon` is a persistent TUI over `tmux` for one
person too. It exists mainly because teams need the shared version of
that loop: attribution, lifecycle, low-priv launch users, supervision,
and cross-host visibility.

## Model

Each developer is paired with a low-privilege OS account on the
host (`<user>_agent`). Agents are launched there via `sudo -iu`,
on a per-user `tmux` socket. The developer's shell account remains
the boundary that holds dotfiles, SSH keys, and credentials; the
agent's account holds the project working tree and whatever the
developer has explicitly granted it. A yolo-mode run can damage
only what the agent account can write to.

Cross-user supervision is granted through ordinary sudoers entries.
`lead ALL=(alice_agent,bob_agent) NOPASSWD: ALL` lets the lead
attach to and reap Alice's and Bob's agent sessions, but does not
grant `sudo -iu alice` or `sudo -iu bob` — the lead never becomes
the developer. Anything that requires the developer's shell-user
identity (SSH keys behind a passphrase prompt, `gh` / `aws`
sessions, an unlocked browser profile) stays out of reach via this
path.

Cross-host aggregation works the same way: each peer evaluates its
own sudoers independently. There is no central authority, no shared
state, no cluster coordinator. The local TUI polls each configured
`[[remote_hosts]]` peer over SSH and renders those sessions alongside
local sessions. Remote attach and per-session
`uxon kill --host <peer> --user <name>` route to the peer over SSH
and are gated by the peer's own sudoers. Bulk destructive operations
stay strictly local — fan-out kills are an explicit operator gesture,
not a uxon primitive.

## Fit and boundaries

`uxon` fits teams that already operate Linux hosts and want agent
session management to follow the same OS boundaries as the rest of
the host:

- launch users are regular Unix accounts;
- cross-user visibility is granted with sudoers;
- cross-host visibility is granted with SSH plus the peer's sudoers;
- the primary UI is a terminal TUI, not a web service.

The runtime has no daemon, database, or central control plane. Each
host remains independently configured and independently authorised.

Boundaries:

- `uxon` does not constrain what the agent binary does once launched.
  Anything the agent's OS account can do, the agent can do.
- `uxon` does not configure cgroups, AppArmor, seccomp, kernel
  namespaces, SSO, central RBAC, audit infrastructure, secrets
  management, or cost / token accounting.
- `uxon` has no web dashboard. The primary interface is the TUI over
  SSH or another terminal transport.

### Isolation model: OS users, not containers

`uxon` uses dedicated low-privilege Linux users (`<user>_agent`)
and `sudo -iu` rather than spinning up a container per agent
session. The tradeoff: no UID-mapping pain, no network namespace
plumbing, no auth duplication, no per-image maintenance — at the
cost of relying on regular Linux UID separation (same kernel, same
network namespace, same `/proc` unless `hidepid=2`). That suits
hosts where the threat model is "developers on this team, plus
their agents running yolo by accident", not hosts where the
developers themselves are untrusted. If you need stronger
isolation, run `uxon` inside a VM (or container) per team and keep
the OS-user model inside it. Full reasoning and the per-tradeoff
breakdown in
[SECURITY.md § Why OS users instead of containers](SECURITY.md#why-os-users-instead-of-containers).

## Install

Requires **Python 3.11+**, `tmux`, and Linux. Dependencies
(`textual`, `tomlkit`) come in automatically.

```bash
# Team / shared host (recommended): one root-owned binary in
# /usr/local/bin/uxon. Operator owns the version and the install
# path; launch users can emit audit events but cannot edit the
# binary or the trail.
sudo pipx install --global uxon

# Solo / single-owner: each OS user manages their own copy.
uv tool install uxon              # or:  pipx install uxon

uxon                              # launch the TUI; it self-diagnoses
```

The host-wide path is what makes the audit channel tamper-evident
(`uxon` and the journald / syslog sinks all root-owned), and it is
the prerequisite for the TUI's cross-user dashboard once you add
passwordless `sudo` to launch users plus a `session_users` list
(see [The TUI](#the-tui) below). The per-user path suits a single
owner who wants their own version pin and easy `uninstall` — fine
for solo, not the recommended posture on a shared host.

For the bundled installer (Ansible / Puppet rollout), PEP 668
caveats, the bootstrap config snippet, and unreleased-from-`main`
builds, see [`docs/getting-started.md`](docs/getting-started.md).
For multi-host rollout, JSON-rendered configs, and pinned refs,
see [`docs/deployment.md`](docs/deployment.md). For the **client
side** (laptop, phone, tablet) connecting to the host, see
[`docs/clients.md`](docs/clients.md).

`uxon` emits audit events to the platform log channel (journald
native on systemd hosts, `/dev/log` syslog fallback). Per-event
schema in [`docs/audit-events.md`](docs/audit-events.md); channel
topology and `journalctl` recipes in
[`docs/deployment.md`](docs/deployment.md#audit-channel).

## Quick start

```bash
uxon                  # full-screen TUI picker (recommended; needs a TTY)
```

That's the intended entry point. Everything below — creating
projects, attaching, killing, switching agents — happens inside the
TUI. Read on.

For non-interactive use (scripts, CI, SSH one-liners), the same
operations are available as subcommands; see [CLI](#cli) at the end.

---

## The TUI

`uxon` with no arguments on a TTY opens a full-screen `textual`
picker. One screen, three blocks:

### 1. Actions (top)

1. **New session in current folder** — start the default agent in
   `$PWD` (gated on the allowed-roots check).
2. **Create new project** — prompt for a name, create
   `<new_project_root>/<name>`, optionally create a GitHub remote
   (if [git-remote profiles](docs/configuration.md#use-case-github-repo-creation-on-new-project)
   are configured), launch the agent.
3. **Open existing project** — pick a directory under
   `new_project_root` and launch.

Before every launch, a permissions modal asks whether to start the
agent in normal mode or with `--dangerously-skip-permissions`
("yolo"). The TUI does not start yolo mode without this explicit
choice.

### 2. Session dashboard

A single, sortable table mounting every session you can see — your
own local sessions, other-user local sessions visible via
`sudo -niu` (when the superuser block is active), and one row per
session on each configured `[[remote_hosts]]` peer.

Per-row data:

- session name (with a per-host colour glyph in multi-host setups),
  agent, working directory;
- live CPU / RAM (refreshed every `tui_refresh_interval_seconds`);
- attached-or-not marker;
- creation time and last-activity time.

The column set adapts to the layout: a `HOST` column appears
automatically when peers are configured; a `USER` column appears
when other-user rows are present. The full set of columns and
their order is configurable via `[tui.table]` in `config.toml` —
see [`docs/configuration.md`](docs/configuration.md#use-case-dashboard-columns)
for the column ids, the registry-of-defaults rule, and
`default_sort_by`. Unknown column ids in an older config are
silently dropped (forward-compat).

`Enter` attaches (local rows attach directly; remote rows open
`ssh <alias> uxon attach …`). `d` kills the highlighted row with
confirmation — the same key works for local and remote rows
uniformly; the per-target sudo gating happens on the peer's
`uxon kill`. Bulk `kill-all` (`D`) stays local to the host where
it is invoked.

Press `s` to cycle the sort column (CPU → RAM → LAST → NAME); `S`
(Shift+s) toggles direction. The new ranking applies across local
own, local other-user, and every peer's rows in one flat list.

The remote collector is fail-soft: a dead or slow peer falls back
to the on-disk snapshot
(`~/.local/state/uxon/remote/<name>.json`) and never stalls the
local view. See
[`docs/deployment.md`](docs/deployment.md#multi-host) for the full
SSH model and config schema.

### 3. Server status (bottom)

Load average, normalised CPU load, RAM, disk, uptime. When you're
inside an SSH session, an async `ssh-link` probe shows RTT, jitter,
and retransmits — quick read on whether the lag you're feeling is
the agent or the network.

### ⚡ Superuser block (only when passwordless `sudo` is detected)

When the caller has passwordless `sudo` to one or more users in
`session_users`, the dashboard adds those users' rows under a
yellow `USER` column and a superuser block appears with three
extras:

- **Other users' sessions** in the same dashboard. `Enter` attaches
  via `sudo -niu <user>`; `d` kills.
- **⚙ Settings** — repo-level `config.toml` editor (preserves
  comments via `tomlkit` round-trip; falls back to `sudo tee` for
  root-owned files).
- **Kill ALL uxon sessions (reachable users)** — the "fire alarm",
  gated on typing `kill-all-reachable`.

Visibility is scoped to the **reachable** subset of `session_users`
— probed once at TUI startup via `sudo -niu <U> -- true`. The
section header shows `(N/M users reachable)` when not all of
`session_users` is reachable; new sudoers grants are picked up by
quitting and re-launching. Grants target the `*_agent` accounts,
not the developers' shell users — supervision without
impersonation. Full sudoers patterns and resolution order in
[`docs/configuration.md` § Operator view](docs/configuration.md#operator-view-who-sees-whose-sessions).

### Keys

| Key                | Action |
|--------------------|--------|
| `↑` `↓`            | Navigate |
| `1`–`9`            | Jump to item by number |
| `Enter`            | Activate (launch / attach) |
| `d`                | Kill highlighted session (with confirmation) |
| `D`                | Kill all *own* sessions (`kill-all` to confirm) |
| `s` / `S`          | Cycle sort column / toggle direction |
| `r`                | Refresh |
| `q` / `Esc`        | Quit (or back, in sub-screens) |

### Detach and re-enter

When the launched session exits — or you `Ctrl-b d` to detach —
`uxon` returns to the main screen with a refreshed list. The same
binary you launched is the same binary you come back to. `q` / `Esc`
on the main screen exits to the shell.

If a launch fails, the failure output stays on the physical
terminal with a banner before the TUI re-enters fullscreen, so you
can read whatever stderr the agent printed.

---

## Supported agents

| Agent id | Binary | `--auto` mode | `--dsp` (yolo) | Install |
|----------|--------|---------------|----------------|---------|
| `claude` | `claude` | `--permission-mode auto` | `--dangerously-skip-permissions` | [Anthropic docs](https://docs.claude.com/claude-code) |
| `codex`  | `codex` | `--full-auto` | `--dangerously-bypass-approvals-and-sandbox` | `npm i -g @openai/codex` |
| `cursor` | `cursor-agent` | (not supported) | `--yolo` | `curl https://cursor.com/install -fsSL \| bash` |

Enable agents in `config/config.toml`:

```toml
[agents]
enabled = ["claude", "codex"]
default = "claude"
```

`uxon doctor` probes each enabled agent and prints its path, version,
and status. If none of the enabled agents are installed for the
launch user, the TUI shows a modal with install hints.

`-w <branch>` (worktree mode) is currently claude-only.
`--auto` is unavailable for cursor.

---

## Configuration

`uxon run` and the TUI's "New session in current folder" work out of
the box with no configuration — the launch user can run an agent
anywhere they have write access. `uxon new <name>` (creating a
project) requires `allowed_roots` to be non-empty and to cover
`new_project_root`.

Configuration becomes useful once you're hosting more than one user,
or want to restrict where agents may run, or want `uxon new` to
scaffold projects, or want one-shot GitHub repo creation, or want to
switch the default agent. All keys, with the **use case** for each,
live in **[`docs/configuration.md`](docs/configuration.md)**.

Two config layers (later wins):

1. **Repo config** — `<repo>/config/config.toml`, host-wide.
   `config/config.example.toml` is the tracked starting point.
2. **Project config** — the nearest `.uxon.toml` in `cwd` or a
   parent inside an `allowed_roots` entry. Per-project overrides.
   The TUI never writes `.uxon.toml`.

---

## CLI

The TUI is the recommended entry point. The CLI exposes the same
operations for scripting, SSH one-liners, and CI. Brief summary:

| Command | What it does |
|---------|--------------|
| `uxon` | Open the interactive TUI (needs a TTY). |
| `uxon run [-w <branch>] [agent-flags...]` | Start an agent in `$PWD`. |
| `uxon new <name> [-w <branch>] [...]` | Create / reuse a project under `new_project_root` and start an agent. |
| `uxon list [--all-users] [--host <name> \| --all-hosts]` | List local sessions, reachable users, or configured peers. |
| `uxon attach <id>` | Re-attach to a session. Accepts full name, short name, bare stem, or active-pane PID. |
| `uxon kill <id> [--user <name>] [--host <name>]` | Kill one local, cross-user, or remote session. |
| `uxon kill-all [--force]` | Kill every `uxon-*` session for the current launch user. |
| `uxon doctor` | Read-only diagnostics: caller / launch user, config, allowed_roots, agents, sockets, sessions, detected issues. Run this first when something looks wrong. |
| `uxon version` | Print version + git commit. |

Short forms: `-l` / `-a` / `-k` / `-n` / `-V` / `--killall`.

Full reference with every flag, exit code, identifier-resolution
rules, repeat-behaviour, worktree details, and legacy-session notes
is in **[`docs/cli.md`](docs/cli.md)**.

---

## Troubleshooting

- **`uxon` started inside an existing `tmux`?** Handled
  transparently when `$TMUX` names the same socket as `uxon`. If
  it's a different (foreign) `tmux`, `uxon` prints an actionable
  error — `Ctrl-b d` first.
- **`textual` not installed?** The TUI prints an install hint and
  exits. The whole CLI keeps working.
- **TUI errors render as a red toast.** Tmux-gone, permission
  denied, allowed-roots mismatch, git remote failure, config-write
  conflict — all caught and shown in-place rather than crashing.
- **TUI surfaces tmux/agent issues in line.** Missing `tmux` or
  missing agent binaries trigger a preflight error from the
  CLI and an in-line hint in the TUI; freshly-installed agents are
  auto-detected and offered for one-keypress enabling. For deeper
  diagnostics see [`docs/cli.md`](docs/cli.md#doctor).

More edge cases (legacy session prefixes, failed-launch banner, the
`--dsp` flag and its legacy aliases) are documented in
[`docs/cli.md`](docs/cli.md).

---

## Documentation

- [`docs/getting-started.md`](docs/getting-started.md) — full
  install paths (per-user / host-wide / bundled installer),
  PEP 668 caveat, after-install bootstrap.
- [`docs/configuration.md`](docs/configuration.md) — all config keys
  organised by deployment scenario (solo / team × single-host /
  multi-host), plus orthogonal use cases (GitHub repo on new
  project, refresh cadence, session-prefix migration, …).
- [`docs/cli.md`](docs/cli.md) — full CLI reference (every flag,
  exit code, identifier resolution, repeat behaviour).
- [`docs/deployment.md`](docs/deployment.md) — multi-host rollout,
  config rendering from JSON, runtime dependencies, peer-host
  aggregation over SSH (`[[remote_hosts]]`, `--host`,
  `--all-hosts`).
- [`docs/clients.md`](docs/clients.md) — client-side setup:
  Eternal Terminal as the recommended SSH replacement,
  `~/.ssh/config` patterns, hardware-protected SSH keys.
- [`docs/architecture.md`](docs/architecture.md) — module map, TUI
  internals, code boundaries.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — local checks, branch
  policy, release process.
- [`SECURITY.md`](SECURITY.md) — threat model, disclosure policy.
- [`CHANGELOG.md`](CHANGELOG.md) — version history.

## Versioning

`uxon` follows [SemVer](https://semver.org/). `uxon --version`
prints the version and short git commit (with `-dirty` when the
checkout is dirty).

## License

[MIT](LICENSE) © 2026 Vasily Zakharov.

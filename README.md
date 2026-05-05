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

`uxon` uses dedicated low-privilege Linux users (`<user>_agent`) and
`sudo -iu` instead of creating a container per agent session. Docker
or Podman are stronger isolation primitives, but they add operational
cost on a shared development host:

- **Bind-mount UID mapping.** With naive `docker run` defaults, files
  created in the container come back owned by `root` (or whatever
  UID was baked in) on the host, breaking save-and-edit. Rootless
  Podman with the `:U` mount option, and rootless Docker via
  `subuid`/`subgid`, close this — at the cost of a per-host setup
  that is itself non-trivial.
- **Networking.** Anything the agent talks to on
  `localhost` (a local DB, a model proxy, an internal service, an
  `mDNS`/`.local` host) needs `host.docker.internal`,
  `--network=host`, or explicit port plumbing. SSH-agent
  forwarding needs socket bind-mounts and breaks across
  reconnects.
- **Auth duplication.** `~/.claude`, `~/.gitconfig`, `~/.aws/`,
  `known_hosts`, SSH keys — each has to be passed through, or the
  container becomes a second place to re-auth every agent.
- **Per-image maintenance.** Tool updates → image rebuilds → push or
  share. For a team that just wants "Claude Code with the
  project's deps", this is extra operational work.

OS-user isolation removes those four at the cost of relying on
Linux user separation rather than container primitives:

- **Same kernel.** A kernel-level escape from inside the agent
  binary reaches the host. Containers narrow this surface via
  default seccomp / AppArmor profiles; `<user>_agent` does not.
- **Same network namespace.** The agent can reach `127.0.0.1`
  services on the host and scan the LAN. `iptables`/`nftables`
  rules per UID can mitigate, but uxon does not configure them.
- **Same `/proc`.** Without `hidepid=2` mounted on `/proc`, every
  user can see every other user's processes (not their memory,
  but command lines and environments).

The isolation `<user>_agent` actually provides is what regular
Linux UID separation provides: the agent cannot read files outside
its UID's reach, cannot signal another UID's processes, cannot
read another user's `~/.ssh/`. That is enough when the host's
threat model is "developers on this team, plus their agents
running yolo by accident". It is not enough when you do not trust
the developers logging into the box.

If you need stronger isolation than that, run uxon itself inside a
VM (or container) per team and keep the OS-user model inside it.
The layers compose.

## Install

Requires **Python 3.11+**, `tmux`, and Linux. Dependencies (`textual`,
`tomlkit`) are pulled in automatically.

`uxon` is built for persistent Linux servers where one or several OS
users run agent sessions that need to survive disconnects. Two install
flavours, depending on whether each user installs their own copy or
the host has one shared `uxon` on `PATH` for everyone.

### Per-user install (recommended)

**Use this when** each OS user manages their own copy of `uxon` —
independently versioned, no `sudo` needed, easy `uninstall`. The
common case for solo developers and small teams where everyone
prefers full control over their tooling.

Each OS user runs one of these in their own account:

```bash
# uv tool — recommended for isolated CLI installs.
uv tool install uxon

# pipx — equivalent. Same console-script entrypoint.
pipx install uxon

# pip --user — no isolation. See PEP 668 caveat below.
pip install --user uxon
```

uv/pipx isolate `uxon` and its deps in a per-user venv; `pip --user`
puts them under `~/.local/` shared with anything else installed that
way. All three put a `uxon` console script on the user's `PATH`.
Updates: `uv tool upgrade uxon` / `pipx upgrade uxon` /
`pip install --user --upgrade uxon`.

On Debian/Ubuntu/Fedora system Python, PEP 668 blocks
`pip install --user`; use `pipx` (recommended) or
`pip install --user --break-system-packages uxon` if you know what
you're doing. With your own Python (pyenv/asdf/uv-managed) PEP 668
doesn't apply.

For unreleased changes from `main`:

```bash
uv tool install git+https://github.com/vzd3v/uxon.git
# or:  pipx install git+https://github.com/vzd3v/uxon.git
```

### Host-wide install (one `uxon` for all users on the host)

**Use this when** you administer a server where several OS users
launch agent sessions and you want them on a single shared `uxon`
in `/usr/local/bin/uxon` — one version, one update path, one place
to audit. With (a) passwordless `sudo` to other launch users
(per-target NOPASSWD on each `<user>_agent`, or root NOPASSWD) and
(b) those users listed in `session_users` in `config.toml`, the
operator additionally **sees and can attach to those users'
sessions** from the same TUI (the Superuser block, described under
[The TUI](#the-tui) below) — visibility is scoped to the users you
can actually sudo into. Missing either piece — no `sudo`, or empty
`session_users` — and every OS user sees only their own
sessions. Each OS user keeps their own `tmux` socket and
their own `uxon-*` sessions; only the binary is shared.

```bash
# Simple: pipx as a system installer (pipx 1.5+).
sudo pipx install --global uxon
# Updates: sudo pipx upgrade --global uxon
```

```bash
# Explicit: bundled installer. Useful for fleet rollout (Ansible /
# Puppet) and when ops conventions pin paths like /opt/uxon/venv.
git clone https://github.com/vzd3v/uxon.git
cd uxon
sudo python3 install/install_uxon.py \
  --repo-dir "$(pwd)" \
  --install-path /usr/local/bin/uxon
# (uses /opt/uxon/venv by default; override with --venv-dir)
# Updates: re-run with --reinstall
```

Both isolate `uxon`'s Python deps in a dedicated venv and put the
console script on `PATH` via a `/usr/local/bin/uxon` shim. Don't use
`sudo pip install uxon` — it dumps `textual` / `rich` / `tomlkit` /
etc. into the system Python `site-packages` and conflicts with the
distro's package manager (this is what PEP 668 protects against).

For multi-host rollout, generated config from a JSON payload, and
deployment topology, see [`docs/deployment.md`](docs/deployment.md).

### After install

```bash
uxon                              # launch the TUI; it self-diagnoses
# Optional: bootstrap an example config. The file ships as a working
# "solo on a single host" config — works as-is, no edits needed.
# Uncomment a scenario block at the bottom for team / multi-host setups.
curl -fsSL https://raw.githubusercontent.com/vzd3v/uxon/main/config/config.example.toml -o ./config.toml
```

For deeper, scriptable host inspection see
[`docs/cli.md`](docs/cli.md#doctor).

You'll also need at least one of the agent CLIs installed for the
launch user — see [Supported agents](#supported-agents).

For the **client side** (your laptop, phone, tablet) — connecting
to the host so that sessions actually survive disconnects — see
[`docs/clients.md`](docs/clients.md). The short version: prefer
Eternal Terminal (`et`) over bare `ssh`; put hosts in
`~/.ssh/config`; use a hardware-protected SSH key.

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

### 2. Your sessions

Live list of `uxon-*` sessions for the current user with:

- session name, agent, working directory;
- live CPU / RAM (refreshed every `tui_refresh_interval_seconds`);
- attached-or-not marker;
- creation time and last-attach time.

`Enter` attaches. `d` kills (with confirmation). `D` kills *all your
own* sessions after typing `kill-all`.

### 3. Remote sessions (only when `[[remote_hosts]]` is configured)

A separate `── remote sessions ──` block aggregates `uxon list --json`
output from peer hosts over SSH. One section per host, with an extra
`HOST` column when more than one peer is configured. The collector is
fail-soft: a dead or slow peer falls back to the on-disk snapshot
(`~/.local/state/uxon/remote/<name>.json`) and never stalls the local
view. `Enter` attaches to the highlighted remote session; `k` kills
one highlighted remote session through the peer's own `uxon kill`
gate. Bulk `kill-all` remains local to the host where it is invoked.
See [`docs/deployment.md`](docs/deployment.md#multi-host) for the
full SSH model and config schema.

### 4. Server status (bottom)

Load average, normalised CPU load, RAM, disk, uptime. When you're
inside an SSH session, an async `ssh-link` probe shows RTT, jitter,
and retransmits — quick read on whether the lag you're feeling is
the agent or the network.

### ⚡ Superuser block (only when passwordless `sudo` is detected)

Visibility falls out of your sudo rules per-target. At TUI startup
`uxon` probes `sudo -niu <U> -- true` for each user in
`session_users` and shows the block scoped to the **reachable**
subset:

- **`<caller> ALL=(ALL) NOPASSWD: ALL`** (root NOPASSWD) — full
  block, every user in `session_users` listed.
- **`<caller> ALL=(alice_agent,bob_agent) NOPASSWD: ALL`**
  (per-target NOPASSWD) — block shows alice/bob; the section header
  carries a `(2/N users reachable)` hint when `session_users` lists
  more. Note the grant targets `*_agent` accounts, **not** `alice` /
  `bob` shell users — the operator gets visibility into the agents'
  tmux sockets without the ability to log in as the developers
  themselves.
- **No passwordless sudo** — block hidden; you see only your own
  sessions.

The probe runs **once at startup**; new sudoers grants are picked
up by quitting (`q`) and re-launching `uxon`.

Inside the block:

- **Other users' sessions** with a yellow `USER` column. `Enter`
  attaches via `sudo -niu <user>` (read-only-ish — you're a guest
  in their tmux); `d` kills the highlighted one.
- **⚙ Settings** — repo-level `config.toml` editor. Bool keys
  toggle, enums cycle, strings open an input, arrays use
  comma-separated input. Saves rewrite the file via a `tomlkit`
  round-trip (using `sudo tee` automatically when needed) and
  preserve untouched comments and formatting. Visible only when the
  caller has root NOPASSWD or the file is locally writable.
  Project-level `.uxon.toml` keys are read-only here — edit them in
  the project.
- **Kill ALL uxon sessions (reachable users)** — appears when at
  least one session exists across reachable users; requires typing
  `kill-all-reachable` to confirm. The "fire alarm" button. Acts
  only on users you can sudo into.

### Keys

| Key                | Action |
|--------------------|--------|
| `↑` `↓` / `j` `k`  | Navigate |
| `1`–`9`            | Jump to item by number |
| `Enter`            | Activate (launch / attach) |
| `d`                | Kill highlighted session (with confirmation) |
| `D`                | Kill all *own* sessions (`kill-all` to confirm) |
| `r`                | Refresh |
| `g` / `G`          | Jump to first / last |
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

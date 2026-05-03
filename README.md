# uxon

[![PyPI](https://img.shields.io/pypi/v/uxon)](https://pypi.org/project/uxon/)
[![CI](https://github.com/vzd3v/uxon/actions/workflows/ci.yml/badge.svg)](https://github.com/vzd3v/uxon/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![Platform: Linux](https://img.shields.io/badge/platform-Linux-lightgrey)

**uxon is the team-scale control plane for terminal AI coding agents.**
Run Claude Code, Codex, and Cursor across shared Linux servers and a
fleet of hosts — with per-user OS isolation, sysadmin-grade operator
control, and a TUI that fits a phone over SSH.

Where personal session managers stop ("you and your laptop"), `uxon`
picks up: multiple developers, multiple hosts, one safe shared runtime.

<!-- screenshot goes here -->

## Why uxon

A team-scale control plane is a different problem from a personal
session picker. The matrix below compares `uxon` to the four most
active personal managers (READMEs read May 2026):

| Capability | **uxon** | [Claude Squad](https://github.com/smtg-ai/claude-squad) | [agent-deck](https://github.com/asheshgoplani/agent-deck) | [Agent of Empires](https://github.com/njbrake/agent-of-empires) | [CCManager](https://github.com/kbwo/ccmanager) |
|---|---|---|---|---|---|
| Multiple agents on one host | ✓ | ✓ | ✓ | ✓ | ✓ |
| Persistent `tmux` sessions | ✓ | ✓ | ✓ | ✓ | — (no tmux by design) |
| Multi-host SSH aggregation | ✓ | — | ✓ | — | — |
| **Multi-user on one box** (per-user OS isolation) | ✓ | — | — | — | — |
| **Operator view across users** (`sudo` attach, `kill-all-global`) | ✓ | — | — | — | — |
| **Low-priv `runtime_user` agent sandbox** | ✓ | — | — | — | — |
| Docker-based sandbox | — (deliberate, see below) | — | optional | optional | partial (devcontainer) |
| Web dashboard | — | — | — | ✓ | — |
| Phone access over SSH | TUI | — | Telegram/Slack relay | web (HTTPS + QR) | — |
| Git worktrees | claude-only | ✓ | ✓ | ✓ | ✓ |

Multi-host alone isn't unique — `agent-deck` does it too. The
combination that **only `uxon` ships today** is the three bold rows:
multi-user OS isolation, sysadmin operator view, and the low-priv
agent runtime — all on the same shared Linux host.

**Use the right tool for the shape of your fleet.**

- Whole fleet is your laptop, want polished worktree UX → **Claude Squad** or **CCManager**.
- Want a web dashboard, optional Docker sandboxing, mobile via web → **Agent of Empires**.
- Want chat-based remote control of your own machine → **agent-deck**.
- Administer a box several developers share, or several boxes a team shares, and need to see and reap runaway agents across all of them → **`uxon`**.

### Sandbox model: OS users, not containers

Two of the four competitors offer Docker-based sandboxing as an
option. `uxon` deliberately doesn't — it pushes you toward a
dedicated low-privilege Linux user (`runtime_user`) and `sudo -iu`
into it. The trade-off is honest: Docker is a real isolation primitive
and most dev machines have it installed. For a small team sharing a
Linux box the daily friction of containerised agents is also real:

- **Bind-mount UID tape.** Files created in the container come back
  owned by `root` (or whatever UID was baked in) on the host, breaking
  save-and-edit. Cleanly fixing it means per-machine images with a
  matching UID/GID — or rootless mode — neither of which is free.
- **Networking gymnastics.** Anything the agent talks to on
  `localhost` (a local DB, a model proxy, an internal service, an
  `mDNS`/`.local` host) needs `host.docker.internal`,
  `--network=host`, or explicit port plumbing. SSH-agent forwarding
  needs socket bind-mounts and breaks across reconnects.
- **Auth duplication.** `~/.claude`, `~/.gitconfig`, `~/.aws/`,
  known_hosts, SSH keys — each has to be passed through, or the
  container becomes a second place to re-auth every agent.
- **Per-image churn.** Tool updates → image rebuilds → push or share.
  For a team that just wants "Claude Code with the project's deps,"
  this is a maintenance loop with no payoff.

OS-user isolation removes all four: the developer is already
authenticated as themselves, `runtime_user` is a separate Linux
account bounded with `sudo`/`pam_limits`/quotas, networking is the
host's networking, SSH-agent forwarding is `ForwardAgent yes`. The
cost is that `runtime_user` lives in the host's kernel namespace —
fine when you trust the developers logging into the box, deliberately
not fine if you don't.

If you need stronger isolation than that, run `uxon` itself inside a
VM (or container) per team and keep the OS-user model inside it. The
layers compose; one replaces the other only if you accept the
friction.

---

## What you get

- **Multi-user on one box.** Every launch user runs on a dedicated
  socket at `/tmp/uxon-<user>.sock` with their own authenticated
  agents and quotas; nobody sees another user's `tmux` by accident.
- **Operator visibility.** With per-target sudo (or root NOPASSWD)
  to listed `session_users`, the TUI shows every reachable user's
  sessions with CPU/RAM/age and last-attach. `Enter` attaches as a
  guest, `d` kills any session you can sudo to (locally or via
  `--host` for a peer), `kill-all-reachable` reaps every reachable
  user on the local box — one screen, every runaway process, no SSH
  tour. The probe is one-shot at startup; new sudo grants →
  restart `uxon`.
- **Multi-host aggregation.** `[[remote_hosts]]` blocks turn the same
  TUI into a fleet view: per-peer remote-sessions table over SSH,
  fail-soft snapshot cache, no cluster coordinator. Destructive
  actions stay local — the SSH gesture for `kill` is deliberate.
- **Attach from anywhere.** Sessions live in `tmux`, so they survive
  every disconnect. Start at your desk, reattach from your phone on
  the train, switch to a tablet — same session, same state. The TUI
  is keyboard-only and fits a small screen.
- **One tool, every agent.** Flip a config switch to enable Claude
  Code, Codex, Cursor — together or any subset. `--dsp`
  ("yolo") and `--auto` map to each agent's native equivalent;
  predictable session names (`uxon-<project>@<agent>`) replace
  hand-rolled `tmux new -s` strings.

Configuration use cases — deployment scenarios (solo / team ×
single-host / multi-host), strict-whitelist mode, sandbox launch
user, per-project overrides, GitHub repo creation on new project —
live in [`docs/configuration.md`](docs/configuration.md).

---

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
# uv tool — recommended. uv is the 2026 default Python toolchain;
# fast, uses uv-managed Python and a shared dep cache.
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
`session_users` — and every OS user is sandboxed to their own
sessions only. Each OS user keeps their own `tmux` socket and
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
("yolo"). No accidental yolos.

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
view. Destructive actions stay local — there is no remote `kill` or
`kill-all`. See [`docs/deployment.md`](docs/deployment.md#multi-host)
for the full SSH model and config schema.

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
  more.
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
| `uxon list [--all-users]` | List `uxon-*` sessions for this user (or all configured users). |
| `uxon attach <id>` | Re-attach to a session. Accepts full name, short name, bare stem, or active-pane PID. |
| `uxon kill <id>` | Kill one session. |
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
  missing agent binaries trigger a friendly preflight error from the
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

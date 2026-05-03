# uxon

[![PyPI](https://img.shields.io/pypi/v/uxon)](https://pypi.org/project/uxon/)
[![CI](https://github.com/vzd3v/uxon/actions/workflows/ci.yml/badge.svg)](https://github.com/vzd3v/uxon/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![Platform: Linux](https://img.shields.io/badge/platform-Linux-lightgrey)

**Run terminal AI coding agents (Claude Code, Codex, Cursor) on a shared
server — safely, persistently, and visibly to the operator.**

`uxon` is a small `tmux` wrapper with a full-screen TUI session picker.
It standardises session names, isolates each user on a dedicated `tmux`
socket, and lets you start, attach, monitor, and kill agent sessions
from one screen — your laptop, your phone over SSH, or a sysadmin's
shell with `sudo`.

<!-- screenshot goes here -->

## What it solves

- **Sandboxed agent runs.** Drop the agent into a low-privilege OS
  account with a restricted filesystem view via `sudo -iu`, while you
  keep operating from your own login. The agent can't reach what its
  user can't reach.
- **Many users on one box.** Each developer logs into their own OS
  user, runs their own authenticated `claude` / `codex` / `cursor`
  with their own keys and quotas, and never sees another user's tmux
  sessions by accident — every launch user gets a dedicated socket
  at `/tmp/uxon-<user>.sock`.
- **Operator visibility and control.** With passwordless `sudo` to
  the launch users listed in `session_users`, the operator opens
  `uxon` and sees every agent session of those users — their own
  *and* others' — with CPU, RAM, age, last attach, attached-or-not.
  **`Enter` attaches to any of those sessions** (you join their
  `tmux` as a guest via `sudo -iu`), `d` kills a runaway, and
  `kill-all-global` reaps every session of every listed
  `session_user` after explicit confirmation. No more "who's that
  38 GB python on the dashboard?".
- **Attach from anywhere.** Sessions live in `tmux`, so they survive
  every disconnect. Start at your desk, reattach from your phone over
  SSH on the train, switch to a tablet later — same session, same
  state. The TUI is keyboard-only and fits a small screen.
- **Predictable session names.** `uxon-<project>@<agent>` (`-2`,
  `-3` for parallels). No more hand-rolled `tmux new -s` strings or
  guessing what you called it yesterday.
- **Permissive defaults, strict-whitelist for ops.** With
  `allowed_roots = []` (default), the TUI's "new session in current
  folder" and `uxon run` (CLI) both launch anywhere the launch user
  can write. With `allowed_roots = [...]` set, both switch to
  strict whitelist — only those paths are accepted, with no
  `$HOME`-implicit or any other side allowance. `allowed_roots`
  also bounds `uxon new` (creating a new project directory). To
  restrict what the agent can reach on disk regardless of where
  `uxon` is invoked, sandbox launches under a low-privilege OS user
  via `runtime_user`. See
  [`docs/configuration.md`](docs/configuration.md).
- **One tool, every agent.** Flip a config switch to enable Claude
  Code, Codex, Cursor — together or any subset. Built-in `--dsp`
  (skip-permissions / "yolo") flag maps to each agent's native
  equivalent across all three; `--auto` does the same for `claude`
  and `codex` (cursor has no auto mode and errors out).
- **Optional niceties.** Git worktrees (currently `claude`-only —
  `codex` and `cursor` error if you pass `-w`), GitHub repo creation
  on a new project (with a strict whitelist of named profiles),
  per-project config overrides. All off by default.

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
to audit. With (a) passwordless `sudo` to other launch users and
(b) those users listed in `session_users` in `config.toml`, the
operator additionally **sees and can attach to those users'
sessions** from the same TUI (the Superuser block, described under
[The TUI](#the-tui) below). Missing either piece — no `sudo`, or
empty `session_users` — and every OS user is sandboxed to their
own sessions only. Each OS user keeps their own `tmux` socket and
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
# Optional: bootstrap an example config (uxon also runs with defaults).
curl -fsSL https://raw.githubusercontent.com/vzd3v/uxon/main/config/config.example.toml -o ./config.toml
$EDITOR ./config.toml             # set allowed_roots, session_users, agents
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

Appears below your own sessions whenever the host trusts you with
`sudo`:

- **Other users' sessions** with a yellow `USER` column. `Enter`
  attaches via `sudo -iu <user>` (read-only-ish — you're a guest in
  their tmux); `d` kills the highlighted one.
- **⚙ Settings** — repo-level `config.toml` editor. Bool keys
  toggle, enums cycle, strings open an input, arrays use
  comma-separated input. Saves rewrite the file via a `tomlkit`
  round-trip (using `sudo tee` automatically when needed) and
  preserve untouched comments and formatting. Project-level
  `.uxon.toml` keys are read-only here — edit them in the project.
- **Kill ALL uxon sessions (all users)** — appears when at least
  one session exists anywhere; requires typing `kill-all-global`
  to confirm. The "fire alarm" button.

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
  organised by use case (single-user laptop, shared multi-user host,
  restricted launch directories, GitHub repo on new project, …).
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

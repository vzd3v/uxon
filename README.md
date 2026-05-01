# uxon

[![CI](https://github.com/vzd3v/vz_devagent_cli_tool/actions/workflows/ci.yml/badge.svg)](https://github.com/vzd3v/vz_devagent_cli_tool/actions/workflows/ci.yml)
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
- **Operator visibility.** Anyone with passwordless `sudo` opens
  `uxon` and immediately sees every agent session on the host:
  CPU, RAM, age, last attach, attached-or-not. Attach to any of
  them with `Enter`, kill a runaway with `d`, nuke the lot with
  a confirmed `kill-all-global`. No more "who's that 38 GB python
  on the dashboard?".
- **Attach from anywhere.** Sessions live in `tmux`, so they survive
  every disconnect. Start at your desk, reattach from your phone over
  SSH on the train, switch to a tablet later — same session, same
  state. The TUI is keyboard-only and fits a small screen.
- **Predictable session names.** `uxon-<project>@<agent>` (`-2`,
  `-3` for parallels). No more hand-rolled `tmux new -s` strings or
  guessing what you called it yesterday.
- **Allowed-roots safety net.** `uxon` refuses to start an agent in
  a directory you didn't whitelist. Typos and `cd ..` accidents stop
  there.
- **One tool, every agent.** Flip a config switch to enable Claude
  Code, Codex, Cursor — together or any subset. Built-in `--auto`
  and `--dsp` (skip-permissions / "yolo") flags translate to whatever
  the underlying agent uses today.
- **Optional niceties.** Git worktrees, GitHub repo creation on a new
  project (with a strict whitelist), per-project config overrides.
  All off by default.

---

## Install

Requires **Python 3.11+**, `tmux`, and Linux. Optional: `textual` for
the TUI, `tomlkit` for in-TUI settings edits.

```bash
git clone https://github.com/vzd3v/vz_devagent_cli_tool.git
cd vz_devagent_cli_tool

# 1. Symlink the entrypoint into PATH (root-writable location).
sudo python3 install/install_uxon.py \
  --repo-dir "$(pwd)" \
  --install-path /usr/local/bin/uxon

# 2. Create the host config from the example and edit it.
cp config/config.example.toml config/config.toml
$EDITOR config/config.toml      # set allowed_roots, session_users, agents

# 3. Install the TUI (recommended). Without it, only the CLI works.
pip install 'textual>=0.80,<9' tomlkit
#   or, distro-managed:  apt install python3-tomlkit  +  pipx install textual

# 4. Verify.
uxon doctor
```

That's it — the installer just creates a symlink, there's no Python
package to build. To install for a single user without `sudo`, point
`--install-path` at `~/.local/bin/uxon` instead.

You'll also need at least one of the agent CLIs installed for the
launch user — see [Supported agents](#supported-agents).

For multi-host rollout, generated config from a JSON payload, and
deployment topology, see [`docs/deployment.md`](docs/deployment.md).

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
   (if [git-remote profiles](#git-remote-on-new-project) are
   configured), launch the agent.
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

### 3. Server status (bottom)

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

Two layers, merged in order (later wins):

1. **Repo config** — `<repo>/config/config.toml`, host-wide.
   `config/config.example.toml` is the starting point checked into
   the repo.
2. **Project config** — the nearest `.uxon.toml` in `cwd` or a parent
   that is itself inside an `allowed_roots` entry. Useful to override
   one or two keys per project. The TUI never writes `.uxon.toml`.

The keys you'll most likely touch:

| Key | Type | Default | Purpose |
|-----|------|---------|---------|
| `allowed_roots` | array | `[]` | Directories `uxon` may launch in. The launch user's home is implicitly allowed. |
| `new_project_root` | string | `~/projects` | Base directory for `uxon new <name>`. Must live inside an `allowed_roots` entry (or under `$HOME`). |
| `session_users` | array | `[]` | Users scanned by `list --all-users` and the TUI superuser block. |
| `default_launch_mode` | `"caller"` / `"fixed"` | `"caller"` | Default for unmapped callers. |
| `runtime_user` | string | `""` | Launch user when `default_launch_mode = "fixed"`. |
| `launch_user_by_caller` | table | `{}` | Per-caller override of the launch user. |
| `agents.enabled` | array | `["claude"]` | Ordered list of enabled agent ids. |
| `agents.default` | string | `"claude"` | Agent picked when `--agent` is not passed. |
| `repeat_noninteractive_mode` | `"fail"` / `"attach"` / `"new"` | `"fail"` | Non-TTY fallback when `uxon new` finds an existing matching session. |
| `git_create_enabled` | bool | `false` | Master switch for GitHub repo creation on new project. |

The full list (sockets, refresh intervals, git-remote profiles, env
overrides) lives in
[`config/config.example.toml`](config/config.example.toml) with
inline comments, and in [`docs/deployment.md`](docs/deployment.md).

### Multi-user / launch user

`uxon` distinguishes the **caller** (who invoked the command) from
the **launch user** (who actually owns the tmux session and runs
the agent). Resolution order:

1. `launch_user_by_caller[<caller>]` — explicit per-caller override;
2. `default_launch_mode = "caller"` → caller is the launch user;
3. otherwise → `runtime_user`.

When the two differ, `uxon` runs `tmux`, `git`, and `mkdir` under
the launch user via `sudo -iu <user>`. Each launch user gets a
private `tmux` socket — sessions never bleed between users.

### Git remote on new project (optional)

Off by default. When `git_create_enabled = true` and you've defined
`[[git_remote_profiles]]` whitelisting hosts/owners, the TUI offers
to create a fresh GitHub repo for new projects (via `gh` or a
fine-grained PAT) before launching the agent. Tokens are held only
for the duration of the API call, never logged, never printed in
`--dry-run`. Configuration details and security model live in
[`docs/deployment.md#git-remote-on-new-project`](docs/deployment.md#git-remote-on-new-project).

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
- **Always run `uxon doctor` first.** It prints the resolved caller /
  launch user, active config paths, allowed roots, agent paths,
  socket details, current sessions, and any detected configuration
  issues.

More edge cases (legacy session prefixes, failed-launch banner, the
`--dsp` flag and its legacy aliases) are documented in
[`docs/cli.md`](docs/cli.md).

---

## Documentation

- [`docs/cli.md`](docs/cli.md) — full CLI reference.
- [`docs/deployment.md`](docs/deployment.md) — multi-host rollout,
  config rendering from JSON, runtime dependencies.
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

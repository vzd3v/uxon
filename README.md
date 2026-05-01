# ccw

[![CI](https://github.com/vzd3v/vz_devagent_cli_tool/actions/workflows/ci.yml/badge.svg)](https://github.com/vzd3v/vz_devagent_cli_tool/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)

A small, readable `tmux` wrapper for running terminal AI coding
agents — `claude`, `codex`, `cursor-agent` — on a single host or a
shared VPS, for one user or several.

`ccw` standardises session names, isolates agent sessions on a
dedicated `tmux` socket, supports git worktrees, optionally creates
the GitHub remote on a new project, and ships an interactive TUI
session picker.

<!-- screenshot goes here -->

## Why use it

- **Predictable session names.** `ccw-<project>@<agent>` (and
  `-2`, `-3` for parallels). No hand-rolled `tmux new -s` strings.
- **Per-user dedicated `tmux` socket.** Every launch user gets
  `/tmp/ccw-<user>.sock`. `list`, `attach`, `kill`, `kill-all` are
  deterministic — they don't see your user's other tmux sessions.
- **Multi-agent.** One config switch enables `claude`, `codex`,
  `cursor-agent`, or any subset.
- **Multi-user.** Caller user ≠ launch user via `sudo -iu`. One
  shared host can host agent sessions for several OS users.
- **Allowed-roots safety net.** Refuses to start an agent in
  unexpected directories.
- **Optional GitHub remote on new project.** Whitelisted profiles
  with either `gh` CLI or a fine-grained PAT.
- **Lazy TUI.** The interactive picker uses `textual` lazily —
  non-TUI subcommands work without it installed.

## Install

```bash
git clone https://github.com/vzd3v/vz_devagent_cli_tool.git
cd vz_devagent_cli_tool
sudo python3 install/install_ccw.py \
  --repo-dir "$(pwd)" \
  --install-path /usr/local/bin/ccw

cp config/config.example.toml config/config.toml
$EDITOR config/config.toml         # set allowed_roots, agents
ccw doctor                         # verify
```

The installer just creates a symlink — there is no Python package
to build. To use `ccw` from a different path, change `--install-path`
or skip the installer entirely and call `bin/ccw` directly.

### Optional dependencies

| Package | When required | Install |
|---------|---------------|---------|
| `textual >= 0.80, < 9` | interactive TUI (`ccw` with no args) | `pip install textual` |
| `tomlkit` | TUI Settings screen (config writes) | `pip install tomlkit` or `apt install python3-tomlkit` |
| `gh` | `auth = "gh"` git-remote profiles | distro package or [cli.github.com](https://cli.github.com) |

Without `textual`, `ccw` prints a hint and all non-interactive
subcommands still work. Without `tomlkit`, the TUI Settings save
fails — read-paths still work.

Operators running on multiple hosts: see
[`docs/deployment.md`](docs/deployment.md).

## Quick start

```bash
ccw                         # interactive TUI session picker (needs a TTY)
ccw run                     # start the default agent in the current directory
ccw -n myproj               # create ~/projects/myproj and start an agent there
ccw -n myrepo -w feature/x  # run claude inside git worktree 'feature/x'
ccw list                    # list active ccw-* sessions for this user
ccw attach myproj           # re-attach to an existing session
ccw kill myproj             # kill a session
ccw doctor                  # print diagnostics
```

## Commands

Short and long forms are equivalent unless noted.

### `ccw` (no arguments)
- With a TTY: opens the interactive TUI.
- Without a TTY: prints usage and exits.

### `ccw run [-w <branch>] [--dry-run] [--agent <id>] [--auto] [--dsp] [agent-flags...]`
Start an agent in the current directory.
- `--agent claude|codex|cursor` — pick the agent (default:
  `agents.default` from config).
- `--auto` — agent's "auto" permission mode (claude:
  `--permission-mode auto`; codex: `--full-auto`). Not supported by
  `cursor`.
- `--dsp` — agent's "yolo" permission mode
  (`--dangerously-skip-permissions` for claude,
  `--dangerously-bypass-approvals-and-sandbox` for codex,
  `--yolo` for cursor). Legacy aliases: `--dap`, `-dap`, `-dsp`.
- `--auto` and `--dsp` are mutually exclusive.
- `-w <branch>` — run inside an existing git worktree branch at
  cwd (claude only; errors for other agents).
- `--dry-run` — print the `tmux` command instead of executing.
- Any unknown flag is forwarded to the selected agent binary.

### `ccw new <name> [-w <branch>] [...]`
Short form: `ccw -n <name> ...`.
- Without `-w`: creates (or reuses) `<new_project_root>/<name>` and
  starts the agent there.
- With `-w <branch>`: uses the git repo inside
  `<new_project_root>/<name>` (the directory must already exist
  and be a git repo).
- `--attach-existing` / `--new-session` — bypass the repeat prompt
  (see [Repeat behaviour](#repeat-behaviour)).
- `--git-remote <profile>` — before launching, create a remote
  repo for the project through the named
  [git remote profile](#git-remote-on-new-project). `default`
  uses `default_git_remote_profile`. Incompatible with `-w`.
  Without `--git-remote`, no git is touched (CLI is non-interactive).
- `--git-visibility private|public` — override the profile's
  visibility default for this one call.
- `--no-git` — explicit "don't touch git" (same as omitting
  `--git-remote`).

### `ccw list [--all-users]`
Short form: `ccw -l [--all-users]`. Lists `ccw-*` (and legacy
`cc-*`) sessions with PID, CPU, RAM, creation time, last attach,
current command, and path.
- Default scope: the current launch user.
- `--all-users`: scope all `session_users` from config (requires
  `enable_all_users_list = true`).

### `ccw attach <id>`
Short form: `ccw -a <id>`. Re-attaches to a session. `<id>` accepts:
- full session name (`ccw-myproj@claude`);
- short name without prefix (`myproj@claude`);
- bare stem (`myproj`) when exactly one session matches;
- legacy (`cc-myproj`);
- active-pane PID.

### `ccw kill <id> [--dry-run]`
Short form: `ccw -k <id>`. Kills one session.

### `ccw kill-all [--force] [--dry-run]`
Alias: `ccw --killall`. Kills every `ccw-*` (and legacy `cc-*`)
session for the current launch user. Requires interactive
confirmation (`kill-all`) or `--force`.

### `ccw doctor`
Read-only diagnostics. Prints caller / launch user, active config
paths, `allowed_roots`, `new_project_root`,
`repeat_noninteractive_mode` and any env override, `tmux` and agent
paths for the launch user, dedicated socket details, current
sessions on the dedicated socket, legacy sessions on the default
socket, and a list of detected configuration issues. Use it first
whenever behaviour is unexpected.

### `ccw version`
Prints repo version and short git commit (with `-dirty` suffix
when the checkout is dirty). Also: `ccw -V`, `ccw --version`.

## Interactive TUI

`ccw` with no arguments on a TTY opens a full-screen picker. The
picker offers:

- **Actions** at the top:
  1. *New session in current folder* — `ccw run` equivalent
     (gated on write access).
  2. *Create new project* — prompts for a name, creates it under
     `new_project_root`, starts the default agent.
  3. *Open existing project* — pick an existing directory under
     `new_project_root`.
- **Sessions list** (your own) with live CPU/RAM, attached
  marker, and recency.
- **Server status** — load, normalised CPU load, RAM, disk, uptime.
  Inside a live SSH session the line also includes an async
  `ssh-link` quality probe (RTT, variance, retransmits) on its
  own cadence (`tui_ssh_refresh_interval_seconds`). The main
  screen auto-refreshes every `tui_refresh_interval_seconds`
  seconds and preserves the highlighted row.
- **⚡ Superuser block** (whenever passwordless sudo is detected):
  - Other users' sessions with a yellow `USER` column. `Enter`
    attaches via `sudo -iu <user>`; `d` kills the highlighted one.
  - *⚙ Settings* — repo-level `config.toml` editor (see below).
  - *Kill ALL ccw sessions (all users)* — appears when at least
    one session exists anywhere; requires typing `kill-all-global`
    to confirm.
- **Permissions prompt** before every launch — choose between
  regular and `--dangerously-skip-permissions`.

When the launched session exits (or you detach with `Ctrl-b d`),
`ccw` returns to the main screen with a refreshed list. `q` / `Esc`
on the main screen exits to the shell.

The CLI entry points (`ccw attach <id>`, `ccw run`, `ccw new`)
keep their original one-shot behaviour — they replace the process
with `tmux`, so detach returns to the shell.

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

### Settings sub-screen (superuser only)

Lists every `config.toml` key with its current value and origin
(`default` / `repo` / `project:<path>`):

- `Enter` — type-appropriate editor: bools toggle, enums cycle,
  strings get a text input, arrays use comma-separated input,
  `launch_user_by_caller` opens a dedicated mapping editor
  (`a` add / `d` delete / `Enter` edit / `s` save).
- `x` — revert a repo-level override back to the built-in default.
- Project-level (`.ccw.toml`) values are read-only — edit them in
  the project.
- Saves rewrite `config/config.toml` in place via a `tomlkit`
  round-trip (using `sudo tee` automatically when the file is not
  directly writable). Comments and formatting of untouched parts
  are preserved.
- Structured tables (`[[git_remote_profiles]]`,
  `[launch_user_by_caller]`) are not edited as forms here;
  `launch_user_by_caller` has its own mapping editor,
  `[[git_remote_profiles]]` is hand-edited in `config.toml`
  (press `g` to view them read-only).

## Configuration

Two layers, merged in order (later wins):

1. **Repo config** — `<repo>/config/config.toml`, host-wide.
   `config/config.example.toml` is the tracked starting point.
2. **Project config** — the nearest `.ccw.toml` in cwd or a parent
   that is itself inside an `allowed_roots` entry. Useful to
   override individual keys for one project. The TUI never writes
   `.ccw.toml`.

### Keys

| Key | Type | Default | Purpose |
|-----|------|---------|---------|
| `runtime_user` | string | `""` | Launch user when `default_launch_mode = "fixed"`. |
| `default_launch_mode` | `"caller"` / `"fixed"` | `"caller"` | Default for unmapped callers. |
| `launch_user_by_caller` | table | `{}` | Per-caller override. |
| `session_users` | array | `[]` | Users scanned by `list --all-users` and the TUI superuser block. |
| `enable_all_users_list` | bool | `false` | Enables `list --all-users`. |
| `allowed_roots` | array | `[]` | Directories `ccw` is allowed to launch in. The launch user's home is implicitly allowed too. |
| `new_project_root` | string | `~/projects` | Base directory for `ccw new <name>`. Must be inside an `allowed_roots` entry (or be a sub-path of `$HOME`). |
| `session_prefix` | string | `"ccw-"` | TMUX session-name prefix (hardcoded for new sessions). |
| `agents.enabled` | array | `["claude"]` | Ordered list of enabled agent ids (`claude`, `codex`, `cursor`). |
| `agents.default` | string | `"claude"` | Default agent when `--agent` is not passed. Must be in `agents.enabled`. |
| `agents.<id>.default_args` | array | `[]` | Flags prepended to every invocation of that agent. |
| `tmux_socket_template` | string | `/tmp/ccw-{user}.sock` | Per-user socket path. Placeholders: `{user}`, `{uid}`. |
| `tui_refresh_interval_seconds` | number | `2.0` | Main TUI auto-refresh interval. |
| `tui_ssh_refresh_interval_seconds` | number | `10.0` | `ssh-link` refresh interval. |
| `repeat_noninteractive_mode` | `"fail"` / `"attach"` / `"new"` | `"fail"` | Non-TTY fallback for repeat prompt. |
| `git_create_enabled` | bool | `false` | Master switch for git-remote-on-new-project. |
| `default_git_remote_profile` | string | `""` | Profile used when `--git-remote default` is passed or as the TUI pre-selected default. |
| `git_remote_profiles` | array of tables | `[]` | Whitelist of allowed targets (see below). |

### Environment variables

- `CCW_REPEAT_NONINTERACTIVE_POLICY` — overrides
  `repeat_noninteractive_mode` per invocation.
- `CCW_LOG_DIR` — overrides the TUI event-log directory.
  Default: `${XDG_STATE_HOME:-~/.local/state}/ccw`.
- `SUDO_USER` — honoured when `ccw` is invoked via `sudo` to
  identify the real caller.

### Rendering config from JSON

When deploying across multiple hosts, generate `config.toml` from
a single JSON payload:

```bash
python3 install/render_ccw_config.py \
  --config-json examples/ccw-config.json \
  --output config/config.toml
```

## Worktrees (`-w <branch>`)

- `ccw run -w <branch>` — uses the git repo at the current working
  directory.
- `ccw new <name> -w <branch>` — uses the repo inside
  `<new_project_root>/<name>`. The directory must already exist
  and be a git repo. `ccw` never creates worktrees for you.
- The session name includes both repo and branch slugs, so
  multiple branches of the same repo coexist cleanly.

## Repeat behaviour

When `ccw new` finds a session that already matches the requested
target (same project or same worktree):

- **Interactive TTY** — prompts to attach, start a parallel
  session, or cancel.
- **Non-interactive**, resolved in this order:
  1. Explicit flag: `--attach-existing` or `--new-session`.
  2. Env var `CCW_REPEAT_NONINTERACTIVE_POLICY=fail|attach|new`.
  3. Config key `repeat_noninteractive_mode` (default `fail`).

If compatible sessions exist only on the **legacy default tmux
socket** (pre-dedicated-socket era), `ccw new` fails with an
explicit hint instead of silently creating duplicates on the
dedicated socket.

## Multi-user / launch user

`ccw` distinguishes the **caller user** (who invoked the command)
from the **launch user** (who actually owns the tmux session and
runs the agent). One tool can therefore support multiple service
users on a single host.

Resolution order for the launch user:
1. `launch_user_by_caller[<caller>]` if set.
2. If `default_launch_mode = "caller"` → the caller.
3. Otherwise → `runtime_user`.

When the caller differs from the launch user, `ccw` uses
`sudo -iu <user>` to run `tmux`, `git`, and `mkdir` as that user.
Each launch user gets its own `tmux` socket.

## Supported agents

| Agent id | Binary | `--auto` mode | `--dsp` (yolo) mode | Install |
|----------|--------|---------------|---------------------|---------|
| `claude` | `claude` | `--permission-mode auto` | `--dangerously-skip-permissions` | [Anthropic docs](https://docs.claude.com/claude-code) |
| `codex`  | `codex` | `--full-auto` | `--dangerously-bypass-approvals-and-sandbox` | `npm i -g @openai/codex` |
| `cursor` | `cursor-agent` | (not supported) | `--yolo` | `curl https://cursor.com/install -fsSL \| bash` |

Notes:

- `cursor` does not have an `--auto` permission mode. Passing
  `--auto` with cursor is an error.
- `-w <branch>` (worktree) is claude-only. Using it with codex or
  cursor is an error.
- `ccw doctor` probes each enabled agent and prints its path,
  version, and status.

If `ccw` starts and none of the agents listed under
`[agents] enabled` are installed, the TUI shows a modal with the
install hint for each one. The modal appears only after the
background probe resolves — initial paint is not blocked. Press
`r` on the main screen after installing to re-probe.

## Git remote on new project

When `git_create_enabled = true`, `ccw new <name>` can create a
fresh remote repo on a supported host (currently GitHub) before
launching the agent. The TUI asks interactively; the CLI only
acts when you pass `--git-remote <profile>` explicitly — it never
prompts.

`ccw` never creates a repo outside the `git_remote_profiles`
whitelist.

Each profile names:

- `host`, `owner` — where the repo lands (e.g.
  `github.com` / `vzd3v`).
- `auth` — how the remote is created:
    - `"gh"` — runs `gh repo create` under `creds_user` (the
      OAuth token from that user's `~/.config/gh/hosts.yml` is
      used; `repo` scope is required).
    - `"token"` — reads a fine-grained Personal Access Token from
      `token_file` under `creds_user` and calls the GitHub REST
      API directly. The token is held only for the duration of
      the API call, never logged, never printed in `--dry-run`
      output.
- `creds_user` (optional) — OS user on this host whose
  credentials are used for the *remote creation* step. Falls
  back to the launch user. Local `git init` / `commit` / `push`
  always run under the launch user.
- `token_file` — required when `auth = "token"`; must be readable
  by `creds_user`. `ccw` only probes readability; ownership and
  mode are up to the operator.
- `visibility` — `"private"` (default) or `"public"` for newly
  created repos. Override per call with `--git-visibility`.

Example `config/config.toml`:

```toml
git_create_enabled = true
default_git_remote_profile = "personal"

[[git_remote_profiles]]
name       = "personal"
host       = "github.com"
owner      = "your-username"
auth       = "gh"
creds_user = "your-os-user"
visibility = "private"

[[git_remote_profiles]]
name       = "acme-org"
host       = "github.com"
owner      = "acme"
auth       = "token"
creds_user = "your-os-user"
token_file = "/home/your-os-user/.secrets/ccw-acme.token"
visibility = "private"
```

`ccw doctor` prints one line per profile with a read-only status
(`ok` / `warn:<reason>`) — passwordless sudo to `creds_user`,
presence of `gh`, login status or token-file readability. It never
attempts the create call.

Under the hood:

1. `git init -b main` + empty initial commit in the project
   directory (under launch user).
2. `gh repo create … --source=<dir> --push` (`auth = "gh"`) or
   `POST /user/repos` / `/orgs/<owner>/repos` with the
   fine-grained token (`auth = "token"`), under `creds_user`.
3. For `auth = "token"`: `git remote add origin <ssh_url>` +
   `git push -u origin main` under launch user. For
   `auth = "gh"`: `gh` already pushed, `ccw` only verifies
   the `origin` URL.

If any step fails, the local `.git` stays in place so you can
inspect or rerun. `ccw` reports which *stage* failed
(`preflight` / `local_init` / `remote_create` / `push`).

## Session naming

```
ccw-<stem>@<agent>           plain
ccw-<repo>-<branch>@<agent>  worktree
ccw-<stem>@<agent>-N         parallels (N appended after the agent)
```

The `ccw-` prefix is hardcoded for new sessions. Legacy `cc-<stem>`
/ `cc-<stem>-N` sessions (from before 2026-04-21) are still
recognised as read-only `claude` sessions — they appear in
`ccw list`, can be attached and killed, but `ccw` never *creates*
new `cc-*` sessions.

Identifier resolution accepts: full name, name without prefix,
bare stem (when unique), legacy `cc-*`, and active-pane PID.

## Troubleshooting

### Running `ccw` from inside an existing `tmux` session

`tmux` refuses to nest. `ccw` handles this transparently when
`$TMUX` names the same socket that `ccw` uses for the launch user:
attach becomes `tmux switch-client`, and `new` first creates
the session detached and then `switch-client`s to it.

If `$TMUX` names a **different** socket (a tmux server not owned
by `ccw`), `ccw` prints an actionable error telling you to
`Ctrl-B d` first.

### `textual` not installed

The TUI prints an install hint and exits. `ccw run`, `ccw list`,
`ccw doctor`, `ccw new`, `ccw attach`, `ccw kill`, `ccw version`
all work without `textual`.

### TUI errors render as a red toast

Operations launched from the TUI (attach / kill / create /
refresh) can fail for many reasons — tmux session gone,
permission denied, allowed-roots mismatch, git remote creation
error, config write conflict. Every TUI callback is wrapped: any
`SystemExit` (or other exception) is converted into a
`CallbackError` and rendered as a red toast (e.g.
`Kill ccw-demo failed: tmux: no server running`). Refresh,
settings, kill-all, activate all go through this path.

A failed launch (non-zero rc from the forked tmux/agent process)
also pauses with a banner on the physical terminal before the
main screen re-enters fullscreen, so you can read whatever stderr
the failed command printed. `rc 0` (success) and `rc 130`
(user `Ctrl-C` in the agent) do not pause.

### Legacy `cc-*` sessions on the default socket

If you had `cc-*` sessions on the user's default tmux socket
before the dedicated-socket switch, they are not automatically
managed. Use `ccw doctor` to spot them, then clean up or migrate
manually.

## `--dsp` (dangerously-skip-permissions)

`--dsp` is the canonical short form for the agent's "skip
permission prompts" mode. The TUI asks explicitly before every
launch; the CLI requires the flag.

Legacy aliases accepted for back-compat: `--dap`, `-dap`, `-dsp`.

## Architecture & development

- [`docs/architecture.md`](docs/architecture.md) — module map,
  TUI internals, boundaries, hard rules.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — local checks, branch
  policy, when to bump `VERSION`, adding a config key.
- [`docs/deployment.md`](docs/deployment.md) — multi-host
  topology, runtime dependencies, migration notes.
- [`SECURITY.md`](SECURITY.md) — threat model, disclosure
  policy.
- [`CHANGELOG.md`](CHANGELOG.md) — version history.

## Versioning

`ccw` follows [SemVer](https://semver.org/). Bump `VERSION` in
the same commit as any user-visible behaviour change.
`ccw --version` prints the version plus the git commit (with
`-dirty` suffix when the checkout is dirty).

## License

[MIT](LICENSE) © 2026 Vasily Zakharov.

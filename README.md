# ccw — Claude Code tmux wrapper

`ccw` is a readable, multi-user wrapper around `tmux` for running `claude`
sessions on a shared VPS. It standardizes session naming, isolates state on a
dedicated tmux socket, supports git worktrees, and provides an interactive TUI
picker.

Canonical paths on a deployed host:

- repo checkout: `/srv/apps/vz_devagent_cli_tool`
- user command: `/usr/local/bin/ccw` → symlink to `bin/ccw`
- config: `/srv/apps/vz_devagent_cli_tool/config/config.toml`
- tmux socket: `/tmp/ccw-<launch-user>.sock` (default)

---

## Quick start

```bash
ccw                         # interactive TUI session picker (needs a TTY)
ccw run                     # start claude in the current directory
ccw -n myproj               # create /srv/repos/myproj and start claude there
ccw -n myrepo -w feature/x  # run claude inside git worktree 'feature/x'
ccw list                    # list active cc-* sessions for this user
ccw attach myproj           # re-attach to an existing session
ccw kill myproj             # kill a session
ccw doctor                  # print diagnostics
```

All `ccw` sessions are named `cc-<stem>` (or `cc-<stem>-N` for parallels). The
prefix is configurable.

---

## Commands

Short form and long form are equivalent unless noted.

### `ccw` (no args)
- With a TTY: opens the interactive TUI.
- Without a TTY: prints usage and exits.

### `ccw run [-w <branch>] [--dry-run] [--agent <id>] [--auto] [--dsp] [agent-flags...]`
Start an agent in the current directory.
- `--agent claude|codex|cursor`: choose which agent to launch (default: `agents.default` from config).
- `--auto`: select the agent's "auto" permission mode (claude: `--permission-mode auto`; codex: `--full-auto`). Not supported by `cursor`.
- `--dsp`: select the agent's "yolo" permission mode (`--dangerously-skip-permissions` for claude, `--dangerously-bypass-approvals-and-sandbox` for codex, `--yolo` for cursor). Legacy aliases: `--dap`, `-dap`, `-dsp`.
- `--auto` and `--dsp` are mutually exclusive.
- `-w <branch>`: run inside an existing git worktree branch at cwd (claude only; errors for other agents).
- `--dry-run`: print the tmux command instead of executing.
- Any unknown flag is forwarded to the selected agent binary.

### `ccw new <name> [-w <branch>] [--attach-existing|--new-session] [--dry-run] [--agent <id>] [--auto] [--dsp] [--git-remote <profile>|default | --no-git] [--git-visibility private|public] [agent-flags...]`
Short form: `ccw -n <name> ...`.
- Without `-w`: creates (or reuses) `<new_project_root>/<name>` and starts the agent there.
- With `-w <branch>`: uses the git repo inside `<new_project_root>/<name>`
  (the directory must exist and be a git repo).
- `--attach-existing` / `--new-session`: bypass the repeat prompt (see
  [Repeat behavior](#repeat-behavior)).
- `--git-remote <profile>`: before launching, create a remote repo for the
  project through the named [git remote profile](#git-remote-on-new-project).
  `default` uses `default_git_remote_profile`. Incompatible with `-w`.
  Without `--git-remote`, no git is touched (CLI is non-interactive).
- `--git-visibility private|public`: override the profile's visibility
  default for this one call.
- `--no-git`: explicit "don't touch git" — same as omitting `--git-remote`.

### `ccw list [--all-users]`
Short form: `ccw -l [--all-users]`.
Lists `cc-*` sessions with PID, CPU, RAM, creation time, last-attach time,
current command, and path.
- Default scope: the current launch user.
- `--all-users`: scope all `session_users` from config (requires
  `enable_all_users_list = true`).

### `ccw attach <id>`
Short form: `ccw -a <id>`.
Re-attaches to a session. `<id>` accepts:
- full session name (`cc-myproj`)
- short name without prefix (`myproj`)
- unique prefix (`my` if it matches exactly one)
- active-pane PID.

### `ccw kill <id> [--dry-run]`
Short form: `ccw -k <id> [--dry-run]`. Kills one session.

### `ccw kill-all [--force] [--dry-run]`
Alias: `ccw --killall`. Kills all `cc-*` sessions for the current launch user.
Requires an interactive confirmation (`kill-all`) or `--force`.

### `ccw doctor`
Read-only diagnostics. See [Diagnostics](#diagnostics).

### `ccw version`
Prints repo version and short git commit (if available).
Also: `ccw -V`, `ccw --version`.

---

## Interactive TUI

`ccw` with no arguments on a TTY opens a full-screen picker (requires the
`textual` Python package, `>=0.80,<9`). It offers:

- **Actions** at the top:
  1. *New session in current folder* — `ccw run` equivalent.
  2. *Create new project* — prompts for a name, creates it under
     `new_project_root`, and starts `claude`.
  3. *Open existing project* — pick an existing directory under
     `new_project_root`.
- **Sessions list** (your own) with live CPU/RAM, attached marker, and recency.
- **Server status** with load, normalized CPU load, RAM, disk usage, and uptime.
  The same line also includes an async `ssh-link` probe for latency, jitter,
  and packet loss on the current SSH path. If `icmplib` is not installed,
  ccw shows a one-line install hint and keeps running.
  The main screen auto-refreshes every `tui_refresh_interval_seconds` seconds
  while preserving the highlighted row.
- **⚡ Superuser block** (whenever passwordless sudo is detected):
  - Other users' sessions with a yellow `USER` column (if any exist).
    `Enter` attaches via `sudo -iu <user>`, `d` kills the highlighted one.
  - *⚙ Settings* — opens a repo-level `config.toml` editor (see
    [Superuser mode](#superuser-mode-tui)).
  - *Kill ALL ccw sessions (all users)* — appears when at least one session
    exists anywhere; requires typing `kill-all-global` to confirm.
- **Permissions prompt** before every launch: choose between regular and
  `--dangerously-skip-permissions`.

When the session you launched or attached to exits (or you detach with
`Ctrl-b d`), `ccw` comes back to this main screen with a refreshed session
list — it does not drop you to the shell. Pressing `q` / `Esc` on the main
screen still exits to the shell. The CLI entry points (`ccw attach <id>`,
`ccw run`, `ccw new`) keep their original one-shot behavior: they replace
the process with tmux, so detach returns to the shell.

### Keys
| Key | Action |
|-----|--------|
| `↑` `↓` / `j` `k` | Navigate |
| `1`–`9` | Jump to item by number |
| `Enter` | Activate (launch action / attach session / trigger global kill-all) |
| `d` | Kill highlighted session (with confirmation; works on own and other-user sessions when superuser) |
| `D` | Kill all **own** sessions (type `kill-all` to confirm) |
| `r` | Refresh |
| `g` / `G` | Jump to first / last |
| `q` / `Esc` | Quit (or back, in sub-screens) |

---

## Session naming

New sessions follow the form `ccw-<stem>@<agent>`:

- Plain: `ccw-<slug(dirname)>@<agent>` (e.g. `ccw-myproject@claude`)
- Worktree: `ccw-<slug(repo)>-<slug(branch)>@<agent>` (e.g. `ccw-myrepo-feature-x@cursor`)
- Parallels: suffix `-2`, `-3`, … appended **after** the agent (e.g. `ccw-myproject@codex-2`)

The prefix `ccw-` is hardcoded for new sessions. Legacy `cc-<stem>` / `cc-<stem>-N` sessions
(from before 2026-04-21) are still recognized as read-only claude sessions — they appear in
`ccw list`, can be attached/killed, but `ccw` will never create new `cc-*` sessions.

Identifier resolution (for `ccw attach`, `ccw kill`, etc.) accepts:
- Full name: `ccw-myproject@codex`
- Without prefix: `myproject@codex`
- Bare stem: `myproject` (succeeds if exactly one session matches across all agents; errors with candidates if ambiguous)
- Legacy: `cc-myproject`
- Active pane PID

---

## Worktrees (`-w <branch>`)

- `ccw run -w <branch>`: uses the git repo at the current working directory.
- `ccw new <name> -w <branch>`: uses the repo inside
  `<new_project_root>/<name>`. The directory must already exist and be a git
  repo — `ccw` never creates worktrees for you.
- The session name includes both repo and branch slugs, so multiple branches
  of the same repo coexist cleanly.

---

## Repeat behavior

When `ccw new` finds a session that is already compatible with the requested
target (same project or same worktree):

- **Interactive TTY**: prompts to attach, start a parallel session, or cancel.
- **Non-interactive**, resolved in this order:
  1. Explicit flag: `--attach-existing` or `--new-session`.
  2. Env var `CCW_REPEAT_NONINTERACTIVE_POLICY=fail|attach|new`.
  3. Config key `repeat_noninteractive_mode` (default `fail`).

If compatible sessions exist only on the **legacy default tmux socket**
(pre-dedicated-socket era), `ccw new` fails with an explicit hint instead of
silently creating duplicates on the dedicated socket.

---

## Allowed roots

`ccw` refuses to launch `claude` in directories outside `allowed_roots`. This
guards against accidentally starting `claude` in `/tmp`, system paths, etc.
Configure via `allowed_roots` in `config.toml`.

In addition to the configured list, the **launch user's home directory is
always treated as an allowed root** for launching (not for project creation) —
running `claude` in your own home is a normal workflow and shouldn't require
an explicit config entry. It's a launch-time augmentation only; it does not
show up in `config.toml` and `ccw new <name>` still creates projects under
`new_project_root` exclusively.

`new_project_root` (the base for `ccw new <name>` without `-w`) must itself be
under `allowed_roots` — `ccw doctor` flags this.

---

## Multi-user / launch user

`ccw` distinguishes the **caller user** (who invoked the command) from the
**launch user** (who actually owns the tmux session and runs `claude`). This
lets a single tool support multiple service users on one host.

Resolution order for the launch user:

1. `launch_user_by_caller[<caller>]` if set.
2. If `default_launch_mode = "caller"` → the caller.
3. Otherwise → `runtime_user`.

When the caller differs from the launch user, `ccw` uses `sudo -iu <user>` to
run tmux / git / mkdir as that user. Each launch user gets a separate tmux
socket.

`ccw list --all-users` aggregates sessions across every user in
`session_users` (requires `enable_all_users_list = true`).

---

## Superuser mode (TUI)

When the TUI starts, `ccw` runs a fast, non-interactive check for
passwordless sudo:

1. `os.geteuid() == 0` → True.
2. Otherwise `sudo -n true` with a 0.5 s timeout. Exit 0 → True.
   (We probe with `true`, not `-v`: `sudo -v` validates the credential
   cache and fails under `-n` even for `NOPASSWD: ALL` users.)

On True, the TUI gains a ⚡ superuser marker in the header/footer and
collects sessions for every user in `session_users` (other than the current
launch user). The *── superuser ──* block at the bottom contains, in order:

1. **Other users' sessions** (if any), with a highlighted yellow `USER`
   column. `Enter`, `d`, and other per-session actions work the same as for
   your own — they are transparently routed through `sudo -iu <user>`.
2. **⚙ Settings** — a sub-screen listing every `config.toml` key, its
   current value, and its origin (`default` / `repo` / `project:<path>`):
   - `Enter` opens a type-appropriate editor: bools toggle, enums cycle,
     strings get a text input, arrays use comma-separated input,
     `launch_user_by_caller` opens a dedicated mapping editor
     (`a` add / `d` delete / `Enter` edit / `s` save).
   - `x` reverts a repo-level override back to the built-in default.
   - Values that come from a project-level `.ccw.toml` are read-only and
     clearly marked — edit them in the project instead.
   - Saves rewrite `config/config.toml` in place (using `sudo tee`
     automatically when the file is not directly writable). Comments
     and formatting of untouched parts are preserved — the writer
     round-trips through `tomlkit`.
   - Structured tables (`[[git_remote_profiles]]`, `[launch_user_by_caller]`)
     are not edited here as forms; `launch_user_by_caller` has its own
     mapping editor above, `[[git_remote_profiles]]` is hand-edited in
     `config.toml` (press `g` to view them read-only).
3. **Kill ALL ccw sessions (all users)** — last item, only when at least
   one session exists. Confirmation phrase: `kill-all-global` (distinct
   from the regular `kill-all` phrase for `D`).

When passwordless sudo is not available, nothing superuser-related is
shown — the TUI behaves exactly as before.

---

## Dedicated tmux socket

Every launch user has its own socket, rendered from `tmux_socket_template`
(default: `/tmp/ccw-{user}.sock`; placeholders: `{user}`, `{uid}`). This
isolates `ccw` sessions from the user's default tmux server, making
`list`/`attach`/`kill`/`kill-all` deterministic.

**Migration**: if you had `cc-*` sessions on the user's default socket before
this change, they are not automatically managed. Use `ccw doctor` to spot
them, then clean up or migrate manually.

### Error surfacing in the TUI

Operations launched from the TUI (attach / kill / create / refresh) can fail
for many reasons — tmux session gone, permission denied, allowed-roots
mismatch, git remote creation error, config write conflict, and so on.
Historically some of these failures reached `fail()` inside ccw, which
prints `ccw: <msg>` to stderr and `raise SystemExit`. Under the fullscreen
TUI context that combination looked like ccw silently quitting.

Since 0.10.3 every TUI callback is wrapped so any `SystemExit` (or other
exception) is converted into a `CallbackError`; the screen renders it as
a red toast via ``self.notify(..., severity="error")`` (e.g.
`Kill cc-demo failed: tmux: no server running on /tmp/ccw-u-ed.sock`).
Refresh / settings / kill-all / activate all go through this path. A
crash in the outer loop is caught after textual releases the terminal
and printed as a visible traceback instead of leaving a blank screen.

Failed launches (non-zero rc from the forked tmux/claude process) also
pause with a banner on the physical terminal before the main screen
re-enters fullscreen, so the user can read whatever stderr the failed
command printed:

    ccw: launch cc-myproj failed (rc=1, stage=cmd)
      command: tmux -S /tmp/ccw-u-vz.sock new-session -As cc-myproj -c /srv/repos/myproj claude
      see output above for details
    press any key to return to the ccw menu...

rc `0` (success) and rc `130` (user Ctrl-C in claude) do not pause.

### Running `ccw` from inside an existing tmux session

tmux refuses to nest (`sessions should be nested with care, unset $TMUX`),
so a naive `tmux attach-session` / `tmux new-session` from a shell already
inside tmux just flashes the refusal message and drops back to the prompt.
`ccw` handles this transparently: when `$TMUX` names the same socket that
`ccw` uses for the launch user, `attach` is executed as
`tmux switch-client -t <session>` and `new` first creates the session
detached (`tmux new-session -dA -s <name> -c <dir> claude …`) and then
switch-clients to it. The end result is the same — the user's tmux client
ends up attached to the target session — with no refusal message. When
`$TMUX` names a **different** socket (a tmux server not owned by `ccw`),
`ccw` bails out with an actionable error telling the user to `Ctrl-B d`
first.

---

## `--dsp` (dangerously-skip-permissions)

`--dsp` is the canonical short form for `--dangerously-skip-permissions`. Use
it to let `claude` operate without prompting for tool permissions. The TUI
asks explicitly before every launch; the CLI requires the flag.

Legacy aliases accepted for back-compat: `--dap`, `-dap`, `-dsp`.

---

## Diagnostics

`ccw doctor` prints:

- caller user and resolved launch user
- active config paths (repo-level + project-level `.ccw.toml`, if any)
- `allowed_roots` and `new_project_root`
- `repeat_noninteractive_mode` and env override
- `tmux` path for the launch user
- dedicated socket path, parent existence, parent writability
- `claude` path for the launch user
- current sessions on the dedicated socket
- legacy sessions on the default socket (if any)
- a list of detected configuration / runtime issues

Use it first whenever behavior is unexpected.

---

## Configuration

Two layers, merged in order (later wins):

1. **Repo config**: `/srv/apps/vz_devagent_cli_tool/config/config.toml` —
   host-wide `ccw` settings, usually owned by an admin user. Edited
   directly or via the [superuser Settings screen](#superuser-mode-tui)
   in the TUI.
2. **Project config**: the nearest `.ccw.toml` in the cwd or a parent,
   provided that parent is inside an `allowed_roots` entry. Used to
   override individual keys for a specific project/repo; ships with the
   project source. The TUI never writes `.ccw.toml`.

### Keys

| Key | Type | Default | Purpose |
|-----|------|---------|---------|
| `runtime_user` | string | `""` | Launch user when `default_launch_mode="fixed"`. |
| `default_launch_mode` | `"caller"` / `"fixed"` | `"caller"` | Default for unmapped callers. |
| `launch_user_by_caller` | table | `{}` | Per-caller override. |
| `session_users` | array | `[]` | Users scanned by `list --all-users` and by the TUI superuser block. |
| `enable_all_users_list` | bool | `false` | Enables `list --all-users`. |
| `allowed_roots` | array | (see source) | Dirs `ccw` is allowed to run in. |
| `new_project_root` | string | `/srv/repos` | Base dir for `ccw new <name>`. |
| `session_prefix` | string | `"ccw-"` | Tmux session name prefix (hardcoded for new sessions). |
| `agents.enabled` | array | `["claude"]` | Ordered list of enabled agent ids (`claude`, `codex`, `cursor`). |
| `agents.default` | string | `"claude"` | Default agent when `--agent` is not passed. Must be in `agents.enabled`. |
| `agents.claude.default_args` | array | `[]` | Flags prepended to every claude invocation. |
| `agents.codex.default_args` | array | `[]` | Flags prepended to every codex invocation. |
| `agents.cursor.default_args` | array | `[]` | Flags prepended to every cursor-agent invocation. |
| `tmux_socket_template` | string | `/tmp/ccw-{user}.sock` | Per-user socket path. Placeholders: `{user}`, `{uid}`. |
| `tui_refresh_interval_seconds` | number | `2.0` | Main TUI auto-refresh interval in seconds. |
| `tui_ssh_health_target` | string | `""` | Override target for the `ssh-link` probe. Empty = derive from `SSH_CLIENT`. |
| `repeat_noninteractive_mode` | `"fail"` / `"attach"` / `"new"` | `"fail"` | Non-TTY fallback for repeat prompt. |
| `git_create_enabled` | bool | `false` | Master switch for the [git remote on new project](#git-remote-on-new-project) flow. |
| `default_git_remote_profile` | string | `""` | Profile used when `--git-remote default` is passed or as the TUI pre-selected default. |
| `git_remote_profiles` | array of tables | `[]` | Whitelist of allowed targets; see section below. |

### Git remote on new project

When `git_create_enabled = true`, `ccw new <name>` can create a fresh
remote repo on a supported host (currently GitHub) before launching
claude. The TUI asks interactively; the CLI only acts when you pass
`--git-remote <profile>` explicitly — it never prompts.

ccw never creates a repo outside the `git_remote_profiles` whitelist.

Each profile explicitly names:
- `host`, `owner` — where the repo lands (e.g. `github.com` / `vzd3v`).
- `auth` — how the remote is created:
    - `"gh"` — runs `gh repo create` under `creds_user` (the OAuth token
      from that user's `~/.config/gh/hosts.yml` is used; `repo` scope is
      required). Other devs don't need their own `gh` login.
    - `"token"` — reads a fine-grained Personal Access Token from
      `token_file` under `creds_user` and calls the GitHub REST API
      directly. The token is held only for the duration of the API
      call, never logged, never printed in `--dry-run` output.
- `creds_user` (optional) — OS user on this host whose credentials are
  used for the *remote creation* step. Falls back to the launch_user.
  Local `git init`/`commit`/`push` always run under the launch_user.
- `token_file` — required when `auth="token"`; must be readable by
  `creds_user`. ccw only probes readability; ownership and mode are up
  to the operator.
- `visibility` — `"private"` (default) or `"public"` for newly created
  repos. Override per-call with `--git-visibility`.

Example `config/config.toml`:

```toml
git_create_enabled = true
default_git_remote_profile = "vzd3v-personal-gh"

# ─ vzd3v's personal account via the `gh` CLI logged in as "remdepl" ─
[[git_remote_profiles]]
name       = "vzd3v-personal-gh"
host       = "github.com"
owner      = "vzd3v"
auth       = "gh"
creds_user = "remdepl"
visibility = "private"

# ─ Org "acme", restricted to create-repo via a fine-grained token ─
[[git_remote_profiles]]
name       = "acme-fg"
host       = "github.com"
owner      = "acme"
auth       = "token"
creds_user = "remdepl"
token_file = "/home/remdepl/.secrets/ccw-acme-create.token"
visibility = "private"
```

`ccw doctor` prints one line per profile with a read-only status
(`ok` / `warn:<reason>`) — passwordless sudo to `creds_user`, presence
of `gh`, login status or token-file readability. It never attempts the
create call.

Under the hood:
1. `git init -b main` + empty initial commit in the project dir (under
   launch_user).
2. `gh repo create … --source=<dir> --push` (for `auth="gh"`) or `POST
   /user/repos` / `/orgs/<owner>/repos` with the fine-grained token
   (for `auth="token"`), under `creds_user`.
3. For `auth="token"`: `git remote add origin <ssh_url>` + `git push -u
   origin main` under launch_user. For `auth="gh"`: `gh` already pushed,
   we just make sure `origin` URL matches.

If any step fails, the local `.git` stays in place so you can inspect or
rerun — ccw reports which *stage* failed (`preflight` / `local_init` /
`remote_create` / `push`).

### Environment

- `CCW_REPEAT_NONINTERACTIVE_POLICY` — overrides `repeat_noninteractive_mode`
  per invocation.
- `SUDO_USER` — honored when `ccw` is invoked via `sudo` to identify the real
  caller.

### Rendering config from JSON

```bash
python3 install/render_ccw_config.py \
  --config-json examples/ccw-config.json \
  --output config/config.toml
```

---

## Supported agents

`ccw` can launch three terminal AI agents. Which agents are available on a given host is declared in
`config/config.toml` under `[agents]`.

### Agent catalog

| Agent id | Binary | `--auto` mode | `--dsp` (yolo) mode | Install |
|----------|--------|---------------|---------------------|---------|
| `claude` | `claude` | `--permission-mode auto` | `--dangerously-skip-permissions` | see https://docs.claude.com/claude-code |
| `codex` | `codex` | `--full-auto` | `--dangerously-bypass-approvals-and-sandbox` | `npm i -g @openai/codex` |
| `cursor` | `cursor-agent` | (not supported) | `--yolo` | `curl https://cursor.com/install -fsSL \| bash` |

### Example config

```toml
[agents]
enabled = ["claude", "cursor"]   # codex not installed on this host
default = "claude"

[agents.claude]
default_args = []

[agents.codex]
default_args = []

[agents.cursor]
default_args = []
```

### CLI cheat sheet

```bash
# Use the default agent (from agents.default):
ccw run
ccw new myproject

# Explicit agent:
ccw new myproject --agent cursor
ccw run --agent codex --auto   # codex in full-auto mode

# Permission modes:
ccw run                        # normal (default)
ccw run --auto                 # auto (claude/codex only)
ccw run --dsp                  # yolo/dangerously-skip-permissions
```

### Notes

- `cursor` does not have an `--auto` permission mode. Passing `--auto` with cursor (explicitly or as the default agent) is an error.
- `-w <branch>` (worktree) is claude-only. Using it with codex or cursor is an error.
- `ccw doctor` probes each enabled agent and prints its path, version, and status.

If ccw starts and none of the agents listed under `[agents] enabled` are
installed, the TUI shows a modal with the install hint for each one
(links / package names from the agent catalog). The modal appears only
after the background probe resolves — initial paint is not blocked.
Press `r` on the main screen after installing to re-probe.

---

## Install

```bash
sudo python3 install/install_ccw.py \
  --repo-dir /srv/apps/vz_devagent_cli_tool \
  --install-path /usr/local/bin/ccw
```

`textual` (for the TUI) is optional: `pip install textual`. Without it,
`ccw` prints a hint and all non-interactive subcommands still work.

---

## Versioning

- Bump `VERSION` whenever user-visible behavior changes.
- `ccw --version` prints repo version and git commit (with `-dirty` suffix
  when the checkout is dirty).
- Verify a host:

```bash
ccw --version
git -C /srv/apps/vz_devagent_cli_tool rev-parse --short HEAD
cat /srv/apps/vz_devagent_cli_tool/VERSION
```

---

## Repo structure

- `bin/ccw` — CLI entrypoint (thin wrapper around `lib/`).
- `lib/ccw_tui.py` — interactive TUI main loop + main-screen rendering.
- `lib/ccw_tui_widgets.py` — reusable TUI primitives (`dim`,
  `confirm_phrase`, `confirm_yn`, `text_input`, `flash_error`).
- `lib/ccw_tui_settings.py` — superuser *⚙ Settings* sub-screens.
- `lib/ccw_settings.py` — settings schema + repo-level TOML read/write.
- `lib/ccw_git_profiles.py` + `ccw_git_backend_gh.py` +
  `ccw_git_backend_token.py` + `ccw_git_create.py` — git-remote-on-new-project
  (schema, two backends, orchestrator).
- `install/install_ccw.py` — installs `/usr/local/bin/ccw` symlink.
- `install/render_ccw_config.py` — renders `config.toml` from JSON.
- `tests/` — unit tests.
- `examples/ccw-config.json` — example config-rendering payload.
- `config/` — host config dir (gitignored).
- `VERSION` — release version.

---

## Local checks

See the checks block in [`AGENTS.md`](AGENTS.md) — CI runs the same two
commands on pushes to `main` and pull requests.

---

## Release / rollout checklist

1. Update code, tests, docs, and `VERSION`.
2. Run local checks plus `ccw doctor` against a rendered repo-local config.
3. Commit and push.
4. Deploy the exact ref to each host.
5. Verify `ccw --version`, `ccw doctor`, repeat-session behavior, and socket
   path on each host.
6. Update infra runbooks, host passports, and change logs.

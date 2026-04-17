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

### `ccw run [-w <branch>] [--dry-run] [--dsp] [claude-flags...]`
Start `claude` in the current directory.
- `-w <branch>`: run inside an existing git worktree branch at cwd.
- `--dry-run`: print the tmux command instead of executing.
- `--dsp`: pass `--dangerously-skip-permissions` to `claude`
  (legacy aliases: `--dap`, `-dap`, `-dsp`).
- Any unknown flag is forwarded to `claude`.

### `ccw new <name> [-w <branch>] [--attach-existing|--new-session] [--dry-run] [--dsp] [claude-flags...]`
Short form: `ccw -n <name> ...`.
- Without `-w`: creates (or reuses) `<new_project_root>/<name>` and starts
  `claude` there.
- With `-w <branch>`: uses the git repo inside `<new_project_root>/<name>`
  (the directory must exist and be a git repo).
- `--attach-existing` / `--new-session`: bypass the repeat prompt (see
  [Repeat behavior](#repeat-behavior)).

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
`blessed` Python package). It offers:

- **Actions** at the top:
  1. *New session in current folder* — `ccw run` equivalent.
  2. *Create new project* — prompts for a name, creates it under
     `new_project_root`, and starts `claude`.
  3. *Open existing project* — pick an existing directory under
     `new_project_root`.
- **Sessions list** (your own) with live CPU/RAM, attached marker, and recency.
- **⚡ Superuser block** (whenever passwordless sudo is detected):
  - Other users' sessions with a yellow `USER` column (if any exist).
    `Enter` attaches via `sudo -iu <user>`, `d` kills the highlighted one.
  - *⚙ Settings* — opens a repo-level `config.toml` editor (see
    [Superuser mode](#superuser-mode-tui)).
  - *Kill ALL ccw sessions (all users)* — appears when at least one session
    exists anywhere; requires typing `kill-all-global` to confirm.
- **Permissions prompt** before every launch: choose between regular and
  `--dangerously-skip-permissions`.

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

- Plain: `cc-<slug(dirname)>`
- Worktree: `cc-<slug(repo)>-<slug(branch)>` (collapses to `cc-<repo>` when
  slugs match)
- Parallels: suffix `-2`, `-3`, … auto-allocated on demand

The prefix `cc-` is configurable via `session_prefix`.

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

`ccw` refuses to run in directories outside `allowed_roots`. This guards
against accidentally starting `claude` in `$HOME`, `/tmp`, system paths, etc.
Configure via `allowed_roots` in `config.toml`.

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
2. Otherwise `sudo -n -v` with a 0.5 s timeout. Exit 0 → True.

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
   - Saves rewrite `config/config.toml` (using `sudo` automatically when
     the file is not directly writable). **Comments in `config.toml` are
     lost on save** — that is the intentional tradeoff for a stdlib-only
     writer.
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
| `session_prefix` | string | `"cc-"` | Tmux session name prefix. |
| `default_claude_args` | array | `[]` | Prepended to every `claude` invocation. |
| `tmux_socket_template` | string | `/tmp/ccw-{user}.sock` | Per-user socket path. Placeholders: `{user}`, `{uid}`. |
| `repeat_noninteractive_mode` | `"fail"` / `"attach"` / `"new"` | `"fail"` | Non-TTY fallback for repeat prompt. |

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

## Install

```bash
sudo python3 install/install_ccw.py \
  --repo-dir /srv/apps/vz_devagent_cli_tool \
  --install-path /usr/local/bin/ccw
```

`blessed` (for the TUI) is optional: `pip install blessed`. Without it,
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

- `bin/ccw` — CLI entrypoint (stdlib-only; thin wrapper around `lib/`).
- `lib/ccw_tui.py` — interactive TUI main loop + main-screen rendering.
- `lib/ccw_tui_widgets.py` — reusable TUI primitives (`dim`,
  `confirm_phrase`, `confirm_yn`, `text_input`, `flash_error`).
- `lib/ccw_tui_settings.py` — superuser *⚙ Settings* sub-screens.
- `lib/ccw_settings.py` — settings schema + repo-level TOML read/write.
- `install/install_ccw.py` — installs `/usr/local/bin/ccw` symlink.
- `install/render_ccw_config.py` — renders `config.toml` from JSON.
- `tests/` — unit tests.
- `examples/ccw-config.json` — example config-rendering payload.
- `config/` — host config dir (gitignored).
- `VERSION` — release version.

---

## Local checks

```bash
python3 -m py_compile bin/ccw lib/ccw_tui.py lib/ccw_tui_widgets.py \
  lib/ccw_tui_settings.py lib/ccw_settings.py \
  tests/test_ccw.py tests/test_ccw_tui.py tests/test_ccw_settings.py \
  install/install_ccw.py install/render_ccw_config.py
python3 -m unittest discover -s tests -p 'test_*.py'
```

CI runs the same two checks on pushes to `main` and pull requests.

---

## Release / rollout checklist

1. Update code, tests, docs, and `VERSION`.
2. Run local checks plus `ccw doctor` against a rendered repo-local config.
3. Commit and push.
4. Deploy the exact ref to each host.
5. Verify `ccw --version`, `ccw doctor`, repeat-session behavior, and socket
   path on each host.
6. Update infra runbooks, host passports, and change logs.

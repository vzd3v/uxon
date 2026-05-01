# Configuration guide

`uxon` works with **no configuration at all** — the launch user can
run an agent in any folder they have write access to, and `uxon new`
creates fresh projects under `~/projects`. This guide explains *when
and why* you'd want to change that.

Two layers, merged in this order (later wins):

1. **Repo config** — `<repo>/config/config.toml`, host-wide.
   `config/config.example.toml` is the tracked starting point.
2. **Project config** — the nearest `.uxon.toml` in `cwd` or a
   parent that is itself inside an `allowed_roots` entry. Useful to
   override one or two keys for a single project. The TUI never
   writes `.uxon.toml`.

Settings can also be edited live from the TUI's ⚙ Settings screen
(superuser block) — it round-trips `config.toml` via `tomlkit`,
preserving comments and formatting.

---

## Use case: single user on a laptop

You're the only user. You install `uxon`, type `uxon`, and it
should just work.

What you need: nothing. Defaults are tuned for this case.

- `default_launch_mode = "caller"` — the agent runs as you.
- `allowed_roots = []` — your `$HOME` is implicitly allowed for
  `uxon run`, and the TUI's "New session in current folder"
  action is gated on **write access**, not on `allowed_roots`. So
  any folder you can write to works.
- `new_project_root = "~/projects"` — `uxon new myproj` creates
  `~/projects/myproj` and launches there.

Optional tweaks:

- Switch the default agent: `agents.default = "codex"` (and add it
  to `agents.enabled`).
- Add per-agent flags every session should get:
  `agents.claude.default_args = ["--model", "claude-sonnet-4-6"]`.

---

## Use case: shared host with several users

Several developers SSH into the same box. Each runs their own
agent, with their own keys and quotas, in their own home
directory. Operators with `sudo` see everything.

What you need:

```toml
default_launch_mode = "caller"   # the agent runs as the SSH user
session_users = ["alice", "bob", "carol"]
enable_all_users_list = true     # `uxon list --all-users` works
```

`session_users` populates the TUI's superuser block (visible to
anyone with passwordless `sudo`) and the scope of `uxon list
--all-users`. Add every developer who'll run agents.

Each launch user automatically gets a private `tmux` socket
(`/tmp/uxon-<user>.sock`) — no cross-user session leakage.

---

## Use case: dedicated low-privilege "agent" user

You want every agent to run as a sandboxed OS user (e.g. `agent`)
with limited filesystem access, regardless of who logged in. The
caller stays themselves; the agent runs as the sandbox account
via `sudo -iu agent`.

What you need:

```toml
default_launch_mode = "fixed"
runtime_user        = "agent"
session_users       = ["agent"]
allowed_roots       = ["/srv/projects"]   # what 'agent' can touch
new_project_root    = "/srv/projects"
```

Plus, on the host: passwordless `sudo -iu agent` for every caller
who's allowed to launch.

Variant — **per-caller mapping** (different callers run as
different sandbox accounts):

```toml
default_launch_mode = "fixed"
runtime_user        = "agent"           # fallback for unmapped callers

[launch_user_by_caller]
alice = "agent-alice"
bob   = "agent-bob"
```

Resolution order:
1. `launch_user_by_caller[<caller>]` if set;
2. else, `default_launch_mode = "caller"` → caller is the launch
   user;
3. else → `runtime_user`.

---

## Use case: pin agents to specific directories

You want the agent to never run in random places — only inside
the project areas you've sanctioned. This matters more on a shared
box than on a laptop.

```toml
allowed_roots = [
  "/srv/projects",
  "/srv/sandbox",
]
new_project_root = "/srv/projects"
```

Effects:

- `uxon run` requires `cwd` to be under one of these
  (the launch user's `$HOME` is *additionally* implicit unless you
  also drop `$HOME` from this set by leaving it absent — note
  that `$HOME` is **always** implicit for `run`, by design).
- `uxon new <name>` requires `<new_project_root>/<name>` to be
  under `allowed_roots`. There is no `$HOME` fallback for `new`.
- The TUI superuser action "Open existing project" lists folders
  under `new_project_root`.
- The TUI's "New session in current folder" still works wherever
  the launch user has write access — `allowed_roots` is enforced
  at the CLI / `new` boundary, not in the TUI's launch action.

---

## Use case: GitHub repo creation on new project

You'd like `uxon new myproj --git-remote default` (or the TUI
"Create new project" flow) to also create a fresh GitHub repo
before launching the agent.

```toml
git_create_enabled         = true
default_git_remote_profile = "personal"

[[git_remote_profiles]]
name       = "personal"
host       = "github.com"
owner      = "your-username"
auth       = "gh"               # uses `gh repo create` under creds_user
creds_user = "your-os-user"     # whose ~/.config/gh/hosts.yml token to use
visibility = "private"

[[git_remote_profiles]]
name       = "acme-org"
host       = "github.com"
owner      = "acme"
auth       = "token"            # fine-grained PAT via REST API
creds_user = "your-os-user"
token_file = "/home/your-os-user/.secrets/uxon-acme.token"
visibility = "private"
```

Notes:

- `uxon` only ever creates repos for profiles in this whitelist.
  No `<owner>` outside the table is reachable.
- `auth = "token"` reads the PAT from `token_file` under
  `creds_user`. The token is held only for the duration of the
  REST call, never logged, never echoed in `--dry-run`. `repo`
  scope is the minimum.
- `creds_user` is the OS user whose credentials are used for the
  *create* step. Local `git init` / `commit` / `push` always run
  under the launch user. `creds_user` defaults to launch user.
- `uxon doctor` prints one line per profile with `ok` /
  `warn:<reason>` for: passwordless sudo to `creds_user`, presence
  of `gh`, login status or `token_file` readability. It never
  attempts the create call.
- The CLI is non-interactive: `uxon new` only touches git when you
  pass `--git-remote <profile>`. The TUI prompts.

If a step fails, the local `.git` is left in place for inspection.
The error names which stage failed: `preflight` / `local_init` /
`remote_create` / `push`.

---

## Use case: migrating from a previous session prefix

You changed `session_prefix` and have running sessions under the
old value that you don't want to lose track of.

```toml
session_prefix          = "uxon-"
legacy_session_prefixes = ["old-"]
```

`list`, `attach`, `kill`, and `kill-all` recognise both prefixes.
New sessions are *always* created under `session_prefix`. `uxon`
never *creates* a session under a legacy prefix.

---

## Use case: tweak refresh cadence on a slow link

Defaults: TUI refreshes every 2 s, the SSH-link probe (only shown
inside an SSH session) every 10 s. On a high-latency link, slow
both down to keep the screen calm.

```toml
tui_refresh_interval_seconds      = 5.0
tui_ssh_refresh_interval_seconds  = 30.0
```

---

## Reference: every key

| Key | Type | Default | Purpose |
|-----|------|---------|---------|
| `runtime_user` | string | `""` | Launch user when `default_launch_mode = "fixed"`. |
| `default_launch_mode` | `"caller"` / `"fixed"` | `"caller"` | Launch-user resolution for callers without a mapping. |
| `launch_user_by_caller` | table | `{}` | Per-caller override (`<caller> = <launch user>`). |
| `session_users` | array | `[]` | Users scanned by `list --all-users` and the TUI superuser block. |
| `enable_all_users_list` | bool | `false` | Enables `list --all-users`. |
| `allowed_roots` | array | `[]` | Directories `uxon` will launch agents in. The launch user's `$HOME` is implicitly allowed for `run`. The TUI's "New session in current folder" gates on write access, not on this list. |
| `new_project_root` | string | `~/projects` | Base directory for `uxon new <name>`. Must be inside `allowed_roots` (or under `$HOME`). |
| `session_prefix` | string | `"uxon-"` | TMUX session-name prefix for new sessions. |
| `legacy_session_prefixes` | array | `[]` | Extra prefixes recognised by `list`/`attach`/`kill`. Never used to create new sessions. |
| `agents.enabled` | array | `["claude"]` | Ordered list of enabled agent ids (`claude`, `codex`, `cursor`). |
| `agents.default` | string | `"claude"` | Default agent when `--agent` is not passed. Must be in `agents.enabled`. |
| `agents.<id>.default_args` | array | `[]` | Flags prepended to every invocation of that agent. |
| `tmux_socket_template` | string | `/tmp/uxon-{user}.sock` | Per-user socket path. Placeholders: `{user}`, `{uid}`. |
| `tui_refresh_interval_seconds` | number | `2.0` | TUI auto-refresh interval. |
| `tui_ssh_refresh_interval_seconds` | number | `10.0` | `ssh-link` probe interval (only visible inside SSH). |
| `repeat_noninteractive_mode` | `"fail"` / `"attach"` / `"new"` | `"fail"` | Non-TTY fallback when `uxon new` finds an existing matching session. |
| `git_create_enabled` | bool | `false` | Master switch for GitHub repo creation on new project. |
| `default_git_remote_profile` | string | `""` | Profile picked by `--git-remote default` and the TUI default. |
| `git_remote_profiles` | array of tables | `[]` | Whitelist of allowed targets (see above). |

## Reference: environment variables

| Variable | Effect |
|----------|--------|
| `UXON_REPEAT_NONINTERACTIVE_POLICY` | Overrides `repeat_noninteractive_mode` per invocation (`fail` / `attach` / `new`). |
| `UXON_LOG_DIR` | Overrides the TUI event-log directory. Default: `${XDG_STATE_HOME:-~/.local/state}/uxon`. |
| `SUDO_USER` | Honoured when `uxon` is invoked via `sudo` to identify the real caller. |

## Rendering config from JSON

For multi-host rollouts, generate `config.toml` from a single JSON
payload — the canonical pattern for keeping fleets in sync:

```bash
python3 install/render_uxon_config.py \
  --config-json examples/uxon-config.json \
  --output config/config.toml
```

See [`docs/deployment.md`](deployment.md) for the multi-host
operating model, including how to combine this with config
management tooling.

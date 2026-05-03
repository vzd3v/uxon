# Configuration guide

`uxon run` works with **no configuration at all** — the launch user
can run an agent in any folder they have write access to. `uxon new`
(creating a project) needs one config key set: `allowed_roots` must be
non-empty and cover `new_project_root`. This guide explains *when
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

## Scenarios at a glance

`uxon` supports four shapes of deployment, along two axes — how many
developers share the host, and how many hosts are in the picture.
Every config key falls out of where you sit on this grid.

|              | One host                                                    | Multiple hosts                                                  |
|--------------|-------------------------------------------------------------|-----------------------------------------------------------------|
| **Solo** (one developer)         | [Solo on a single host](#solo-on-a-single-host)           | [Solo on multiple hosts](#solo-on-multiple-hosts)             |
| **Team** (several developers)    | [Team on a single host](#team-on-a-single-host)           | [Team on multiple hosts](#team-on-multiple-hosts)             |

### Recommended workflow: pair every shell user with a `<user>_agent`

Across all four scenarios the recommended pattern is the same:
**give each shell user a low-privilege OS account that owns the
agent's runtime**, e.g. `vz` (you) + `vz_agent`, or `alice` +
`alice_agent`. The agent runs as `<user>_agent` via `sudo -iu`;
your shell user stays the trust boundary you keep dotfiles, SSH
keys, and credentials in.

This matters more with each rung of agent autonomy: plain mode asks
before every tool use; `--auto` skips a class of prompts; `--dsp`
("yolo") skips them all. The blast radius of "yolo + bug + prompt
injection" is whatever the launch user can write to. With the
paired sandbox, that radius is `<user>_agent`'s files — not your
home directory, your SSH keys, or your team's shared filesystem.

The simpler `default_launch_mode = "caller"` (agent runs as you) is
supported in every scenario for setups that don't intend to use
`--dsp` and accept the larger blast radius.

Capabilities by scenario:

| Capability | solo·1 | solo·N | team·1 | team·N |
|---|:---:|:---:|:---:|:---:|
| Paired sandbox (`<user>_agent`, `sudo -iu`)                       | recommended | recommended | recommended | recommended |
| Launch as caller (`default_launch_mode = "caller"`)              | ✓ | ✓ | ✓ | ✓ |
| Per-caller mapping (`[launch_user_by_caller]`)                   | — | — | ✓ | ✓ |
| `session_users` + `--all-users` cross-user listing               | — | — | ✓ | ✓ |
| Per-user `tmux` socket (`/tmp/uxon-<user>.sock`)                 | ✓ | ✓ | ✓ | ✓ |
| `allowed_roots` strict whitelist                                  | optional | optional | recommended | recommended |
| TUI superuser block (auto-detected via `sudo`)                   | — | — | ✓ | ✓ |
| Multi-host aggregation (`[[remote_hosts]]`, `--all-hosts`)       | — | ✓ | — | ✓ |

---

## Solo on a single host

You're the only user. The recommended setup pairs your shell user
(say `vz`) with a low-privilege agent account (`vz_agent`); the
agent runs as `vz_agent` via `sudo -iu`. The simpler caller mode
(agent runs as you) is documented below as an alternative.

### Recommended: paired sandbox user

One-time host setup:

```bash
sudo useradd -m -s /bin/bash vz_agent
# Allow your shell user to sudo into the sandbox without a password:
echo 'vz ALL=(vz_agent) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/uxon-vz-agent
sudo chmod 440 /etc/sudoers.d/uxon-vz-agent
# Give vz_agent a workspace it owns:
sudo install -d -o vz_agent -g vz_agent /srv/projects
```

`config/config.toml`:

```toml
default_launch_mode = "fixed"
runtime_user        = "vz_agent"
session_users       = ["vz_agent"]
allowed_roots       = ["/srv/projects"]
new_project_root    = "/srv/projects"
```

What you get:

- The agent runs entirely as `vz_agent`, with its own home, its
  own `~/.claude/`, its own `~/.gitconfig`. Your dotfiles, SSH
  keys, and credentials are not in reach.
- `--dsp` ("yolo") runs blow up `vz_agent`'s sandbox, not your
  account.
- Same primitive scales 1:1 to teams (one `<user>_agent` per
  developer via `[launch_user_by_caller]`).

If the agent needs your SSH keys (e.g. to push to private repos),
forward them explicitly: `ssh -A` from your laptop, and ensure
`vz_agent` can read your `SSH_AUTH_SOCK` (group ACL, or set up the
agent forwarding inside the `sudo -iu` step).

### Simplest: agent runs as you

If you don't intend to run with `--dsp` and accept that the agent
shares your trust boundary, skip the sandbox account:

```toml
default_launch_mode = "caller"
```

`uxon run` and the TUI's "New session in current folder" need
nothing else — they gate on write access. For `uxon new <name>`,
add:

```toml
allowed_roots    = ["~/projects"]
new_project_root = "~/projects"
```

### Optional tweaks (either mode)

- Switch the default agent: `agents.default = "codex"` (and add it
  to `agents.enabled`).
- Add per-agent flags every session should get:
  `agents.claude.default_args = ["--model", "claude-sonnet-4-6"]`.

---

## Solo on multiple hosts

You're the only user, but agents run on more than one server (one
per project, or production / staging / scratch). Each peer runs an
independent `uxon` install; one machine — typically your daily
driver — aggregates the others over SSH.

On each peer: configure as
[solo on a single host](#solo-on-a-single-host) — the recommended
paired-sandbox setup applies per host (`vz_agent` on each peer, or
a host-specific name like `vz_agent_prod1`). The aggregator host
itself uses the same pattern locally.

On the aggregator, add one `[[remote_hosts]]` block per peer:

```toml
[[remote_hosts]]
name      = "vz-prod1"
ssh_alias = "vz-prod1"     # auth/port live in ~/.ssh/config

[[remote_hosts]]
name      = "vz-scratch"
ssh_alias = "vz-scratch"
```

What this unlocks:

- The TUI grows a `── remote sessions ──` block with sessions from
  every peer, refreshed on `tui_ssh_refresh_interval_seconds`.
- `uxon list --all-hosts` and `uxon list --host <name>` work from
  the CLI; pair with `--json` for scripting (JSON Lines, one
  envelope per source).

Destructive operations (`kill`, `kill-all`) stay local — to act on a
remote session you SSH in and run the gesture there. See
[`docs/deployment.md` § Multi-host](deployment.md#multi-host) for
the SSH model, snapshot cache, and wire schema.

---

## Team on a single host

Several developers SSH into the same box and launch agents there.
`uxon` gives the operator a single TUI that sees every agent on the
host (with `sudo`) plus the option to confine each agent to a
sandbox OS user, so a runaway tool can't write outside its corner.

Three caller-to-launch-user mappings below; (a) is the recommended
extension of the [solo paired-sandbox pattern](#solo-on-a-single-host)
to a team.

### (a) Recommended: per-caller paired sandbox

Each developer keeps their own shell user and gets a paired
`<user>_agent` account. The agent runs there via `sudo -iu`; the
TUI's superuser block lets the operator see every agent's session.

```toml
default_launch_mode = "fixed"
runtime_user        = "team_agent"      # fallback for unmapped callers
session_users       = ["alice_agent", "bob_agent", "carol_agent"]
enable_all_users_list = true
allowed_roots       = ["/srv/projects"]
new_project_root    = "/srv/projects"

[launch_user_by_caller]
alice = "alice_agent"
bob   = "bob_agent"
carol = "carol_agent"
```

Host setup, once per developer (template):

```bash
sudo useradd -m -s /bin/bash alice_agent
echo 'alice ALL=(alice_agent) NOPASSWD: ALL' \
  | sudo tee /etc/sudoers.d/uxon-alice-agent
sudo chmod 440 /etc/sudoers.d/uxon-alice-agent
```

Each launch user automatically gets a private `tmux` socket
(`/tmp/uxon-<user>.sock`) — no cross-user session leakage. Yolo
blasts stay inside the offending `<user>_agent` account.

### (b) Shared sandbox user

Every agent runs as the same sandbox account (e.g. `team_agent`),
regardless of who logged in. The caller stays themselves; the agent
runs as the shared sandbox via `sudo -iu team_agent`. Useful when
the agent needs shared state — a common workspace, a common cache —
across developers.

```toml
default_launch_mode = "fixed"
runtime_user        = "team_agent"
session_users       = ["team_agent"]
allowed_roots       = ["/srv/projects"]   # what 'team_agent' can touch
new_project_root    = "/srv/projects"
```

Plus, on the host: passwordless `sudo -iu team_agent` for every
caller who's allowed to launch. Note: with this mode every
developer's agent shares one `~/.claude/`, one `~/.gitconfig`, one
session pool — a runaway agent affects everybody.

### (c) Each developer runs as themselves (no sandbox)

The simplest setup; accept that each agent has the same trust as
its caller. Same caveat as the [solo "agent runs as you"
mode](#solo-on-a-single-host) — multiplied by the team size.

```toml
default_launch_mode   = "caller"
session_users         = ["alice", "bob", "carol"]
enable_all_users_list = true
```

`session_users` populates the TUI's superuser block (visible to
anyone with passwordless `sudo`) and the scope of `uxon list
--all-users`.

### Resolution order

For any of the three modes:

1. `launch_user_by_caller[<caller>]` if set;
2. else, `default_launch_mode = "caller"` → caller is the launch
   user;
3. else → `runtime_user`.

### Defensive perimeter — `allowed_roots`

`allowed_roots` switches `uxon run`, `uxon new -w`, and the TUI's
"New session in current folder" from "any writable folder" to a
strict whitelist:

```toml
allowed_roots = [
  "/srv/projects",
  "/srv/sandbox",
]
new_project_root = "/srv/projects"
```

There is no `$HOME`-implicit allowance and no other side allowance.
`uxon new <name>` requires `<new_project_root>/<name>` to be under
`allowed_roots`. The TUI superuser action "Open existing project"
lists folders under `new_project_root`.

---

## Team on multiple hosts

Several developers, several hosts. Each host is configured as
[team on a single host](#team-on-a-single-host) with its own users,
its own sandbox account, its own allowed-roots. One designated host
(typically the operator's workstation) aggregates the rest over SSH
via `[[remote_hosts]]`, exactly as in
[solo on multiple hosts](#solo-on-multiple-hosts):

```toml
# On the aggregator, in addition to the team·1 keys:

[[remote_hosts]]
name      = "vz-prod1"
ssh_alias = "vz-prod1"

[[remote_hosts]]
name      = "vz-prod2"
ssh_alias = "vz-prod2"
```

Operating model:

- The TUI shows local users' sessions and remote hosts' sessions in
  separate blocks; the operator sees everything in one screen.
- `uxon list --all-hosts --json` is the integration surface — JSON
  Lines, one envelope per source, suitable for piping into log
  aggregation or dashboards.
- Destructive actions stay strictly local. Reaping an agent on a
  peer means SSHing in. The rationale is in
  [`docs/deployment.md` § Multi-host](deployment.md#multi-host).

For the multi-host install / rollout pattern (one venv path per
host, JSON-rendered configs, pinned refs), see
[`docs/deployment.md`](deployment.md).

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
| `allowed_roots` | array | `[]` | When empty: `uxon run` and the TUI's "New session in current folder" gate on **write access** alone. When non-empty: strict whitelist — `uxon run` / `uxon new -w` / the TUI's current-folder action all refuse anything outside the listed paths (no `$HOME`-implicit, no other implicit allowance). `uxon new` (creating a project) always requires a non-empty whitelist that covers `new_project_root`. |
| `new_project_root` | string | `~/projects` | Base directory for `uxon new <name>`. Must be inside `allowed_roots`. |
| `session_prefix` | string | `"uxon-"` | TMUX session-name prefix for new sessions. |
| `legacy_session_prefixes` | array | `[]` | Extra prefixes recognised by `list`/`attach`/`kill`. Never used to create new sessions. |
| `agents.enabled` | array | `["claude"]` | Ordered list of enabled agent ids (`claude`, `codex`, `cursor`). |
| `agents.default` | string | `"claude"` | Default agent when `--agent` is not passed. Must be in `agents.enabled`. |
| `agents.<id>.default_args` | array | `[]` | Flags prepended to every invocation of that agent. |
| `tmux_socket_template` | string | `/tmp/uxon-{user}.sock` | Per-user socket path. Placeholders: `{user}`, `{uid}`. |
| `tui_refresh_interval_seconds` | number | `2.0` | Local-tmux refresh cadence. |
| `tui_ssh_refresh_interval_seconds` | number | `10.0` | Cadence for SSH-driven streams: the `ssh-link` probe (visible inside SSH) and the per-peer remote-sessions poller (when `[[remote_hosts]]` is configured). |
| `repeat_noninteractive_mode` | `"fail"` / `"attach"` / `"new"` | `"fail"` | Non-TTY fallback when `uxon new` finds an existing matching session. |
| `git_create_enabled` | bool | `false` | Master switch for GitHub repo creation on new project. |
| `default_git_remote_profile` | string | `""` | Profile picked by `--git-remote default` and the TUI default. |
| `git_remote_profiles` | array of tables | `[]` | Whitelist of allowed targets (see above). |
| `remote_hosts` | array of tables | `[]` | Peer hosts polled over SSH for the multi-host TUI block and `uxon list --host`/`--all-hosts`. See [`docs/deployment.md` § Multi-host](deployment.md#multi-host). |

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

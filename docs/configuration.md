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
paired-account, that radius is `<user>_agent`'s files — not your
home directory, your SSH keys, or your team's shared filesystem.

The boundary works in both directions: `<user>_agent` is a separate
OS user with its own home, so the agent has no implicit access to
your shell user's files. Anything you want it to see (the project
working tree, an SSH agent socket, a credentials file) you opt in
to explicitly via group ACLs, bind-mounts, or the `sudo -iu` step
itself.

> **uxon does not add a sandbox of its own.** Isolation between
> `<user>_agent` and the rest of the host is whatever ordinary Unix
> UID separation provides — file permissions, process ownership,
> per-user `tmux` sockets. uxon does not configure cgroups,
> AppArmor, seccomp, or kernel namespaces. The "paired-account"
> term throughout these docs is shorthand for the OS-account-pair
> pattern, not a claim of containerised isolation.

The simpler `default_launch_mode = "caller"` (agent runs as you) is
supported in every scenario for setups that don't intend to run
yolo-mode (`--dsp`) and accept the larger blast radius.

Capabilities by scenario:

| Capability | solo·1 | solo·N | team·1 | team·N |
|---|:---:|:---:|:---:|:---:|
| Paired-account (`<user>_agent`, `sudo -iu`)                       | recommended | recommended | recommended | recommended |
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

### Recommended: paired-account setup

One-time host setup:

```bash
sudo useradd -m -s /bin/bash vz_agent
# Allow your shell user to sudo into the agent account without a password:
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
- A yolo-mode run (`--dsp`) blows up `vz_agent`'s files, not your
  account.
- Same primitive scales 1:1 to teams (one `<user>_agent` per
  developer via `[launch_user_by_caller]`).

If the agent needs your SSH keys (e.g. to push to private repos),
forward them explicitly: `ssh -A` from your laptop, and ensure
`vz_agent` can read your `SSH_AUTH_SOCK` (group ACL, or set up the
agent forwarding inside the `sudo -iu` step).

### Simplest: agent runs as you

If you don't intend to run yolo-mode (`--dsp`) and accept that the
agent shares your trust boundary, skip the agent account:

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
paired-account setup applies per host (`vz_agent` on each peer, or
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

- The TUI's session dashboard adds one row per session on each peer
  (a `HOST` column appears automatically when peers are configured),
  refreshed on `tui_ssh_refresh_interval_seconds`.
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
low-priv OS user, so a runaway tool can't write outside its corner.

Three caller-to-launch-user mappings below; (a) is the recommended
extension of the [solo paired-account pattern](#solo-on-a-single-host)
to a team.

### (a) Recommended: per-caller paired-account

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

The sudoers grant lets `alice` become **`alice_agent`**, not the
other way round. `alice_agent` cannot impersonate `alice`, and a
team-lead grant like `lead ALL=(alice_agent,bob_agent) NOPASSWD: ALL`
gives the lead control of agent accounts without any access to the
developers' personal accounts. See [Operator view](#operator-view-who-sees-whose-sessions)
below for the full property.

Each launch user automatically gets a private `tmux` socket
(`/tmp/uxon-<user>.sock`) — no cross-user session leakage. Yolo
blasts stay inside the offending `<user>_agent` account.

### (b) Shared low-priv account

Every agent runs as the same low-priv account (e.g. `team_agent`),
regardless of who logged in. The caller stays themselves; the agent
runs as the shared account via `sudo -iu team_agent`. Useful when
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

### (c) Each developer runs as themselves (no separate account)

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

### Operator view (who sees whose sessions)

`uxon` doesn't have an "operator role" config knob. Visibility falls
out of the caller's sudo rules, **per target**.

**Supervision without impersonation.** All grants in this section
target the agents' launch users (the `<dev>_agent` accounts), not
the developers' shell accounts. A team lead with
`lead ALL=(alice_agent,bob_agent) NOPASSWD: ALL` can attach to and
reap Alice and Bob's agent sessions (including the TUI's
`kill-all-reachable` action) — but cannot `sudo -iu alice` or
`sudo -iu bob`. The grant is over agent accounts only; it does not
let the lead become the developer, so anything that only the
developer's logged-in identity can unlock (SSH keys behind a
passphrase prompt, gh/aws sessions tied to the developer's
keychain, an unlocked browser profile) stays out of reach via this
path. This is a deliberate property of the paired-account model and
the reason it's the recommended team setup. The same contract holds
across hosts via `[[remote_hosts]]` — see
[`docs/deployment.md` § Operator view across hosts](deployment.md#operator-view-across-hosts).


- **`<caller> ALL=(ALL) NOPASSWD: ALL`** (root NOPASSWD). The TUI
  shows every user listed in `session_users` in the "Other users'
  sessions" block; `Enter` attaches via `sudo -niu <user>`; the
  `kill-all-reachable` action covers every reachable user; the
  Settings screen can write a root-owned `config.toml` via
  `sudo tee`.
- **`<caller> ALL=(alice_agent,bob_agent) NOPASSWD: ALL`**
  (per-target NOPASSWD, no root). The TUI shows only `alice_agent`
  and `bob_agent`; the section header gets a
  `(2/N users reachable)` hint when `session_users` lists more.
  `kill-all-reachable` covers exactly that subset. The Settings
  screen marks itself read-only — there's no root sudo to write the
  config file with.
- **No passwordless sudo to anyone in `session_users`.** No
  superuser block, no `--all-users` data, no `kill-all-reachable`
  action. The caller sees only their own (i.e. their `<user>_agent`'s)
  sessions.

The probe runs **once at TUI startup**. New entries in
`/etc/sudoers.d/` are picked up by quitting (`q`) and re-launching
`uxon`; there is no daemon, no `r`-key re-probe, no SIGHUP. This is
deliberate — the TUI must not get slower for capability detection.

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
its own low-priv accounts, its own allowed-roots. One designated host
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

- The TUI shows local and remote sessions in a single dashboard
  with a `HOST` column; the operator sees everything in one screen.
- `Enter` on a remote row attaches to that peer's session through
  SSH; `d` kills one highlighted remote session through the peer's
  own `uxon kill --host ... --user ...` path.
- `uxon list --all-hosts --json` is the integration surface — JSON
  Lines, one envelope per source, suitable for piping into log
  aggregation or dashboards.
- Bulk destructive actions stay strictly local. Reaping every agent
  on a peer means SSHing in and running the bulk gesture there. The
  rationale is in
  [`docs/deployment.md` § Multi-host](deployment.md#multi-host).

**Cross-user visibility on peers.** The aggregator runs
`uxon list --all-users --json` on every peer. For the peer to
return other users' sessions, its own config must have
`enable_all_users_list = true` AND the SSH user on that peer must
have per-target sudo (or root NOPASSWD) to those users — same gate
as the local TUI describes in the
[Operator view](#team-on-a-single-host) section above. If a peer
has `enable_all_users_list = false`, the aggregator falls back to
that peer's own-only sessions and the TUI labels the peer
`(own only)` in the remote-sessions block. No silent partial data:
the badge is always shown when a peer's view is degraded.

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

## Use case: dashboard columns

The TUI's session dashboard ships a default column layout that suits
most setups; the `[tui.table]` block lets you override it.

```toml
[tui.table]
columns         = ["name", "user", "cpu", "ram", "last", "cmd"]
default_sort_by = "cpu"
```

- `tui.table.columns` — list of column ids in display order. Leave
  empty (or omit) to use the registry defaults: every column whose
  `default_visible` is true plus any that the runtime layout
  promotes (`host` in multi-host setups, `user` when other-user
  rows are visible). Listing columns explicitly opts into a fixed
  visual order; ids unknown to the running uxon version are silently
  dropped (an older config carrying a since-removed column id stays
  loadable).
- `tui.table.default_sort_by` — column id used as the initial sort
  on TUI startup. Defaults to `"cpu"`. Unknown values fall back to
  `"cpu"` (with a debug-log entry on `UXON_DEBUG=tui`); the TUI
  never refuses to start because of a cosmetic setting.

Available column ids: `host`, `user`, `name`, `agent`, `cpu`,
`ram`, `new`, `last`, `cmd`, `path`, `pid`, `wins`. The full
contract (which ids are gated by which runtime flags, alignment,
formatting) lives in
[`src/uxon/tui/dashboard/columns.py`](../src/uxon/tui/dashboard/columns.py).

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

## Use case: per-host transport overrides

Each `[[remote_hosts]]` entry accepts five optional knobs. Mix and
match them per peer.

```toml
# A nearby peer — tighter SSH budget, faster polling.
[[remote_hosts]]
name       = "lab-fast"
ssh_alias  = "lab-fast"
interval         = "3s"
connect_timeout  = "1s"
total_timeout    = "5s"

# A peer reachable through a bastion. extra_ssh_options is inserted
# immediately before {ssh_alias} in the default template.
[[remote_hosts]]
name       = "edge-bastioned"
ssh_alias  = "edge1"
extra_ssh_options = ["-o", "ProxyJump=bastion.example.com"]

# A Kubernetes pod running uxon. command_template replaces the entire
# argv — extra_ssh_options and ssh_multiplex are ignored when set
# because the operator owns the transport. The collector substitutes
# {remote_command} with the standard "<remote_uxon> list ..." string.
[[remote_hosts]]
name       = "k8s-east"
ssh_alias  = "ignored"          # required by schema; unused with command_template
remote_uxon = "/usr/local/bin/uxon"
command_template = [
  "kubectl", "exec", "-n", "ops", "uxon-pod-0", "--",
  "/bin/sh", "-c", "{remote_command}",
]

# A Docker container.
[[remote_hosts]]
name             = "docker-staging"
ssh_alias        = "ignored"
remote_uxon      = "uxon"
command_template = [
  "docker", "exec", "uxon-container", "/bin/sh", "-c", "{remote_command}",
]
```

The fleet-wide `ssh_multiplex = "off"` opt-out is for environments
that disallow `ControlPersist` sockets entirely. Default `"auto"`
gives ~5–20 ms warm-tick SSH cost (vs 200–500 ms cold).

---

## Use case: sizing the host for a team

uxon does not enforce per-user resource limits — agents and their
child processes consume what the host gives them. Rough planning
numbers, for Node/Python/Go-shaped projects without heavy local
services:

- ~2 GB RAM per active agent session (the agent CLI, its tool
  invocations, and one or two child processes it leaves running);
- a disciplined developer keeps about 3 sessions open in parallel
  (one writing a feature, one fixing tests, one investigating a
  bug or doing a refactor);
- add headroom for project dev-services (DBs, watchers, build
  caches), plus 20–30 % for spikes during test runs and builds.

Translating that:

| Team shape | RAM (Node/Python) | Notes |
|---|---|---|
| 1 developer, light services | 10–16 GB | Daily-driver laptop class. |
| 2–3 developers on one host | 32 GB | Comfortable; tighter on monorepos. |
| 3–6 developers on one host | 64 GB | Recommended for shared dev-server. |
| 6+ developers, or heavy stacks (Java, large Docker, big tests, monorepos) | 128 GB+ or per-developer hosts | Re-do the math against your stack. |

CPU: budget 2–4 vCPU per active developer for light backend / web
work; more for heavy builds, integration tests, or container-heavy
workflows.

Disk: NVMe only. Each developer ends up with several copies of
project trees (worktrees, scratch checkouts), `node_modules` /
`.venv` / Docker layers, agent state, and logs. Start at
100–200 GB on a small server; do not economise here.

If you need hard limits, configure them at the OS layer:
`pam_limits` for memory / file-descriptor caps per user,
`systemd-run --scope --uid=<user> --property=MemoryMax=…` for
ad-hoc per-process limits, or per-UID `cpu` / `memory` cgroup
slices via `systemd`. uxon does not configure any of these — they
remain the operator's responsibility.

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
| `tui.table.columns` | array | `[]` | Dashboard columns in display order. Empty (or absent) uses the registry defaults; listing ids opts into a fixed order. Unknown ids are dropped silently. See [Use case: dashboard columns](#use-case-dashboard-columns). |
| `tui.table.default_sort_by` | string | `"cpu"` | Initial sort column id. Unknown values fall back to `"cpu"` (logged via `UXON_DEBUG=tui`). |
| `repeat_noninteractive_mode` | `"fail"` / `"attach"` / `"new"` | `"fail"` | Non-TTY fallback when `uxon new` finds an existing matching session. |
| `git_create_enabled` | bool | `false` | Master switch for GitHub repo creation on new project. |
| `default_git_remote_profile` | string | `""` | Profile picked by `--git-remote default` and the TUI default. |
| `git_remote_profiles` | array of tables | `[]` | Whitelist of allowed targets (see above). |
| `remote_hosts` | array of tables | `[]` | Peer hosts polled over SSH for the multi-host TUI block and `uxon list --host`/`--all-hosts`. See [`docs/deployment.md` § Multi-host](deployment.md#multi-host). Per-host options: `interval`, `connect_timeout`, `total_timeout` (durations: `"5s"`, `"500ms"`, `"2m"`, or bare seconds), `extra_ssh_options` (list of extra ssh tokens inserted before `{ssh_alias}`), `command_template` (full-argv override using placeholders `{ssh_alias}`/`{remote_uxon}`/`{connect_timeout}`/`{ssh_control_dir}`/`{remote_command}` — for kubectl-exec / docker-exec recipes). |
| `ssh_multiplex` | `"auto"` / `"off"` | `"auto"` | Adds `ControlMaster=auto`/`ControlPath`/`ControlPersist=60s` to the default fetch template (warm tick: 5–20 ms vs cold 200–500 ms). `"off"` strips the three options for environments that prohibit `ControlPersist` sockets. No effect on a host's `command_template` (operator owns that argv). |
| `fetch_concurrency` | int | `16` | Caps concurrent SSH fetch workers fleet-wide. Without a cap, a 50-host fleet recovering from an outage launches 50 concurrent `subprocess.Popen` calls (each holds ≥3 pipe FDs), saturating the default 1024-FD `ulimit` before scheduling becomes the bottleneck. |
| `audit.enabled` | bool | `true` | Application-level audit channel. When `true`, every `uxon` invocation emits structured events to journald (preferred) or `/dev/log` (fallback). The only kill-switch — there is no environment-variable override. Set to `false` to silence the channel entirely (no events, no sink detection). Per-event schema in [`docs/audit-events.md`](audit-events.md); query recipes in [`docs/deployment.md`](deployment.md#audit-channel). |
| `audit.syslog_facility` | string | `"user"` | Syslog facility name used only when the `/dev/log` fallback path is active (no journald socket). One of `kern`, `user`, `mail`, `daemon`, `auth`, `authpriv`, `cron`, `local0`–`local7`. journald native protocol carries its own metadata fields and ignores this setting. |

## Reference: environment variables

| Variable | Effect |
|----------|--------|
| `UXON_REPEAT_NONINTERACTIVE_POLICY` | Overrides `repeat_noninteractive_mode` per invocation (`fail` / `attach` / `new`). |
| `UXON_LOG_DIR` | Overrides the directory used for the developer-facing `debug` and `metrics` channels (off by default; gated on `UXON_DEBUG` / `UXON_METRICS=1`). Default: `${XDG_STATE_HOME:-~/.local/state}/uxon`. The audit channel goes to journald/syslog regardless of this variable. |
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

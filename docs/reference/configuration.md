# Configuration reference

Every config key, type, default, and semantics. For *when and why*
to set a key — see the [scenario hubs](../scenarios/solo-1.md), the
[tutorials in `start/`](../start/install.md), or the
[how-to guides in `guides/customise/`](../guides/customise/choose-launch-profile.md).

## Config file

`uxon` reads one optional, host-wide operator file:
`/etc/uxon/config.toml`. There is no environment, XDG, checkout, or project
fallback. `config/config.example.toml` is the source template to install there.

The TUI's ⚙ Settings screen atomically installs a root-owned `0644` file via a
`tomlkit` round-trip, preserving comments and formatting.

Project-owned `.uxon.toml` files are not read. Runtime policy,
launch profiles, path rules, agents, execution backends, runtimes, and git credentials
are operator-owned.

## Top-level keys

| Key | Type | Default | Purpose |
|-----|------|---------|---------|
| `default_launch_user` | string | `""` | Launch user when `default_launch_mode = "fixed"`. |
| `default_launch_mode` | `"caller"` / `"fixed"` | `"caller"` | Launch-user resolution for callers without a mapping. |
| `launch_user_by_caller` | table | `{}` | Per-caller override (`<caller> = <launch user>`). |
| `session_users` | array | `[]` | Users scanned by `list --all-users` and the TUI superuser block. |
| `enable_all_users_list` | bool | `false` | Enables `list --all-users`. |
| `allowed_roots` | array | `[]` | When empty: `uxon run` and the TUI's "New session in current folder" gate on **write access** alone. When non-empty: strict whitelist — `uxon run` / `uxon new -w` / the TUI's current-folder action all refuse anything outside the listed paths (no `$HOME`-implicit, no other implicit allowance). `uxon new` (creating a project) always requires a non-empty whitelist that covers `new_project_root`. |
| `new_project_root` | string | `~/projects` | Base directory for `uxon new <name>`. Must be inside `allowed_roots`. |
| `session_prefix` | string | `"uxon-"` | TMUX session-name prefix for new sessions. |
| `legacy_session_prefixes` | array | `[]` | Extra prefixes recognised by `list`/`attach`/`kill`. Never used to create new sessions. |
| `tmux_socket_template` | string | `/tmp/uxon-{user}-{execution_backend}.sock` | Socket path. Placeholders: `{user}`, `{uid}`, `{execution_backend}`, `{execution_fingerprint}`. |
| `launch_record_dir` | string | `""` | Authoritative launch-record directory. Empty uses the controller's private XDG state directory. Set an absolute, pre-provisioned shared directory only for multi-controller supervision; see [Launch records](#launch-records). |
| `tui_refresh_interval_seconds` | number | `2.0` | Local-tmux refresh cadence. |
| `tui_ssh_refresh_interval_seconds` | number | `10.0` | Cadence for SSH-driven streams: the `ssh-link` probe (visible inside SSH) and the per-peer remote-sessions poller (when `[[remote_hosts]]` is configured). |
| `repeat_noninteractive_mode` | `"fail"` / `"attach"` / `"new"` | `"fail"` | Non-TTY fallback when `uxon new` finds an existing matching session. |
| `worktree_root` | string | `""` | Base directory for uxon-managed worktrees. Empty = default `<repo>/.uxon/worktrees/<branch-slug>/` (excluded from git via `.git/info/exclude`). When set: `<worktree_root>/<repo-slug>/<branch-slug>/` — the admin must ensure it is writable by the launch user and inside `allowed_roots`. |
| `worktree_base` | `"local"` / `"remote"` | `"local"` | Base ref for a *new* worktree branch. `local` (default): branch off the local `origin/HEAD` if present, else local `HEAD` — no `git fetch`, no network. `remote`: `git fetch origin` first, then branch off the fetched `origin/HEAD` (claude-like; needs network + credentials). |
| `git_create_enabled` | bool | `false` | Master switch for GitHub repo creation on new project. |
| `ssh_multiplex` | `"auto"` / `"off"` | `"auto"` | Adds `ControlMaster=auto`/`ControlPath`/`ControlPersist=<ssh_control_persist_seconds>s` to the default fetch template (warm tick: 5–20 ms vs cold 200–500 ms). `"off"` strips the three options for environments that prohibit `ControlPersist` sockets. No effect on a host's `command_template` (operator owns that argv). |
| `ssh_control_persist_seconds` | int | `300` | `ControlPersist` lifetime (seconds) for the multiplex master. Must be `> 0`; to disable multiplexing entirely set `ssh_multiplex = "off"` rather than zeroing this out. Ignored when `ssh_multiplex = "off"` and per-host when `command_template` is set. |
| `fetch_concurrency` | int | `16` | Caps concurrent SSH fetch workers fleet-wide. Without a cap, a 50-host fleet recovering from an outage launches 50 concurrent `subprocess.Popen` calls (each holds ≥3 pipe FDs), saturating the default 1024-FD `ulimit` before scheduling becomes the bottleneck. Not exposed on the TUI's ⚙ Settings screen — edit `config.toml` directly. |

## `[launch]` table

Launch profiles are the runnable choices. Agents remain the
binary/mode catalog; a launch profile selects an agent and may pin a
launch user, a workload runtime, and git-remote credentials.

| Key | Type | Default | Purpose |
|-----|------|---------|---------|
| `launch.enabled_profiles` | array | `[]` | Ordered whitelist of launch profile ids. Empty or absent means auto-mode with the shipped built-in profiles `claude`, `codex`, and `cursor`. Operator-defined profiles are enabled only when listed. |
| `launch.default_profile` | string | `""` | Default launch profile. When set, it must be enabled. |

### Launch profiles (`[launch.profiles.<id>]`)

Profile ids must match `[a-z][a-z0-9_]*` and contain no `:` or `.`.

| Key | Type | Default | Purpose |
|---|---|---|---|
| `agent` | string | — | Required agent catalog id. |
| `display_name` | string | `""` | Optional human label. |
| `launch_user` | string | `""` | Optional OS-user override. Empty uses `launch_user_by_caller` / `default_launch_mode` / `default_launch_user`. |
| `runtime` | string | `"direct"` | Workload runtime id from `[runtimes.<id>]`; `direct` is built in. |
| `allowed_git_remote_profiles` | array | `[]` | Git remote profiles this launch profile may use. Empty means this launch profile cannot create remote repos. |
| `default_git_remote_profile` | string | `""` | Default for `--git-remote default`; must be listed in `allowed_git_remote_profiles`. |

```toml
[launch]
enabled_profiles = ["claude_work", "codex_safe"]
default_profile = "claude_work"

[launch.profiles.claude_work]
agent = "claude"
display_name = "Claude work"
launch_user = "wes-claude-agent"
runtime = "claude_work"
allowed_git_remote_profiles = ["work"]
default_git_remote_profile = "work"
```

### Path rules (`[[launch.path_rules]]`)

Path rules narrow launch and git-remote policy under an operator-owned
host path. Longest matching `path_prefix` wins. Paths are matched by
path component, not string prefix.

| Key | Type | Default | Purpose |
|---|---|---|---|
| `path_prefix` | string | — | Required absolute host path. |
| `allowed_profiles` | array | — | Non-empty subset of effective enabled launch profiles. |
| `default_profile` | string | `""` | Default for this path; must be in `allowed_profiles` when set. |
| `allowed_git_remote_profiles` | array | unset | Optional narrowing list. It never grants credentials not already allowed by the selected launch profile. |
| `default_git_remote_profile` | string | `""` | Path default after narrowing; must remain allowed for every profile named by the rule. |

```toml
[[launch.path_rules]]
path_prefix = "/srv/projects/billing-api"
allowed_profiles = ["claude_work", "codex_safe"]
default_profile = "codex_safe"
allowed_git_remote_profiles = ["work"]
default_git_remote_profile = "work"
```

## `[agents.<id>]` catalog

Each `[agents.<id>]` table customises one agent in the catalog, or
declares a brand-new one. The shipped presets are `claude`, `codex`,
`cursor`; supplying a table for a new id adds it (the id must match
`[a-z][a-z0-9_]*` and contain no `:` / `.`).

| Key | Type | Default | Purpose |
|---|---|---|---|
| `agents.<id>.binary` | string | the agent `<id>` | Executable name resolved on the execution backend's inherited `PATH`, or an absolute executable path. uxon does not source a login shell. |
| `agents.<id>.version_args` | array | `["--version"]` | Argv passed by `uxon doctor` to read the version. `[]` ⇒ doctor shows no version line for this agent. |
| `agents.<id>.default_args` | array | `[]` | Flags prepended to every invocation of this agent. |
| `agents.<id>.install_hint` | string | `""` | Message shown by `doctor` / the agent picker when the binary is missing. |

Permission modes are an array-of-tables — **order is significant,
the first entry is the default mode** (used when `--mode` is
omitted). `--mode <id>` selects one by `id`; the mode ids and the
flags they map to per shipped agent are in
[`reference/cli.md`](cli.md#--mode-id).

| Key | Type | Default | Purpose |
|---|---|---|---|
| `[[agents.<id>.mode]]` | array-of-tables | — | One entry per permission mode; first = default. |
| `agents.<id>.mode.id` | string | — | Required. Mode id selected by `--mode`. |
| `agents.<id>.mode.label` | string | the mode `id` | User-facing label (TUI / audit). |
| `agents.<id>.mode.flags` | array | `[]` | Argv appended for this mode (empty for a plain/normal mode). |
| `agents.<id>.mode.dangerous` | bool | `false` | Semantic signal (audit + TUI emphasis); does not change the flags. |

**Merge rules.** Scalar/list fields (`binary`, `version_args`,
`default_args`, `install_hint`) merge **field-by-field** over the
shipped default for that id — omit a field to keep the default. The
mode list is **replace-not-merge**: supplying any `[[agents.<id>.mode]]`
table wholly replaces that id's default modes (you cannot append a
single mode); supplying none inherits the defaults. A brand-new id
(no shipped default) gets `binary = <id>`, `version_args =
["--version"]`, `default_args = []`, `install_hint = ""`, and **must**
declare at least one `[[agents.<id>.mode]]` or load fails.

```toml
[agents.claude]
default_args = ["--model", "claude-sonnet-4-6"]

# A custom agent: declares a binary and at least one mode.
[agents.myagent]
binary       = "my-agent"
install_hint = "install: pipx install my-agent"

[[agents.myagent.mode]]
id    = "normal"          # first entry → the default mode

[[agents.myagent.mode]]
id        = "yolo"
label     = "yolo (unrestricted)"
flags     = ["--auto-approve"]
dangerous = true
```

**File-only.** The agent catalog, execution backends, and runtime
tables are edited in `config.toml` directly. They are not exposed on
the TUI ⚙ Settings screen, which covers scalar keys only.

## `[execution]` table

The execution backend is the complete target-user boundary. It wraps tmux
server creation, list/attach/kill, git and worktrees, filesystem and agent
probes, runtime lifecycle, and workload launch. `local` is built in.

| Key | Type | Default | Purpose |
|---|---|---|---|
| `execution.default_backend` | string | `"local"` | Backend for users without an override. |
| `execution.backend_by_launch_user` | table | `{}` | `<launch user> = <backend id>` overrides. |
| `execution.backends.<id>.kind` | `"command"` | — | Required for configured backends. |
| `execution.backends.<id>.command_prefix` | array | — | Prefix for every target-user command. Exactly one `{user}` placeholder is required. |
| `execution.backends.<id>.probe_timeout_seconds` | number | `5.0` | Positive probe timeout. |

`command_prefix` is an argv template, not a shell command. It must be nonempty;
its first element must be an absolute executable path; and `{user}` must appear
exactly once as the only placeholder. Format conversions and format specs are
rejected. uxon appends the target command unchanged and runs a fixed internal
UID/GID probe through the resulting argv before relying on the backend.

A typical deployment uses an operator-owned helper:

```toml
[execution.backends.host_netns]
kind = "command"
command_prefix = ["/usr/bin/sudo", "-n", "--", "/usr/local/libexec/uxon-exec", "{user}", "--"]
probe_timeout_seconds = 5.0
```

The helper should validate the target user, enter the boundary, establish the
target UID/GID and supplementary groups, and `execve` the appended argv without
a shell. It must preserve the TTY and child exit status/signals. Keep the helper
and its parent directories non-writable by launch users. Configure the helper's
PATH explicitly or use absolute agent binaries; uxon never sources a login shell.

uxon does not create, migrate, or drain an operator-managed execution boundary.
Keep the boundary reachable while its tmux server has sessions, and drain those
sessions before changing the helper or backend mapping. An unreachable socket
or backend is reported as an error, never as an empty session list. The static
backend fingerprint in diagnostics and launch records describes config; it is
not a lifecycle or continuity guarantee.

## Launch records

Every managed tmux session has a controller-side record. The record binds the
tmux session id, creation time and launch nonce to its launch profile, execution
backend and workload runtime. It is finalized and fsynced before the pane is
released. The record path is never passed into the execution backend or workload.

With `launch_record_dir = ""`, records are private to one controller under its
XDG state directory (`0700` directory, `0600` files). Use this default when one
controller account owns supervision.

Multiple trusted controller accounts must use one explicitly provisioned
directory, for example `/var/lib/uxon/launch-records`. It must be root-owned,
mode `2770`, have no POSIX access ACL, and use a control group containing every
controller but no launch user. Shared records are `0640`; the launch user must
not be able to create, replace, or delete them.

Finalized records are removed after a verified successful kill. Enumeration
garbage-collects at most 1,024 records per pass across the whole record store:
pending records after 10 minutes and finalized records after 7 days when no
matching live session exists. Records contain session/profile/runtime identifiers
and timestamps, so treat the directory as operational metadata with the same
retention and access controls as audit data.

## `[runtimes.<id>]` table

`direct` is built in. A command runtime is an operator-owned workload adapter;
a container engine is one possible implementation. Runtime ids must match
`[a-z][a-z0-9_]*`.

| Key | Type | Default | Purpose |
|---|---|---|---|
| `kind` | `"command"` | — | Required. |
| `resource_scope` | `"global"` / `"per_user"` | `"global"` | Whether resources are shared across launch users. |
| `resource_name_template` | string | — | Required workload resource template. |
| `exec_prefix` | array | — | Required argv prefix prepended to the agent. |
| `telemetry` | `"none"` / `"cgroup"` | `"none"` | Optional cgroup attribution. |
| `readiness.ready_command` | array | `[]` | Exit 0 only when ready. |
| `readiness.exists_command` | array | `[]` | Exit 0 when the resource exists. |
| `readiness.on_missing` | `"fail"` / `"start"` / `"create"` | `"fail"` | Missing-resource capability. |
| `readiness.approval` | `"prompt"` / `"auto"` | `"prompt"` | TUI confirmation policy. |
| `readiness.start_command` | array | `[]` | Required for `start` and `create`. |
| `readiness.create_command` | array | `[]` | Required for `create`. |
| `identity.resolve_command` | array | `[]` | Prints JSON with `id`, positive `host_pid`, and `epoch`; required with `session.stop_command`. |
| `session.stop_command` | array | `[]` | Best-effort per-session workload teardown. |
| `timeouts.probe_seconds` | number | `10.0` | Probe timeout. |
| `timeouts.prepare_seconds` | number | `120.0` | Start/create timeout. |
| `timeouts.stop_seconds` | number | `10.0` | Teardown timeout. |
| `path_map` | table | `{}` | Host prefix to runtime prefix; longest match wins. |

Templates support `{user}`, `{launch_profile}`, `{runtime}`, `{agent}`, and
`{project_slug}`; command templates add `{resource}` and `{runtime_dir}`, while
`session.stop_command` adds `{pidfile}`. Resource names and mapped paths are
validated after expansion.

Every lifecycle command traverses the selected execution backend. The tmux
server also runs inside that execution boundary; only the agent workload gets
the additional runtime prefix. Runtime auth is operator-provisioned: uxon has
no environment or credential passthrough. Teardown is best effort and an
identity/fingerprint mismatch fails safe rather than killing an unrelated PID.

## `[tui.table]` table

| Key | Type | Default | Purpose |
|-----|------|---------|---------|
| `tui.table.columns` | array | `[]` | Dashboard columns in display order. Empty (or absent) uses the registry defaults; listing ids opts into a fixed order. Unknown ids are dropped silently (forward-compat). The `path` and `cmd` columns are hidden by default — opt back in by listing `"path"` / `"cmd"` here. |
| `tui.table.default_view` | `"by_host"` / `"flat"` | `"flat"` | Initial dashboard layout. `flat` is a single ranked list across the fleet; `by_host` shows the per-host tab strip and status bar. Toggle at runtime with `v`. ←/→ on the dashboard cycles between hosts: tabs in `by_host`, `(host, own/other)` transitions in `flat`. |

Available column ids: `host`, `user`, `name`, `agent`, `cpu`,
`ram`, `new`, `last`, `cmd`, `path`, `pid`, `wins`. The full
contract (which ids are gated by which runtime flags, alignment,
formatting) lives in
[`src/uxon/tui/dashboard/columns.py`](../../src/uxon/tui/dashboard/columns.py).

## Dashboard view + sort

Sort is a fixed contract owned by the selector — locals first
(own then other-user), then remotes in `[[remote_hosts]]`
declaration order, with within-block ranking by last-attach
descending then name ascending. There is no sort setting and no
sort cycle bindings.

The attach indicator is a glyph in the NAME column: `●` filled
when the session is attached, `○` hollow otherwise. No bold
green override.

The NAME column renders the project stem only. The `@<profile>`
suffix carried by the underlying tmux session name (visible in
`tmux ls` and the CLI `uxon list` table) is omitted here because
the AGENT column carries the underlying agent. The `-N`
disambiguator that distinguishes siblings on the same stem is
preserved (`proj@claude_work-2` → `proj-2`).

## `[tui.search]` table

| Key | Type | Default | Purpose |
|-----|------|---------|---------|
| `tui.search.fields` | array | `["name", "user"]` | Fields the dashboard search bar matches against. Allowed values: `name`, `user`, `host`, `path`, `cmd`. Unknown entries fail loud at load. |

The search bar is summoned on demand — hidden by default, press
`s` (or `/`) from anywhere to reveal it and focus the input.
`Esc` clears the query and returns focus to the widget that
summoned the bar (scoped cancel — never quits). An active search
forces the `flat` view; clearing the query restores the previous
view mode.

## `[tui]` colour palette

| Key | Type | Default | Purpose |
|-----|------|---------|---------|
| `tui.color_palette` | array | `["cyan", "blue"]` | Hue cycle assigned to remote hosts that don't pin their own colour via `[[remote_hosts]] color`. |

## `[local_host]` table

| Key | Type | Default | Purpose |
|-----|------|---------|---------|
| `local_host.color` | string | `"green"` | Block colour applied to local rows in the dashboard (own and other-user). |

## `[audit]` table

| Key | Type | Default | Purpose |
|-----|------|---------|---------|
| `audit.enabled` | bool | `true` | Application-level audit channel. When `true`, every `uxon` invocation emits structured events to journald (preferred) or `/dev/log` (fallback). The only kill-switch — there is no environment-variable override. Set to `false` to silence the channel entirely (no events, no sink detection). |
| `audit.syslog_facility` | string | `"user"` | Syslog facility name used only when the `/dev/log` fallback path is active (no journald socket). One of `kern`, `user`, `mail`, `daemon`, `auth`, `authpriv`, `cron`, `local0`–`local7`. journald native protocol carries its own metadata fields and ignores this setting. |

Per-event schema and the event alphabet are in
[`reference/audit-events.md`](audit-events.md).

## `[[remote_hosts]]` table-array

One entry per peer host the local `uxon` aggregates over SSH.

| Field | Type | Required | Default | Purpose |
|-------|------|----------|---------|---------|
| `name` | string | yes | — | Cache filename + UI label. ASCII, must match `[A-Za-z0-9_.-]+`, unique across the array. |
| `ssh_alias` | string | yes | — | Passed verbatim to `ssh`. Auth/port/identity/ProxyJump live in `~/.ssh/config`. |
| `description` | string | no | `""` | Free-form, shown in TUI tooltips. |
| `remote_uxon` | string | no | `"uxon"` | Path to `uxon` on the peer (override when peer uses a non-PATH location). |
| `interval` | duration | no | `tui_ssh_refresh_interval_seconds` | Per-peer poll cadence (`"5s"`, `"500ms"`, `"2m"`, or bare seconds). |
| `connect_timeout` | duration | no | `5s` | SSH `ConnectTimeout`. |
| `total_timeout` | duration | no | `15s` | Hard wall on the whole fetch (connect + remote run + parse). |
| `extra_ssh_options` | array | no | `[]` | Extra `ssh` tokens inserted immediately before `{ssh_alias}` in the default template. Use for `ProxyJump` / `-i identity` pinning per peer. |
| `command_template` | array | no | `[]` | Full-argv override for the fetch. Replaces the entire SSH command. Substitutes `{ssh_alias}` / `{remote_uxon}` / `{connect_timeout}` / `{ssh_control_dir}` / `{ssh_control_persist_seconds}` / `{remote_command}`. When set, `extra_ssh_options` and `ssh_multiplex` are ignored — the operator owns the transport (kubectl-exec / docker-exec recipes). |
| `color` | string | no | unset | Operator pin for the host's block colour. When unset, the TUI auto-assigns from `tui.color_palette`. Operator pins win unconditionally over the auto-cycle. |

Unknown keys in a peer block are rejected at load time with a
clear error so typos like `ssh_alaias` fail loud rather than
silently disabling the host.

## `[[git_remote_profiles]]` table-array

One entry per allowed GitHub repo-creation target.

| Field | Type | Required | Purpose |
|-------|------|----------|---------|
| `name` | string | yes | Profile id; selected via `--git-remote <name>`. |
| `host` | string | yes | Currently `"github.com"`. |
| `owner` | string | yes | Repo owner (user or org). |
| `auth` | `"gh"` / `"token"` | yes | Backend. `gh` shells out to `gh repo create` under `creds_user`; `token` calls the REST API directly with a fine-grained PAT. |
| `creds_user` | string | no | OS user whose credentials are used for the create step. Defaults to launch user. Local `git init`/`commit`/`push` always run under launch user. |
| `token_file` | string | when `auth = "token"` | Absolute path to the PAT, readable by `creds_user`. Token is held in memory only for the API call, never logged, never echoed in `--dry-run`. `repo` scope is the minimum. |
| `visibility` | `"private"` / `"public"` | no | Default when `--git-visibility` is not passed. |

`uxon` only ever creates repos for profiles in this whitelist —
no `<owner>` outside the array is reachable.

## Dashboard key bindings (summary)

The full keymap lives in
[`reference/keybindings.md`](keybindings.md); this is the
short list that the dashboard commits to:

| Key | Action |
|-----|--------|
| `q` (`й`) | Quit. Only `q` quits — `Esc` never does. |
| `r` (`к`) | Refresh now. |
| `d` (`в`) | Kill highlighted session (typed-phrase confirm). |
| `D` (`В`) | Kill all own sessions. |
| `v` (`м`) | Toggle dashboard view between `flat` and `by_host`. |
| `←` / `→` | Dashboard: in `by_host` advance the active host tab; in `flat` jump across `(host, own/other)` transitions; both cyclic. |
| `s` (or `/`) | Focus the search bar from anywhere. |
| `Esc` | Scoped cancel: clear search / close modal / leave field. Never quits. |

JCUKEN aliases (`й`/`к`/`в`/`м`) bind alongside their QWERTY
twins so the keymap survives a Russian keyboard layout without
touching `xkb`.

## Environment variables

| Variable | Effect |
|----------|--------|
| `UXON_REPEAT_NONINTERACTIVE_POLICY` | Overrides `repeat_noninteractive_mode` per invocation (`fail` / `attach` / `new`). |
| `UXON_LOG_DIR` | Overrides the directory used for the developer-facing `debug` and `metrics` channels (off by default; gated on `UXON_DEBUG` / `UXON_METRICS=1`). Default: `${XDG_STATE_HOME:-~/.local/state}/uxon`. The audit channel goes to journald/syslog regardless of this variable. |
| `UXON_DEBUG` | Comma-separated topic list enabling the `debug` JSONL channel (e.g. `tui,startup,tui-table`). Off by default. |
| `UXON_METRICS` | When set to `1`, writes per-fetch latency rows to `${state_dir}/metrics.jsonl` (rotated at 1 MiB, cap 3 files). |
| `SSH_CONNECTION` | Inspected by `audit.py` to detect peer-inbound invocations and switch local events to `*.remote.in`. |

## Rendering config from JSON (multi-host fleets)

```bash
python3 install/render_uxon_config.py \
  --config-json examples/uxon-config.json \
  --output /etc/uxon/config.toml
```

The renderer imports uxon's installed schema and accepts the complete public
config surface with the same strict keys and types as TOML. It rejects unknown
fields and never coerces booleans or scalars. A payload containing
`git_remote_profiles.token_file` paths is operationally sensitive even though it
contains no token value; protect and remove that input file accordingly.

For the multi-host operating model see
[`explain/multi-host-philosophy.md`](../explain/multi-host-philosophy.md).

## `[tmux]` managed options (3.5.0)

**Off by default.** When enabled, uxon layers a recommended set of `set`
options (see below) on top of whatever the launch user's own tmux config
(`/etc/tmux.conf`, `~/.tmux.conf`, XDG) provides, at session launch, without
editing anyone's files and without guessing config paths. The recommended
tables ship built-in, so enabling is a one-line toggle — set
`manage_options = true` under `[tmux]`; you only write your own `[tmux.*]`
tables if you want to customise the set.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `tmux.manage_options` | bool | `false` | Master switch. Default (or absent) emits none — launch argv is byte-identical to pre-3.5.0. Set `true` to emit the configured `set` commands. |
| `[tmux.options]` | table | recommended (`mouse`, `allow-passthrough`) | Rendered as `set -g <key> <value>` (global session options). Applied only when `manage_options = true`. |
| `[tmux.server_options]` | table | recommended (`extended-keys`) | Rendered as `set -s <key> <value>` (server options). Applied only when `manage_options = true`. |
| `[tmux.append_server_options]` | table | recommended (`terminal-features`) | Rendered as `set -as <key> <value>` (append to a server option's list). Applied only when `manage_options = true`. |

**Enabling and overriding.** Setting `manage_options = true` alone (e.g. from
the settings screen) applies the recommended set — the scope tables ship
built-in and stay intact. To customise, override is **per scope**: writing a
`[tmux.options]` table replaces *that* scope's defaults (re-list every global
option you want to keep) while scopes you omit — `[tmux.server_options]`,
`[tmux.append_server_options]` — keep their recommended defaults.

Values are bool / int / str and passed to tmux **verbatim** — uxon does not
validate option names or values (tmux is the authority on what is valid).
Booleans render as tmux's `on` / `off`.

**Emission order.** The chain is emitted in a fixed inter-table order —
global (`-g`) → server (`-s`) → append-server (`-as`) — and within each table
in declaration order (TOML insertion order is preserved). It is prepended to
the session-creating tmux invocation, before `new-session` (or before
`attach-session` / `switch-client` on the attach path), in a single command
(separated by bare `;` tokens).

**When it runs (server birth vs. live server).** The tmux server is **per
launch-user** and born once; these options are server-scoped, so they only
need applying when the server is born. uxon already knows whether a user's
server is live (a non-empty session list ⇒ alive), so:

- **Server birth** (the launch creates the user's first session): the **full**
  chain — `-g` + `-s` + `-as` — rides the `new-session` invocation.
- **Server already live** (any later launch or attach): uxon re-asserts only
  the **overwrite** scopes `-g` and `-s`. They are idempotent, so re-asserting
  is harmless and lets a `config.toml` edit to e.g. `mouse` take effect on the
  next launch/attach **without** a `tmux kill-server`. The **`-as`** scope is
  **not** re-emitted on a live server — `set -as` *appends* (tmux has no
  idempotent-append), so re-emitting it would grow the target list (e.g.
  duplicate `terminal-features` entries) without bound. `-as` is therefore
  applied once, at birth; editing an `[tmux.append_server_options]` value
  takes effect after a `tmux kill-server` (these are static terminal-capability
  declarations, not values one tunes at runtime).

**Fail-fast.** Because the `set` chain runs in the same invocation as
`new-session` and tmux aborts a `;`-sequence at the first failing command, a
bad option **aborts the launch — no session is created**. uxon never starts a
session whose requested options failed to apply; the operator sees tmux's
error and fixes their config. The recommended set below is verified to apply
cleanly, so only a user's own bad option trips this path.

**The recommended set** — shipped built-in and applied as soon as
`manage_options = true`; also shown for reference in
[`config/config.example.toml`](../../config/config.example.toml):

```toml
[tmux]
manage_options = true

[tmux.options]            # set -g
mouse = "on"
allow-passthrough = "on"

[tmux.server_options]     # set -s
extended-keys = "on"

[tmux.append_server_options]   # set -as
terminal-features = "xterm*:extkeys"
```

**Scope notes.** Structural validation is enforced at load time: a `[tmux]`
or `[tmux.*]` value that is not a table, or a non-scalar option leaf, fails
loud with a clear message. Options apply only on the host where the session is
**born** (each peer runs its own uxon with its own `config.toml`); the
aggregator never pushes options to peers. uxon never touches the operator's
laptop terminal or any outer tmux it cannot reach.

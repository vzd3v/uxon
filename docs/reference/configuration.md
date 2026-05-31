# Configuration reference

Every config key, type, default, and semantics. For *when and why*
to set a key — see the [scenario hubs](../scenarios/solo-1.md), the
[tutorials in `start/`](../start/install.md), or the
[how-to guides in `guides/customise/`](../guides/customise/switch-default-agent.md).

## Layers

`uxon` reads two layers, later wins:

1. **Repo config** — `<repo>/config/config.toml`, host-wide.
   `config/config.example.toml` is the tracked starting point.
2. **Project config** — the nearest `.uxon.toml` in `cwd` or a
   parent inside an `allowed_roots` entry. Per-project overrides.
   The TUI never writes `.uxon.toml`.

The TUI's ⚙ Settings screen rewrites repo config in place via a
`tomlkit` round-trip, preserving comments and formatting.

## Top-level keys

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
| `tmux_socket_template` | string | `/tmp/uxon-{user}.sock` | Per-user socket path. Placeholders: `{user}`, `{uid}`. |
| `tui_refresh_interval_seconds` | number | `2.0` | Local-tmux refresh cadence. |
| `tui_ssh_refresh_interval_seconds` | number | `10.0` | Cadence for SSH-driven streams: the `ssh-link` probe (visible inside SSH) and the per-peer remote-sessions poller (when `[[remote_hosts]]` is configured). |
| `repeat_noninteractive_mode` | `"fail"` / `"attach"` / `"new"` | `"fail"` | Non-TTY fallback when `uxon new` finds an existing matching session. |
| `worktree_root` | string | `""` | Base directory for uxon-managed worktrees. Empty = default `<repo>/.uxon/worktrees/<branch-slug>/` (excluded from git via `.git/info/exclude`). When set: `<worktree_root>/<repo-slug>/<branch-slug>/` — the admin must ensure it is writable by the launch user and inside `allowed_roots`. |
| `worktree_base` | `"local"` / `"remote"` | `"local"` | Base ref for a *new* worktree branch. `local` (default): branch off the local `origin/HEAD` if present, else local `HEAD` — no `git fetch`, no network. `remote`: `git fetch origin` first, then branch off the fetched `origin/HEAD` (claude-like; needs network + credentials). |
| `git_create_enabled` | bool | `false` | Master switch for GitHub repo creation on new project. |
| `default_git_remote_profile` | string | `""` | Profile picked by `--git-remote default` and the TUI default. |
| `ssh_multiplex` | `"auto"` / `"off"` | `"auto"` | Adds `ControlMaster=auto`/`ControlPath`/`ControlPersist=<ssh_control_persist_seconds>s` to the default fetch template (warm tick: 5–20 ms vs cold 200–500 ms). `"off"` strips the three options for environments that prohibit `ControlPersist` sockets. No effect on a host's `command_template` (operator owns that argv). |
| `ssh_control_persist_seconds` | int | `300` | `ControlPersist` lifetime (seconds) for the multiplex master. Must be `> 0`; to disable multiplexing entirely set `ssh_multiplex = "off"` rather than zeroing this out. Ignored when `ssh_multiplex = "off"` and per-host when `command_template` is set. |
| `fetch_concurrency` | int | `16` | Caps concurrent SSH fetch workers fleet-wide. Without a cap, a 50-host fleet recovering from an outage launches 50 concurrent `subprocess.Popen` calls (each holds ≥3 pipe FDs), saturating the default 1024-FD `ulimit` before scheduling becomes the bottleneck. |

## `[agents]` table

| Key | Type | Default | Purpose |
|-----|------|---------|---------|
| `agents.enabled` | array | `[]` | Strict whitelist of agent ids when non-empty (`claude`, `codex`, `cursor`); empty or absent flips uxon into **auto-mode** where every installed CATALOG agent is launchable for the launch user. |
| `agents.default` | string | `""` | Default agent when `--agent` is not passed. Optional; if unset uxon picks the first entry of `agents.enabled` (strict mode) or the first installed agent (auto-mode). Must be in `agents.enabled` when both are set. |
| `agents.<id>.default_args` | array | `[]` | Flags prepended to every invocation of that agent. |

Auto-mode vs strict whitelist:

- **`enabled = []` or absent** — auto-mode. uxon probes the host at
  startup and treats every installed CATALOG agent as launchable.
  `r` on the main screen re-probes (e.g. after `npm i -g …`).
- **`enabled = ["claude", "codex"]`** — strict whitelist. Only the
  listed agents are launchable, even if more are installed. Use this
  to pin a fleet to an approved subset (operator/CI scenarios).

`agents.enabled = []` and an absent `[agents].enabled` are
semantically identical — both mean auto. There is no explicit
"disable" mode; if you want uxon to refuse to launch anything,
uninstall the agent binaries.

Per-agent permission-mode flags are fixed by the agent binary and
not configurable here — see [`reference/cli.md`](cli.md) under
`uxon run`.

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

The NAME column renders the project stem only. The `@<agent>`
suffix carried by the underlying tmux session name (visible in
`tmux ls` and the CLI `uxon list` table) is omitted here because
the AGENT column carries it. The `-N` disambiguator that
distinguishes siblings on the same stem is preserved
(`proj@claude-2` → `proj-2`).

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
| `←` / `→` | Top action row: cycle the three buttons cyclically. Dashboard: in `by_host` advance the active host tab; in `flat` jump across `(host, own/other)` transitions; both cyclic. |
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
| `SUDO_USER` | Honoured when `uxon` is invoked via `sudo` to identify the real caller. |
| `SSH_CONNECTION` | Inspected by `audit.py` to detect peer-inbound invocations and switch local events to `*.remote.in`. |
| `UXON_AGENT_RELEASE_OK` | Internal — gates the agent-only release-class hook bypass. Not for human use. |

## Rendering config from JSON (multi-host fleets)

```bash
python3 install/render_uxon_config.py \
  --config-json examples/uxon-config.json \
  --output config/config.toml
```

`git_create_enabled`, `default_git_remote_profile`, and
`[[git_remote_profiles]]` are intentionally **not** part of the
JSON-to-TOML flow — they reference `creds_user` and `token_file`
that infra shouldn't hard-code across hosts. Hand-edit them in
`config.toml`.

For the multi-host operating model see
[`explain/multi-host-philosophy.md`](../explain/multi-host-philosophy.md).

## `[tmux]` managed options (3.5.0)

**On by default.** uxon layers a recommended set of `set` options (see below)
on top of whatever the launch user's own tmux config (`/etc/tmux.conf`,
`~/.tmux.conf`, XDG) provides, at session launch, without editing anyone's
files and without guessing config paths. You do not need to configure
anything — these are uxon's built-in defaults. Set `manage_options = false`
under `[tmux]` to opt out, or write your own `[tmux.*]` tables to override.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `tmux.manage_options` | bool | `true` | Master switch. When `true` (the default, or absent) uxon emits the configured `set` commands. Set `false` to emit none — launch argv is then byte-identical to pre-3.5.0. |
| `[tmux.options]` | table | recommended (`mouse`, `allow-passthrough`) | Rendered as `set -g <key> <value>` (global session options). |
| `[tmux.server_options]` | table | recommended (`extended-keys`) | Rendered as `set -s <key> <value>` (server options). |
| `[tmux.append_server_options]` | table | recommended (`terminal-features`) | Rendered as `set -as <key> <value>` (append to a server option's list). |

**Overriding.** Override is **per scope**: writing a `[tmux.options]` table
replaces *that* scope's defaults (re-list every global option you want to
keep) while scopes you omit — `[tmux.server_options]`,
`[tmux.append_server_options]` — keep their recommended defaults. Likewise,
toggling `manage_options` alone (e.g. from the settings screen) leaves the
recommended tables intact. To drop the managed options entirely, set
`manage_options = false`.

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

**The default (recommended) set** — applied automatically; also shown for
reference in [`config/config.example.toml`](../../config/config.example.toml):

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

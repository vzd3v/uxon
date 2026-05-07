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
| `git_create_enabled` | bool | `false` | Master switch for GitHub repo creation on new project. |
| `default_git_remote_profile` | string | `""` | Profile picked by `--git-remote default` and the TUI default. |
| `ssh_multiplex` | `"auto"` / `"off"` | `"auto"` | Adds `ControlMaster=auto`/`ControlPath`/`ControlPersist=60s` to the default fetch template (warm tick: 5–20 ms vs cold 200–500 ms). `"off"` strips the three options for environments that prohibit `ControlPersist` sockets. No effect on a host's `command_template` (operator owns that argv). |
| `fetch_concurrency` | int | `16` | Caps concurrent SSH fetch workers fleet-wide. Without a cap, a 50-host fleet recovering from an outage launches 50 concurrent `subprocess.Popen` calls (each holds ≥3 pipe FDs), saturating the default 1024-FD `ulimit` before scheduling becomes the bottleneck. |

## `[agents]` table

| Key | Type | Default | Purpose |
|-----|------|---------|---------|
| `agents.enabled` | array | `["claude"]` | Ordered list of enabled agent ids (`claude`, `codex`, `cursor`). |
| `agents.default` | string | `"claude"` | Default agent when `--agent` is not passed. Must be in `agents.enabled`. |
| `agents.<id>.default_args` | array | `[]` | Flags prepended to every invocation of that agent. |

Per-agent permission-mode flags are fixed by the agent binary and
not configurable here — see [`reference/cli.md`](cli.md) under
`uxon run`.

## `[tui.table]` table

| Key | Type | Default | Purpose |
|-----|------|---------|---------|
| `tui.table.columns` | array | `[]` | Dashboard columns in display order. Empty (or absent) uses the registry defaults; listing ids opts into a fixed order. Unknown ids are dropped silently (forward-compat). |
| `tui.table.default_sort_by` | string | `"cpu"` | Initial sort column id. Unknown values fall back to `"cpu"` (logged via `UXON_DEBUG=tui`). |

Available column ids: `host`, `user`, `name`, `agent`, `cpu`,
`ram`, `new`, `last`, `cmd`, `path`, `pid`, `wins`. The full
contract (which ids are gated by which runtime flags, alignment,
formatting) lives in
[`src/uxon/tui/dashboard/columns.py`](../../src/uxon/tui/dashboard/columns.py).

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
| `command_template` | array | no | `[]` | Full-argv override for the fetch. Replaces the entire SSH command. Substitutes `{ssh_alias}` / `{remote_uxon}` / `{connect_timeout}` / `{ssh_control_dir}` / `{remote_command}`. When set, `extra_ssh_options` and `ssh_multiplex` are ignored — the operator owns the transport (kubectl-exec / docker-exec recipes). |

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

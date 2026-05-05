# Remote attach — design

## Goal

Enable attaching to a tmux session on a configured `[[remote_hosts]]`
peer. Two entry points, both reaching the same peer-side gesture:

- TUI: `Enter` on a remote-sessions row.
- CLI: `uxon attach <id> --host <alias> [--user <u>]`.

Cross-user is in scope from v1 — the peer owns authorisation, the
aggregator never branches on `record.user`.

## Non-goals

- Bulk remote-attach. Attach is per-row. Bulk-destructive ops stay
  strictly local by existing design.
- Persistent SSH tunnels managed by uxon. SSH multiplexing already
  handled by OpenSSH `ControlMaster`; no new transport layer.
- New wire format. Peer returns nothing JSON-shaped on attach — it is
  an interactive `tmux attach`, not an RPC.
- Wire-protocol versioning for older peers. Same posture as
  `uxon kill --host` today: rolling upgrades within a fleet, opaque
  failure on stale peers.
- Confirmation modal. Local cross-user attach does not confirm
  (`tui/state.py:387` → `screens/main.py:483`); remote inherits the
  same posture.

## Key architectural decisions

### Peer owns authorisation

The aggregator dispatches `<remote_uxon> attach <id> --user <u>` over
SSH and lets the peer decide whether sudo is needed. The aggregator
never reads peer's `session_prefix`, sudoers, or socket layout. This
mirrors how `_do_kill_remote` already works.

### One ssh-argv builder for all peer ops

Today `_do_kill_remote` (`cli.py:3730-3740`) ignores
`RemoteHost.command_template` / `extra_ssh_options` — a silent bug that
hits operators with jumphost or non-standard ssh-wrapping setups.
Remote-attach reuses the same hardcoded path → propagates the bug.

Resolution: introduce one helper `build_peer_ssh_argv` in
`remote_collector.py` that all three call sites (fetch, kill, attach)
go through. Helper takes one boolean knob `allocate_tty` and reuses
the existing `_render_argv` machinery for placeholders. Fixes the
existing kill-remote bug as a side effect.

This is not scope creep — it is the canonical implementation. The
alternative ("hardcode attach the same way kill is hardcoded") doubles
down on the bug and forecloses on free SSH multiplexing for both ops.

### Rejected alternatives

- **Aggregator builds `sudo -iu <u> tmux …` directly.** Requires the
  aggregator to know peer's `session_prefix`, socket path, sudoers
  layout. Bad — same anti-pattern that `_do_kill_remote` already
  avoids by deferring to peer's `uxon kill`.
- **Separate `interactive_command_template` field.** Premature
  flexibility. `command_template` semantically describes "how to
  reach this peer"; interactive vs non-interactive lives in the call
  site, not config.
- **Drop `command_template` entirely, rely on `~/.ssh/config`.**
  ControlMaster path needs uxon-private socket to avoid colliding with
  operator's other ssh sessions. Bigger refactor, out of scope.

## Components

| Component | Layer | Responsibility |
|---|---|---|
| `build_peer_ssh_argv` | `remote_collector.py` | Single source of truth for ssh-argv to a peer. Inputs: peer record, rendered remote command, `allocate_tty` flag, multiplexing mode. Output: `list[str]` for `subprocess` / `os.execvp`. |
| `_build_fetch_argv` (refactored) | `remote_collector.py` | Thin wrapper over `build_peer_ssh_argv` with `allocate_tty=False`. Behaviour byte-for-byte identical to today (defended by snapshot test). |
| `do_attach` peer-side extension | `cli.py:2216` | Accepts `--user <u>`. Same-user path unchanged. Cross-user path mirrors `do_kill` cross-user logic (`cli.py:2422-2477`): probe sudo, surface stable `not-reachable` tag on failure, otherwise sudo into target and execvp tmux attach. |
| `attach` parser extension | `cli.py:2149-2158` | Accepts `--host`, `--user`, `--dry-run`. Same shape as `_parse_kill_extras`. |
| `_do_attach_remote` | `cli.py` | Aggregator dispatch when `--host` is set. Renders peer command `<remote_uxon> attach --user <u> <id>`, builds ssh-argv via helper with `allocate_tty=True`, `os.execvp`s. Honours `--dry-run` by printing the rendered argv. |
| `MainIntent.host` + `"attach-remote"` kind | `tui/state.py` | Pure-data intent layer extension. Factory `remote_session_intent(host_name, rec, current_user)` strips `(own only)` suffix and pulls `user`/`name` from the wire record. |
| `on_remote_attach` callback | `tui/context.py`, `tui/config.py` | Signature `(host, user, name) → LaunchRequest`. Symmetric to `on_remote_kill`. |
| Remote-row activation handler | `tui/screens/main.py:453` (`on_data_table_row_selected`) and `_run_intent` | New branches for `RemoteSessionTable` and intent kind `"attach-remote"`. Calls `ctx.on_remote_attach(...)` and hands the resulting `LaunchRequest` to `app.request_launch`. |

## CLI behaviour

- `uxon attach <id> --host <alias> --user <u>` — all three required
  for the cross-host path. Without `--user`: fail with a usage hint.
  Implicit defaults invite "where did this attach actually go?"
  surprises that we already learned to avoid in `kill --host`.
- `uxon attach <id> --host <alias> --dry-run` — print the rendered
  ssh argv (joined via `shlex.join`) and exit 0. Mirrors `kill`'s
  dry-run shape.
- `uxon attach <id>` (no `--host`) — unchanged local path.

## TUI behaviour

- `Enter` on a remote row fires `remote_session_intent` →
  `attach-remote` intent → `on_remote_attach` callback →
  `LaunchRequest` → `request_launch` → fork-and-wait → on tmux
  detach, return to TUI on the same screen.
- `(own only)` suffix stripped from the host display name when
  building the intent, matching `action_kill_remote`'s precedent
  (`screens/main.py:995`).
- After return, focus is restored to the same remote row by the
  existing `_focus_key` / `_focus_index` machinery — there is already
  a `remote:<host>/<user>/<name>` key shape (`screens/main.py:1083-1093`),
  so this is verification, not new code.

## Latency

`Enter`-to-tmux-client time is dominated by SSH channel-open. With
the default `ssh_multiplex = "auto"`, the per-host poller keeps a
warm `ControlMaster` continuously while the TUI is open
(`ControlPersist=60s`, polling cadence = `tui_ssh_refresh_interval_seconds`,
default 10s). A remote row only appears in the TUI after a successful
poll, so the master is guaranteed warm at the moment Enter fires.
Channel-open over a warm master is typically 5-20ms — visually
instant.

With `ssh_multiplex = "off"` the master is suppressed by `_strip_multiplex`
and every connection is cold (200-500ms). This is an explicit operator
opt-out and applies uniformly to fetch / kill / attach.

## Error handling

- **Peer unreachable / SSH fails before tmux starts:** ssh exits with
  non-zero, fork-and-wait returns rc, `pause_on_launch_failure`
  (`tui/launch.py:37`) holds the terminal so the user can read ssh
  stderr, prompts Enter to return. No new TUI machinery needed.
- **Cross-user sudo not granted on peer:** peer's `do_attach`
  `--user` path emits `uxon-error: not-reachable …` on stderr and
  exits 1 (stable tag, same shape as `do_kill`). Surfaces through
  the same `pause_on_launch_failure` UI.
- **Peer doesn't know `--user`** (older binary): peer fails with its
  own usage error; same UI path. Matches kill's behaviour. No
  version negotiation in v1.
- **Session disappeared between poll and attach:** peer's
  `resolve_session` raises, peer exits non-zero, same UI path.

## Testing

- **Unit (`build_peer_ssh_argv`):** default-template byte-snapshot
  for `allocate_tty=False`; `-tt` insertion test for `allocate_tty=True`;
  `ssh_multiplex="off"` strips ControlMaster block; custom
  `command_template` is honoured.
- **Refactor regression:** existing `_do_kill_remote` tests must keep
  passing after switching to the helper. If any pin argv bytes
  literally rather than semantically, update them as part of the
  refactor commit.
- **Peer-side cross-user gate:** `do_attach --user` with a target
  the prober marks unreachable returns the stable `not-reachable`
  tag. Symmetric to existing `do_kill --user` test.
- **CLI parser:** `uxon attach <id> --host h --user u` parses; missing
  `--user` with `--host` is a parse-time error.
- **CLI dry-run:** `uxon attach <id> --host h --user u --dry-run`
  prints the rendered ssh argv.
- **TUI intent:** `remote_session_intent` builds the right
  `MainIntent`; `(own only)` suffix is stripped.
- **TUI activation:** `on_data_table_row_selected` on a
  `RemoteSessionTable` row dispatches the callback with the right
  `(host, user, name)` triple. Pure-state test, no Textual loop.

## Known gaps / out of scope

- `RemoteHost.extra_ssh_options` is rendered into the default
  template via the existing fetch path. The helper inherits that
  behaviour. Operators who set `extra_ssh_options` AND replace the
  whole `command_template` already today have to make sure their
  custom template re-includes the options — this is unchanged.

## File touchpoints

- `src/uxon/remote_collector.py` — new helper, refactor `_build_fetch_argv`.
- `src/uxon/cli.py` — peer-side `do_attach --user`, parser extension,
  aggregator-side `_do_attach_remote`, on_remote_attach callback,
  switch `_do_kill_remote` to the new helper.
- `src/uxon/tui/state.py` — `MainIntent.host` field, kind
  `"attach-remote"`, `remote_session_intent` factory.
- `src/uxon/tui/context.py` — `on_remote_attach` field.
- `src/uxon/tui/config.py` — propagate `on_remote_attach`.
- `src/uxon/tui/screens/main.py` — `on_data_table_row_selected`
  branch, `_run_intent` branch, `_attach_remote_session` method.
- `src/uxon/tui/widgets/remote_session_table.py` — update docstring
  (no longer "no remote SSH gesture wired").
- `docs/configuration.md` — line 385 already promises this behaviour;
  no change needed beyond verifying the wording matches the final
  shape.
- `tests/` — see Testing section.

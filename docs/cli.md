# CLI reference

Full reference for `uxon`'s non-interactive subcommands. The
recommended entry point is the interactive TUI (`uxon` with no args
on a TTY) — see [README.md](../README.md#the-tui). Use this page
when you need a flag, an exit code, or a piece of behaviour the
README's command summary doesn't cover.

Short and long forms are equivalent unless noted. Short forms are
flagged in each section.

## Conventions

- `<id>` — session identifier. Resolution order: full name
  (`uxon-myproj@claude`), short name (`myproj@claude`), bare stem
  (`myproj`) when exactly one session matches, legacy-prefix name
  (e.g. `old-myproj` when `old-` is in `legacy_session_prefixes`),
  or active-pane PID.
- `--dry-run` — print the `tmux` command that would be executed
  instead of executing it. Available on `run`, `new`, `kill`,
  `kill-all`.
- Unknown flags after `run` / `new` are forwarded to the selected
  agent binary verbatim.
- All subcommands honour the launch-user resolution described in
  [`docs/configuration.md`](configuration.md#team-on-a-single-host)
  and run `tmux` / `git` / `mkdir` under the launch user via
  `sudo -iu` when caller ≠ launch user.
- Every state-changing subcommand emits one audit event per
  invocation (success or failure) to the platform log channel —
  see [`docs/audit-events.md`](audit-events.md) for which event each
  command fires and the fields it carries.

## `uxon` (no arguments)

- With a TTY: opens the interactive TUI.
- Without a TTY: prints usage and exits with code `2`.

## `uxon run [-w <branch>] [--dry-run] [--agent <id>] [--auto] [--dsp] [agent-flags...]`

Start an agent in the **current working directory**.

| Flag | Effect |
|------|--------|
| `--agent claude\|codex\|cursor` | Pick the agent. Default: `agents.default` from config. |
| `--auto` | Agent's "auto" permission mode. `claude` → `--permission-mode auto`. `codex` → `--full-auto`. **Not supported by `cursor`** (error). |
| `--dsp` | Agent's "yolo" permission mode. `claude` → `--dangerously-skip-permissions`. `codex` → `--dangerously-bypass-approvals-and-sandbox`. `cursor` → `--yolo`. Legacy aliases: `--dap`, `-dap`, `-dsp`. |
| `-w <branch>` | Run inside an existing git worktree branch at `cwd`. **claude only** — error for other agents. |
| `--dry-run` | Print the `tmux` command instead of executing. |

`--auto` and `--dsp` are mutually exclusive.

When `allowed_roots` is non-empty (strict-whitelist mode), the
current directory must sit under one of the listed paths. When
`allowed_roots` is empty (default), `uxon run` accepts any
directory the launch user can write to — same gate the TUI's "new
session in current folder" applies. There is no `$HOME`-implicit
allowance: setting `allowed_roots` means *only* those paths.

## `uxon new <name> [-w <branch>] [...]`

Short form: `uxon -n <name> ...`.

Without `-w`: creates (or reuses) `<new_project_root>/<name>` and
starts the agent there.

With `-w <branch>`: uses the git repo inside
`<new_project_root>/<name>` (the directory must already exist and
be a git repo — `uxon` never creates worktrees for you).

| Flag | Effect |
|------|--------|
| `--attach-existing` / `--new-session` | Bypass the repeat prompt (see [Repeat behaviour](#repeat-behaviour)). |
| `--git-remote <profile>` | Before launching, create a remote repo via the named [git remote profile](configuration.md#use-case-github-repo-creation-on-new-project). `default` uses `default_git_remote_profile`. Incompatible with `-w`. Without this flag, no git is touched (CLI is non-interactive). |
| `--git-visibility private\|public` | Override the profile's visibility default for this one call. |
| `--no-git` | Explicit "don't touch git" (same as omitting `--git-remote`). |

All flags from `run` (`--agent`, `--auto`, `--dsp`, `--dry-run`,
forwarded agent flags) also apply.

## `uxon list [--all-users] [--host <name> | --all-hosts] [--json]`

Short form: `uxon -l [--all-users]`.

Lists `uxon-*` sessions (and any sessions matching configured
`legacy_session_prefixes`) with: PID, CPU, RAM, creation time, last
attach, current command, and path.

- Default scope: the current launch user only.
- `--all-users`: scope `session_users` from config — but only the
  **reachable** subset (users the caller can `sudo -niu` to without
  a password). Unreachable users are listed once on stderr as
  `# N user(s) skipped (no sudo): <names>`; stdout stays parseable.
  In `--json`, the same names are surfaced in the new field
  `data.scope_skipped: list[str]` (forward-compatible — older peers
  omit it). Requires `enable_all_users_list = true` to be enabled at
  all; if disabled, exits with code 1 and the stable error tag
  `uxon-error: all-users-disabled`, which the multi-host aggregator
  uses to fall back to per-peer "own only" mode.
- `--host <name>`: route to a configured peer over SSH (see
  `[[remote_hosts]]` in `docs/deployment.md`). Mutually exclusive
  with `--all-hosts`.
- `--all-hosts`: print local block first, then one block per
  configured peer.
- `--json`: emit a wire-schema envelope (or JSON Lines stream for
  `--all-hosts`) instead of the human table.

## `uxon attach <id>`

Short form: `uxon -a <id>`. Re-attaches to an existing session.

Identifier resolution (first match wins):
1. Full session name — `uxon-myproj@claude`.
2. Short name without prefix — `myproj@claude`.
3. Bare stem — `myproj` (only when exactly one session matches).
4. Legacy-prefix name — e.g. `old-myproj` when `old-` is in
   `legacy_session_prefixes`.
5. Active-pane PID.

If `$TMUX` names the **same** socket as `uxon` for the launch user,
`attach` becomes `tmux switch-client` automatically.

## `uxon kill <id> [--user <name>] [--host <alias>] [--force] [--dry-run] [--json]`

Short form: `uxon -k <id>`. Kills a single session. Same identifier
resolution as `attach`.

Without `--user` / `--host`, behaves exactly as before: kills a
session owned by the current launch user on the local box.

**`--user <name>`** kills a session belonging to a different launch
user on the same host. `<name>` is a **launch user** — the OS
account that owns the tmux socket (typically `<dev>_agent` in the
recommended paired-account setup), not the developer's shell user.
The grant `<caller> ALL=(<name>) NOPASSWD: ALL` lets the caller
sudo into `<name>`, but does not give them any access to the
developer's personal account. Requires per-target NOPASSWD
(`sudo -niu <name>`) — exactly the same gating the TUI applies to
the "superuser" block. Probed once for the single target; an
unreachable target fails fast with the stable error tag
`uxon-error: not-reachable` on stderr and exit code `1`. Passing
`--user <self>` is a no-op (no probe, same as omitting the flag).

**`--host <alias>`** routes the kill to a configured `[[remote_hosts]]`
peer over SSH. The peer's own `uxon kill` does the per-target sudo
gating, so the local side does not need to know the peer's user
table. May be combined with `--user <name>` to target a specific
launch user on the peer. The wire always sends `--force` — local
confirmation is a UI gesture, not a wire concern.

**Confirmation gating** — `--user`/`--host` add a confirmation
prompt (typing the literal phrase `kill`) when running on a TTY.
`--force` skips the prompt. `--json` is non-interactive and refuses
to run without `--force` or `--dry-run`.

`--dry-run` prints the would-be tmux argv (local) or the SSH command
line (remote) instead of executing it. The probe still runs in the
local cross-user case so the dry-run output reflects reachability.

**Bulk** kill (`kill-all`) is **strictly local** — there is no
`uxon kill-all --host`. Per-session kill is the only destructive
operation that crosses hosts.

## `uxon kill-all [--force] [--dry-run]`

Alias: `uxon --killall`.

Kills every `uxon-*` (and configured legacy-prefix) session for
the current launch user. Requires interactive confirmation (typing
`kill-all`) or `--force`.

This **only** kills sessions for the current launch user. The
"kill all sessions for every reachable user on this host"
operation is TUI-only, requires passwordless `sudo`, and prompts
for `kill-all-reachable` to confirm.

## `uxon doctor` <a id="doctor"></a>

Read-only diagnostics. Always safe to run.

The TUI now surfaces `tmux` and per-agent issues in line, so most
users won't need this. Use `uxon doctor` when an in-line hint is not
enough — for example to script host inspection, capture a snapshot for
a bug report, or audit several launch users at once.

Prints:
- caller user vs launch user;
- active config paths (repo + project);
- `allowed_roots`, `new_project_root`;
- `repeat_noninteractive_mode` and any env override;
- `tmux` and agent binary paths for the launch user;
- dedicated `tmux` socket details;
- current sessions on the dedicated socket;
- any sessions on the default `tmux` socket that match
  `legacy_session_prefixes` (managed but worth noting);
- per-profile status for `[[git_remote_profiles]]` (`ok` /
  `warn:<reason>` — passwordless sudo to `creds_user`, presence of
  `gh`, login status or token-file readability);
- a list of detected configuration issues.

Use this first whenever behaviour is unexpected.

## `uxon version`

Aliases: `uxon -V`, `uxon --version`.

Prints `__version__` from the installed `uxon` package and the short
git commit (with a `-dirty` suffix when the checkout has uncommitted
changes; the commit/dirty info is only available in dev checkouts).

---

## Repeat behaviour

When `uxon new` finds a session that already matches the requested
target (same project, same agent, same worktree branch):

- **Interactive TTY** — prompts to attach, start a parallel
  session, or cancel.
- **Non-interactive**, resolved in this order:
  1. Explicit flag: `--attach-existing` or `--new-session`.
  2. Env var: `UXON_REPEAT_NONINTERACTIVE_POLICY=fail|attach|new`.
  3. Config key: `repeat_noninteractive_mode` (default `fail`).

Compatible sessions found **only** under a legacy prefix on the
default `tmux` socket cause `uxon new` to fail with an explicit
hint, instead of silently creating a duplicate on the dedicated
socket.

## Worktrees (`-w <branch>`)

- `uxon run -w <branch>` — uses the git repo at `cwd`.
- `uxon new <name> -w <branch>` — uses the repo inside
  `<new_project_root>/<name>`. Directory must already exist and be
  a git repo. `uxon` never creates worktrees for you.
- The session name includes both repo and branch slugs, so multiple
  branches of the same repo coexist cleanly.
- **Currently `claude`-only.** Using `-w` with `codex` or `cursor`
  is an error.

## Session naming

```
uxon-<stem>@<agent>             plain
uxon-<repo>-<branch>@<agent>    worktree
uxon-<stem>@<agent>-N           parallels (N appended after the agent)
```

The `uxon-` prefix is the default and is configurable via
`session_prefix`. Operators upgrading from a previous prefix list
the old value in `legacy_session_prefixes` so existing sessions
remain reachable via `list` / `attach` / `kill`. `uxon` never
*creates* sessions under a legacy prefix.

## `--dsp` (dangerously-skip-permissions)

`--dsp` is the canonical short form for the agent's "skip
permission prompts" mode. The TUI asks explicitly before every
launch; the CLI requires the flag.

Legacy aliases accepted for back-compat: `--dap`, `-dap`, `-dsp`.

## Environment variables

| Variable | Effect |
|----------|--------|
| `UXON_REPEAT_NONINTERACTIVE_POLICY` | Overrides `repeat_noninteractive_mode` per invocation (`fail` / `attach` / `new`). |
| `UXON_LOG_DIR` | Overrides the TUI event-log directory. Default: `${XDG_STATE_HOME:-~/.local/state}/uxon`. |
| `SUDO_USER` | Honoured when `uxon` is invoked via `sudo` to identify the real caller. |

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success. |
| `1` | Runtime failure: target unreachable (`uxon-error: not-reachable`), live `--host` fetch failed without a usable cache, SSH timeout, peer rc non-zero. |
| `2` | Usage error (bad flags, no TTY for the bare TUI invocation, unknown subcommand, unknown `--host` alias). |
| `130` | User cancelled the confirmation prompt. |
| `non-zero from forked tmux/agent` | Surfaced to the caller as-is. The TUI pauses with a banner so you can read stderr. `0` (success) and `130` (Ctrl-C inside the agent) do not pause. |

## Failure-mode notes

- **Allowed-roots mismatch** — `run` / `new` exit before touching
  `tmux`. Add the directory to `allowed_roots` or move the project.
- **Foreign `tmux` server** — when `$TMUX` names a socket `uxon`
  doesn't manage, `uxon` prints `Ctrl-b d first` and exits cleanly.
- **`textual` missing** — non-TUI subcommands all keep working;
  the bare-TUI invocation prints an install hint and exits.
- **Git remote creation failure** — local `.git` is left in place
  for inspection. The error names which stage failed:
  `preflight` / `local_init` / `remote_create` / `push`.

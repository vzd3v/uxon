# Respond to a rogue agent

A `--dsp` ("yolo") session is destroying files, eating CPU, or
making API calls you can't afford. Stop it, preserve enough
state to understand what happened, then revoke whatever it had
access to. Order matters: stop fast, forensics second, recovery
third.

## Solo·1 without a paired account — short branch

If you're solo and the agent runs as your own shell user (no
`<user>_agent`), the OS-user containment isn't there. The
recipe collapses to:

1. `Ctrl-c` inside the agent's pane (or detach with `Ctrl-b d`
   and `uxon kill <session>` from another shell). The agent
   sees a SIGINT — most yolo runs unwind cleanly.
2. If the agent is wedged: `pgrep -f claude` (or `codex` /
   `cursor-agent`) and `kill -STOP <pid>` to freeze, then
   `kill -TERM <pid>`.
3. `git status && git diff` in the project tree to see what
   the agent left uncommitted. Stash unwanted changes,
   `git restore` deleted files (within reflog window).
4. Revoke API keys at the provider if the run could have
   leaked them. Solo means *all* your keys are in the agent's
   reach — yes, including `~/.aws/`, `~/.claude/`, anything
   sourced from `.envrc`.

The rest of this page assumes a team setup with paired
accounts. After the next quiet moment, set up a `<user>_agent`
— [`start/solo-1-quickstart.md`](../../start/solo-1-quickstart.md#recommended-paired-account)
shows the 5-line one-time setup that prevents this branch from
existing again.

## Triage in 30 seconds

1. **Identify** the offending session in the TUI: `s` to sort
   by CPU, `S` to flip direction. The runaway is at the top.
   Note the `USER`, `HOST`, `NAME`, and the `cmd` / `path`.
2. **Suspend** before killing if you want to inspect state. From
   another shell on the same host:
   ```bash
   # Find the agent's PID inside the pane:
   sudo -niu <user>_agent tmux -S /tmp/uxon-<user>_agent.sock \
        list-panes -t <session> -F '#{pane_pid}'
   # Suspend everything in that process tree:
   sudo kill -STOP -- -<pid>           # negative = process group
   ```
   Suspended processes consume no CPU and make no syscalls until
   you `kill -CONT` or kill them outright.
3. **Or kill immediately** if forensics aren't needed: in the
   TUI press `d` on the row, type `kill`, `Enter`. The audit
   channel records `session.kill` with `target_user`, `force`,
   `dry_run = false`, `outcome`.

For a full host-wide stop ("the agent broke into the wider
filesystem and we need everything down"), use the TUI's
`kill-all-reachable` action (typing `kill-all-reachable` to
confirm). That covers every reachable user in `session_users`
on the local host. There is no fleet-wide equivalent — see
"Across hosts" below.

## Forensics: capture before reaping

Before killing the suspended session, grab evidence:

**Pane scrollback:**

```bash
sudo -niu <user>_agent tmux -S /tmp/uxon-<user>_agent.sock \
     capture-pane -t <session> -pS -32768 \
  > /tmp/rogue-scrollback-$(date +%s).log
```

The `-pS -32768` flag dumps the last 32k lines of scrollback to
stdout. Adjust depth if your `tmux` history-limit is higher.

**Process tree:**

```bash
sudo ps -fH -p <pid> --forest
sudo lsof -p <pid> | head -50
```

**Recent filesystem changes by the offending agent account:**

```bash
sudo find / -newer /tmp/onset-marker -user <user>_agent 2>/dev/null \
  | head -200
```

(Create `/tmp/onset-marker` with `touch` at the moment you
noticed; otherwise use a `find -mtime -1` filter.)

**Audit trail for the session in question:**

```bash
journalctl SYSLOG_IDENTIFIER=uxon \
  --since "1 hour ago" \
  | grep -E "(session=$SESSION|launch_user=${USER}_agent)"
```

If you have central forwarding (see
[`forward-audit-to-collector.md`](forward-audit-to-collector.md)),
do this query against the collector — it has the cross-host
correlation if the agent had been doing remote things.

## Reap the session

Once you have what you need:

```bash
sudo kill -CONT -- -<pid>            # un-suspend so kill is graceful
sudo -niu <user>_agent uxon kill <session> --force
# or, in the TUI: d on the row, type kill, Enter.
```

`session.kill` audit event lands with `outcome = ok`. Any
already-running child processes the agent forked outside its
pane (background `npm install`, `docker compose up`, etc.) are
**not** reaped automatically — these belong to the user's
session manager, not `tmux`. Look them up with `pgrep -u
<user>_agent` and kill explicitly.

## Revoke whatever it had access to

The agent may have had ambient credentials. Assume compromised:

- **API keys** stored under `~/.claude/`,
  `~/.config/openai/`, `~/.config/anthropic/`, or in `.env`
  files inside the project tree. Revoke at the provider, not on
  the host.
- **`gh` token** under `creds_user`. Revoke at GitHub, then
  re-issue. See
  [`rotate-credentials.md`](rotate-credentials.md).
- **`token_file`** referenced from `[[git_remote_profiles]]`.
  Re-issue and rewrite the file.
- **SSH-agent socket** if the agent had `ForwardAgent yes`
  delegated. There's nothing to revoke at the agent level —
  just unforward (`SSH_AUTH_SOCK` connection drops on next
  login). The developer's underlying private key was never
  exposed if they used a hardware key.

## Recover the project tree

If the agent rewrote files you needed:

- Most projects are git repos. `git status` then `git stash`
  the unwanted changes, or `git reset --hard HEAD@{1}` if a
  commit was made. The local `.git/objects/` keeps deleted
  commits for ~14 days.
- For non-git scratch dirs, restore from your backup
  (see [`back-up-and-restore.md`](back-up-and-restore.md)).
- If files in `/srv/projects/<other_user>/` were touched by an
  account that wasn't supposed to reach there, audit your group
  / ACL setup —
  [`guides/harden/lay-out-shared-projects.md`](../harden/lay-out-shared-projects.md).

## Across hosts (team·N)

The local TUI's `kill-all-reachable` covers only the local host
deliberately. For a remote host, SSH in and run the gesture
there — there's no fan-out kill primitive (see
[`explain/multi-host-philosophy.md`](../../explain/multi-host-philosophy.md)).

For per-session remote kills the TUI's `d` on a remote row works
the same way (dispatches `uxon kill --host <peer> --user <name>
<id>` over SSH).

If the agent had been making cross-host calls, chase the
`correlation_id`:

```bash
journalctl SYSLOG_IDENTIFIER=uxon CORRELATION_ID=<uuid> --since today
# Or, against the central collector, the same query — pulls both sides.
```

Without central forwarding you have to query each host
separately; this is exactly when central forwarding pays off.

## Post-incident

Open an incident note (your team's tracker, not GitHub —
incidents involving credentials shouldn't sit in a public repo).
Include:

- the audit-event sweep (`OUTCOME != "ok"` since onset);
- the scrollback capture path;
- which credentials were rotated;
- which files were touched / restored;
- whether the agent was running with `--dsp` (yolo).

If the agent was running with `--dsp`, consider whether your
team's policy on `--dsp` is right for the current threat shape —
the per-launch confirmation is a UI gesture, not a host-wide
deny. (Host-level `--dsp` deny isn't currently a `uxon` config
knob; if your team needs it, file a feature request and audit
`flags`-contains-`--dsp` in the meantime.)

## Common mistakes

- **Killing the `tmux` session before capturing scrollback.**
  Reverse the order: stop, capture, kill.
- **Killing the agent process directly without notifying tmux.**
  `tmux` then leaves a zombie session. Use `uxon kill` or
  `tmux kill-session`, not raw `kill -9`.
- **Forgetting child processes outside the pane.** `npm`,
  `docker`, `pytest -n auto`, background watchers. `pgrep -u`
  is your friend.
- **Skipping credential revocation because "the agent didn't
  exfiltrate anything."** Assume compromise. Rotate.

## Related

- [`back-up-and-restore.md`](back-up-and-restore.md) — recovery
  side.
- [`rotate-credentials.md`](rotate-credentials.md) — the
  revoke-and-reissue procedure.
- [`forward-audit-to-collector.md`](forward-audit-to-collector.md)
  — why central audit pays off in incident response.
- [`SECURITY.md`](../../../SECURITY.md) — threat-model recap.

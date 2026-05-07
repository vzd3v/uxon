# Read-only attach for supervision

`tmux attach` is read-write by default — once a lead attaches
to a developer's pane, every keypress lands in whatever process
is in that pane. For instructors observing a session, on-call
seniors looking over a junior's shoulder, or any "watch but
don't interfere" posture, you want `tmux attach -r` instead.

`uxon`'s TUI doesn't currently expose a read-only-by-default
attach key. This page covers the manual recipe and the wrapper
patterns that make it ergonomic.

## When to use

- **Pair-programming oversight.** Lead watches a junior debug
  without accidentally typing into the pane.
- **Incident response forensics.** Operator inspects a runaway
  agent's pane without sending stray keystrokes that could
  trigger a `--dangerously-` confirmation.
- **Training / live demos.** Instructor watches several
  students' sessions in tabs, knows they cannot derail a
  session by mistake.
- **Compliance shape.** Some teams require read-only audit
  attach as a process matter.

## Manual recipe (one-off)

The TUI's `Enter` on a cross-user row dispatches the read-write
attach. Bypass that and use `tmux` directly:

```bash
SOCK=/tmp/uxon-alice_agent.sock
SESSION=uxon-myproj@claude

sudo -niu alice_agent tmux -S "$SOCK" attach -r -t "$SESSION"
```

`-r` (read-only) is documented in `man tmux`. To detach: same as
read-write attach (`Ctrl-b d`).

If your `tmux` socket path differs from the default, find it via
`uxon doctor` (`tmux socket: ...` line) or list it:

```bash
sudo -niu alice_agent ls -la /tmp/uxon-alice_agent.sock
```

## Wrapper alias

Drop into the lead's `~/.bashrc`:

```bash
# Read-only attach to another user's uxon session.
# Usage: uxon-watch <user> <session-id>
uxon-watch() {
  local user="$1" session="$2"
  if [[ -z "$user" || -z "$session" ]]; then
    echo "usage: uxon-watch <user> <session-id>" >&2
    return 2
  fi
  local sock="/tmp/uxon-${user}.sock"
  sudo -niu "$user" tmux -S "$sock" attach -r -t "$session"
}
```

Pair with the existing `uxon list --all-users` to discover
session ids:

```bash
uxon list --all-users
# pick a row...
uxon-watch alice_agent uxon-myproj@claude
```

## Wrapper script for a more reliable invocation

For `team·N` where the session is on a remote host, the same
pattern through SSH:

```bash
# /usr/local/bin/uxon-watch (host-wide, lead-only mode 0750:lead):
#!/bin/bash
set -euo pipefail
host="$1" user="$2" session="$3"
ssh -t "$host" "sudo -niu '$user' tmux -S /tmp/uxon-${user}.sock attach -r -t '$session'"
```

```bash
sudo install -m 0750 -o root -g devs uxon-watch /usr/local/bin/uxon-watch
uxon-watch dev-prod-1 alice_agent uxon-myproj@claude
```

The SSH side requires `-t` to allocate a TTY (so `tmux` works);
peer-side `tmux attach -r` is the same as the local case.

## Caveat: `-r` is keypress-only

Read-only attach blocks **input** to the pane. It does not:

- Hide what's on the screen — the operator still sees
  everything the agent renders, including any secrets the
  agent may print (API keys in error messages, environment
  dumps, `.env` files cat'd to the pane).
- Block detach (`Ctrl-b d` still works — that's how you leave).
- Block resize (the operator's terminal size may change the
  pane dimensions seen by the developer's session). For pure
  observation, run a sized terminal that matches the
  developer's, or attach from a separate window without
  changing the focus session.

For threats where the operator should not see the pane at all
(e.g. screen-sharing forensics for a live secret), `tmux
attach -r` is the wrong tool — capture and review pane
scrollback after the fact instead. See
[`respond-to-rogue-agent.md`](../operate/respond-to-rogue-agent.md)
for the scrollback capture recipe.

## Composing with audit

A read-only attach still emits `session.attach` (or
`attach.remote.in` if dispatched over SSH) — the audit channel
treats `-r` and `-rw` the same. You see *that* the operator
attached, not whether they could have typed.

If you need a paper trail of read-only-only access, set up a
shell wrapper that emits its own log line before invoking the
attach:

```bash
uxon-watch() {
  local user="$1" session="$2"
  logger -t uxon-watch "caller=$(id -un) target_user=$user session=$session mode=ro"
  sudo -niu "$user" tmux -S "/tmp/uxon-${user}.sock" attach -r -t "$session"
}
```

`logger -t uxon-watch` lands on `journalctl SYSLOG_IDENTIFIER=uxon-watch`
— distinct identifier, queryable separately.

## Common mistakes

- **Using `tmux attach` (no `-r`) and remembering not to type.**
  Eventually you'll type. Use `-r` deliberately.
- **Dropping `sudo -niu <user>`.** Without it, the lead's
  `tmux` looks at the wrong socket and finds nothing.
- **Running the read-only attach as the developer's shell user
  (`alice`) instead of the agent account (`alice_agent`).**
  Wrong socket. The session is on `alice_agent`'s socket.
- **Forgetting that `-r` is per-attach, not per-session.**
  Another operator can attach read-write to the same session.
  If you need a guaranteed-read-only posture, wrap the entry
  path so no read-write attach is exposed.

## Related

- [`SECURITY.md`](../../../SECURITY.md) — supervision threat
  model, including the explicit "read-write by default" caveat.
- [`explain/supervision-without-impersonation.md`](../../explain/supervision-without-impersonation.md)
  — the team property this composes with.
- [`../operate/respond-to-rogue-agent.md`](../operate/respond-to-rogue-agent.md)
  — when read-only forensics is the right posture.

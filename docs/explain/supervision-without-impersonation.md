# Supervision without impersonation

This is the property at the heart of `uxon`'s team setup, both on
a single host and across a fleet. It's worth one careful read
because the team and team-N scenarios depend on it.

## The shape

In the paired-account team setup, every developer's agent runs
as a separate low-privilege OS account: `alice` (shell user) +
`alice_agent` (agent runtime), `bob` + `bob_agent`, and so on.

A team-lead grant of the form

```
lead ALL=(alice_agent,bob_agent) NOPASSWD: ALL
```

lets the lead:

- attach to Alice's and Bob's running agent sessions
  (`uxon attach`, `Enter` in the TUI on a row in their `USER`
  block);
- reap a stuck or runaway session (`uxon kill`, `d` in the TUI);
- run the TUI's `kill-all-reachable` action across `alice_agent`,
  `bob_agent`, and any other agent accounts the lead can reach.

It does **not** let the lead `sudo -iu alice` or `sudo -iu bob` —
the grant targets the agent accounts only. The lead never becomes
the developer.

## Why this matters

The agent account holds the project working tree, the agent
binary's cached state under `~/.claude/`, and whatever the
developer has explicitly handed it. The shell account holds
everything tied to the developer's identity:

- SSH keys (especially behind a passphrase prompt or hardware-key
  touch);
- `gh auth` tokens, `aws-vault`-style sessions, an unlocked
  browser profile if any;
- the developer's `.bash_history`, `.gitconfig` with a personal
  email, `.zsh_history`, `.viminfo`;
- any long-lived secret an operator might pull out of `$HOME` if
  they could become the developer for a moment.

The grant `ALL=(alice_agent)` is enough to supervise a runaway
agent. `ALL=(alice)` would also let the lead read the developer's
SSH agent socket and act as them. The team setup picks the first
deliberately.

## Three honest caveats

The grant does not erase every avenue, and SECURITY.md spells
these out explicitly. Repeating them here so they're never lost
in a refactor:

1. **`tmux attach` is read-write by default.** Once an operator
   attaches to a developer's running pane, keypresses go to
   whatever process is in that pane. `uxon` does not enforce
   read-only attach. For a true read-only audit posture, attach
   with `tmux attach -r` — see
   [`guides/harden/configure-readonly-attach.md`](../guides/harden/configure-readonly-attach.md).

2. **`ForwardAgent yes` widens the boundary.** If the developer
   ran the agent with SSH-agent forwarding into the
   `<user>_agent` account, the agent's process holds a live
   handle to the developer's SSH agent socket. An operator
   attached to that pane can use that socket to sign as the
   developer (without the private key ever being copied). The
   paired-account model protects the developer's SSH key on
   disk; it does not revoke ambient delegations the agent
   already holds. Forward only for the duration of operations
   that need it.

3. **Secrets stored inside the `<user>_agent` account are
   reachable by anyone who can `sudo -iu <user>_agent`.**
   Long-lived `OPENAI_API_KEY`, `~/.aws/credentials` copied
   in for convenience, session tokens cached under
   `~/.claude/`, `.env` files in the project tree — all of
   these are readable by the lead with the team grant. Use
   ephemeral credentials (`aws-vault`-shaped helpers,
   short-lived tokens, per-session secret managers) rather
   than long-lived keys in agent home directories.

## Across hosts (team·N)

The same property holds per host, independently. Cross-host
operation does not delegate trust between peers. Each peer
evaluates its own `sudoers` against the SSH user landing on
that peer; there is no shared-secret handshake, no central
authority, no certificate chain `uxon` installs across the
fleet.

A grant on host A does **not** propagate to host B. To revoke a
lead's reach on host B you edit `/etc/sudoers.d/` on host B —
touching the central config or the lead's machine doesn't
change what host B accepts.

This is the source of the team·N model: each host stays the
authority on its own users.

## Related

- [`explain/isolation-model.md`](isolation-model.md) — the OS-user
  pattern this supervision model rests on.
- [`SECURITY.md`](../../SECURITY.md) — full threat model,
  including the three caveats expanded with operational detail.
- [`guides/harden/configure-readonly-attach.md`](../guides/harden/configure-readonly-attach.md)
  — the recipe for the read-only-attach posture.

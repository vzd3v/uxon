# Isolation model: OS users, not containers

`uxon` uses dedicated low-privilege Linux users (`<user>_agent`)
and `sudo -iu` to run agents, rather than spinning up a container
per agent session. This page explains why, what the tradeoff is,
and what kind of host this model is *not* suitable for.

## What "paired-account" means

The recommended pattern across all four scenarios is the same:
each shell user is paired with a low-privilege OS account that
owns the agent's runtime — `vz` (you) + `vz_agent`, or `alice` +
`alice_agent`. The agent runs as `<user>_agent` via `sudo -iu`;
the developer's shell user stays the trust boundary that holds
dotfiles, SSH keys, credentials.

In both directions:

- **Caller → agent.** A yolo run (`--dsp`) blasts only what
  `<user>_agent` can write to. The developer's `~/.ssh`,
  `~/.gnupg`, `~/.config/gh`, `~/.aws` are not in reach.
- **Agent → caller.** `<user>_agent` is a separate OS user with
  its own home, so the agent has no implicit access to the
  developer's files. Anything the developer wants the agent to
  see (the project tree, an SSH-agent socket, a credentials
  file) is opt-in via group ACLs, bind mounts, or the `sudo -iu`
  step itself.

`uxon` does **not** add a sandbox of its own. Isolation between
`<user>_agent` and the rest of the host is whatever ordinary
Unix UID separation provides — file permissions, process
ownership, per-user `tmux` sockets. `uxon` does not configure
cgroups, AppArmor, seccomp, or kernel namespaces.

## The tradeoff vs. containers

Containers per agent session would give:

- Independent network namespace per agent (no `/proc` peeking,
  no shared listening ports).
- UID-mapping inside an unprivileged user namespace — even root
  inside the container is harmless on the host.
- Resource limits via cgroups configured by the runtime, not the
  operator.

What they cost:

- **UID-mapping plumbing.** `sudo` from inside vs. outside the
  container, host UIDs vs. guest UIDs, files written by the
  agent that the developer can't read back without `sudo` on
  the host.
- **Network plumbing.** Outbound proxy / DNS / API endpoints
  per container; SSH-agent forwarding gets awkward.
- **Auth duplication.** GitHub `gh` login, Anthropic credentials,
  AWS profiles — each container needs its copy or its own bind
  mount.
- **Per-image maintenance.** Distro upgrades, agent-binary
  upgrades, glibc compatibility, base-image patching.
- **Operator complexity.** A team box with 20 active agent
  containers is meaningfully harder to reason about than 20
  `tmux` sessions on per-user sockets.

For a host where the threat model is "developers on this team,
plus their agents running yolo by accident", paired OS accounts
+ per-user `tmux` sockets are the cheaper bargain. For a host
where the developers themselves are untrusted, OS-user
separation isn't enough — run `uxon` inside a VM (or container)
per team and keep the OS-user model inside it.

## What you keep on the same kernel

- Same kernel, same network namespace, same `/proc` (unless
  `hidepid=2` is mounted — see
  [`guides/harden/enable-hidepid-correctly.md`](../guides/harden/enable-hidepid-correctly.md)).
- Same systemd, same loginds, same DNS resolver, same firewall.
- Per-user `tmux` socket at `/tmp/uxon-<user>.sock` — only that
  user's processes can attach.
- Per-user home, per-user `~/.claude/` config / cache, per-user
  `~/.gitconfig`. A team-shared launch user (`runtime_user =
  "team_agent"`, mode (b) in
  [`start/team-1-bootstrap.md`](../start/team-1-bootstrap.md))
  collapses these into one shared home and shares the blast
  radius across developers — useful when agents legitimately
  need shared workspace, painful when they don't.

## Threat model summary

`uxon`'s authorisation model is the operator's `sudoers` config.
`uxon` never elevates beyond what `sudoers` already grants.
Detailed threat-model writeup, including caveats around
`tmux attach -r`, `ForwardAgent yes`, and secrets persisted to
`<user>_agent`'s home directory, is in
[`SECURITY.md`](../../SECURITY.md).

## Related

- [`explain/supervision-without-impersonation.md`](supervision-without-impersonation.md)
  — the team property that falls out of the paired-account
  model.
- [`guides/harden/lay-out-shared-projects.md`](../guides/harden/lay-out-shared-projects.md)
  — file-ACL conventions on `/srv/projects` so the OS-user model
  composes with multi-developer collaboration.

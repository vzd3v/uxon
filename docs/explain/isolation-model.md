# Isolation model: OS users, composing with containers

`uxon` runs agents as dedicated low-privilege Linux users
(`<user>_agent`) via `sudo -iu`. This is `uxon`'s isolation
default — but it is **orthogonal to whether the agent process is
also containerised**, not an alternative to it. This page explains
the OS-user model, why it is the default, how it composes with a
container, and what kind of host it is *not* suitable for.

`uxon` is not in the business of choosing your isolation primitive.
It pairs an OS account to a developer and execs the agent under it;
if the operator also wants the agent inside a container, `uxon`
wraps the exec in their runtime prefix (`[container]`) and the two
layers stack. The question is never "OS users *or* containers" — it
is "OS users, and optionally a container on top".

## What "paired-account" means

The recommended pattern across all four scenarios is the same:
each shell user is paired with a low-privilege OS account that
owns the agent's runtime — `vz` (you) + `vz_agent`, or `alice` +
`alice_agent`. The agent runs as `<user>_agent` via `sudo -iu`;
the developer's shell user stays the trust boundary that holds
dotfiles, SSH keys, credentials.

In both directions:

- **Caller → agent.** A yolo run (`--mode yolo`) blasts only what
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

## Why OS-user pairing is the default

For the common threat model — "developers on this team, plus their
agents running yolo by accident" — paired OS accounts on per-user
`tmux` sockets are the cheaper bargain than reaching for containers
*as the isolation mechanism*. A container layer adds standing cost:
UID-mapping plumbing (host UIDs vs. guest UIDs, files the developer
can't read back without `sudo`), network plumbing (outbound
proxy / DNS, awkward SSH-agent forwarding), auth duplication (each
container needs its copy of `gh` login / Anthropic credentials / AWS
profiles, or a bind mount), per-image maintenance (distro and
agent-binary upgrades, base-image patching), and operator overhead.
OS-user pairing carries none of that and still bounds the agent's
blast radius. So it is the default, and a container is something you
*add* when you want it — not a prerequisite.

For a host where the developers themselves are untrusted, neither
the OS-user model nor a same-kernel container is enough on its own —
run `uxon` inside a VM (or container) per team and keep the OS-user
model inside it.

## A container on top does not relocate the credential exposures

Containerising the agent is a real isolation gain against some
classes of escape, but it does **not** move `uxon`'s
credential-exposure caveats elsewhere — in places it widens them:

- **Bind-mounted auth dirs keep (or widen) the secrets reach.**
  Making `~/.claude`, `~/.gitconfig`, `~/.aws/`, or an SSH-agent
  socket usable inside the container means bind-mounting it in, so
  the secrets are reachable inside exactly as they were from
  `<user>_agent`'s home.
- **`uxon`'s writable-probe checks the host path.** Under UID
  mapping the container-side writability of that path can diverge,
  so the probe is a convenience hint with no security meaning
  inside the container.
- **Driving a rootful daemon is host-root-equivalent.**
  docker-group membership (or rootful-socket access) lets the
  launch user reach host root and defeats the pairing; **rootless**
  docker/podman keeps the sandbox intact at zero template cost, and
  is necessary but not sufficient (a `--privileged` / host-namespace
  / broad-bind / socket-mounting container definition re-grants host
  access, which `uxon` cannot prevent).

The full writeup of these — with the rootful-vs-rootless framing and
the operator caveat on container definitions — is in
[`SECURITY.md`](../../SECURITY.md). The how-to for actually wiring it
up is
[`guides/customise/run-agents-in-a-container.md`](../guides/customise/run-agents-in-a-container.md).

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
- [`guides/customise/run-agents-in-a-container.md`](../guides/customise/run-agents-in-a-container.md)
  — running the agent inside a container, composed on top of the
  paired account.

# Team on a single host

Several developers SSH into the same Linux box and launch agents
there. `uxon` gives the operator a single TUI that sees every
agent on the host (with `sudo`) plus the option to confine each
agent to a low-privilege OS user, so a runaway tool can't write
outside its corner.

## What you get

- Each developer keeps their own shell user; agents run as a
  paired `<user>-agent` account via `sudo -H -u USER --`. A yolo-mode run
  blasts only inside `<user>-agent`, not the developer's `$HOME`.
- The lead's TUI mounts every developer's sessions in one
  dashboard with a `USER` column, gated on the lead's per-target
  `sudo -n -H -u USER --` reach. Attach (`Enter`) and kill (`d`) work
  uniformly across own and other-user rows.
- **Supervision without impersonation.** A grant
  `lead ALL=(nadia-agent,liam-agent) NOPASSWD: ALL` lets the lead
  attach to and reap Nadia's and Liam's agent sessions but does
  *not* grant `sudo -H -u nadia --` — the lead never becomes the
  developer. Anything tied to the developer's identity (SSH keys
  with passphrase prompt, `gh` / `aws` sessions, unlocked browser
  profile) stays out of reach via this path.
- Per-user/backend `tmux` socket (`/tmp/uxon-<user>-<backend>.sock`) — no
  cross-user session leakage.
- One audit event per substantive operator gesture, going to
  journald (or `/dev/log` syslog fallback). `outcome != "ok"` is
  a complete sweep of denied/errored/not-found gestures.

## Get started

1. **Install host-wide** — [`start/install.md`](../start/install.md).
   Use `sudo pipx install --global uxon` or the bundled installer.
   Per-user install on a team host weakens audit integrity.
2. **Bootstrap the host** — [`start/team-1-bootstrap.md`](../start/team-1-bootstrap.md)
   walks the recommended per-caller paired-account setup end-to-end.
3. **Onboard the first developer** —
   [`guides/operate/onboard-developer.md`](../guides/operate/onboard-developer.md)
   has the runbook (`useradd`, sudoers, agent-account, ACLs on
   `/srv/projects`).

## Operations runbooks

- [`guides/operate/onboard-developer.md`](../guides/operate/onboard-developer.md)
- [`guides/operate/offboard-developer.md`](../guides/operate/offboard-developer.md)
- [`guides/operate/respond-to-rogue-agent.md`](../guides/operate/respond-to-rogue-agent.md)
- [`guides/operate/rotate-credentials.md`](../guides/operate/rotate-credentials.md)
- [`guides/operate/back-up-and-restore.md`](../guides/operate/back-up-and-restore.md)

## Hardening recipes

- [`guides/harden/apply-resource-limits.md`](../guides/harden/apply-resource-limits.md)
  — per-UID `MemoryHigh=` / `CPUWeight=` so one runaway doesn't
  starve the rest.
- [`guides/harden/lay-out-shared-projects.md`](../guides/harden/lay-out-shared-projects.md)
  — group / ACL conventions on `/srv/projects` so devs can review
  each other's worktrees without opening the whole tree.
- [`guides/harden/configure-readonly-attach.md`](../guides/harden/configure-readonly-attach.md)
  — current limitation and safe non-interactive alternatives for
  read-only supervision.
- [`guides/harden/enable-hidepid-correctly.md`](../guides/harden/enable-hidepid-correctly.md)
  — `/proc/<pid>` visibility caveats with the dashboard.
- [`guides/customise/run-agents-in-a-container.md`](../guides/customise/run-agents-in-a-container.md)
  — run the agent in a (rootless) container on top of the paired
  account for a stronger escape boundary.
- [`guides/harden/harden-a-container.md`](../guides/harden/harden-a-container.md)
  — lock that container down: hardened template, default-deny egress,
  file-based secrets, and the agent-writable-definition footgun.

## Reference

- [`reference/cli.md`](../reference/cli.md) — every flag, including `--user`, `--all-users`, `kill --user`.
- [`reference/configuration.md`](../reference/configuration.md) — `default_launch_user`, `launch_user_by_caller`, `session_users`, `enable_all_users_list`, `allowed_roots`.
- [`reference/audit-events.md`](../reference/audit-events.md) — the event alphabet and outcome semantics.

## Worth understanding once

- [`explain/isolation-model.md`](../explain/isolation-model.md) — the OS-user model, and how a container composes on top of it.
- [`explain/supervision-without-impersonation.md`](../explain/supervision-without-impersonation.md) — the property at the heart of the team setup, and its three honest caveats.
- [`explain/audit-channel-design.md`](../explain/audit-channel-design.md) — what's recorded and why journald.
- [`explain/sizing-a-host.md`](../explain/sizing-a-host.md) — RAM/CPU/disk planning for N developers.
- [`privacy.md`](../privacy.md) — for sharing with your team: what `uxon` records about each developer.

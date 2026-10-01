# Lay out shared `/srv/projects` ACLs

In a `team·1` setup with paired accounts (`nadia-agent`,
`liam-agent`, …), every developer's agent writes under
`/srv/projects`. Without an explicit ACL convention you end up
with one of two failure modes: too open (`chmod 777` everywhere,
so any agent can rewrite any project) or too closed (`chmod 700`
per-user, so devs can't review each other's worktrees).

This page covers a layout that lets developers review each
other while keeping write access scoped.

## Recommended layout

```text
/srv/projects/             root:devs         2750 (no peer writes)
├── nadia/                 nadia-agent:devs  personal ACL
│   ├── repo-foo/                            inherited personal ACL
│   └── repo-bar/                            inherited personal ACL
├── liam/                  liam-agent:devs   personal ACL
└── shared/                root:devs         2770 + group-write default ACL
    └── team-monorepo/                       inherited shared ACL
```

Properties:

- **Per-developer subdir.** `nadia-agent` writes only under
  `/srv/projects/nadia/`. `allowed_roots = ["/srv/projects"]`
  in `config.toml` covers the whole tree; the directory ACL, not ownership
  alone, denies peer writes. The owner's shell account also has write access.
- **`devs` group ownership.** Every developer's shell user (and
  every `*-agent`) is a member. The lead is also a member.
- **Setgid bit (`2xxx`).** New files inside inherit the parent
  directory's group, so `nadia-agent`'s commits in
  `/srv/projects/nadia/foo` end up `:devs`-readable
  automatically, subject to file modes and inherited ACLs.
- **`shared/` root-owned.** A neutral subdir for projects that
  multiple developers' agents need to write to (a team
  monorepo). Use sparingly — most projects belong under one
  developer's subtree.

## Set it up

```bash
sudo groupadd -r devs

# Add every developer's shell user AND agent account to devs:
for u in nadia liam ethan; do
  sudo usermod -aG devs "$u"
  sudo usermod -aG devs "${u}-agent"
done
sudo usermod -aG devs lead       # supervisor

# Create the layout:
sudo install -d -o root -g devs -m 2750 /srv/projects
for u in nadia liam ethan; do
  sudo install -d -o "${u}-agent" -g devs -m 2750 "/srv/projects/$u"
  sudo setfacl -m "u:$u:rwx,g::r-x,m::rwx,o::---" "/srv/projects/$u"
  sudo setfacl -d -m "u::rwx,u:$u:rwx,g::r-x,m::rwx,o::---" "/srv/projects/$u"
done
sudo install -d -o root -g devs -m 2770 /srv/projects/shared
sudo setfacl -d -m u::rwx,g::rwx,m::rwx,o::--- /srv/projects/shared
```

These commands set up new directories. Review existing access and default ACLs
with `getfacl` before adapting an existing tree; extra named-user/group entries
may still grant peer writes. Defaults affect new objects, not existing files.

The personal ACL grants `devs` read/traverse access and the named shell user
write access. Because the ACL mask includes that named user's write permission,
`ls -l` can display group-write bits: use `getfacl` to see that `group::r-x`
still denies peers writes. Verify the effective permissions:

```bash
sudo -n -H -u nadia-agent -- sh -c 'printf "review\n" > /srv/projects/nadia/test.txt'
getfacl /srv/projects/nadia/test.txt
sudo -n -H -u nadia -- sh -c 'printf "owner edit\n" >> /srv/projects/nadia/test.txt'

# Cross-user check — liam can read, can't write:
sudo -n -H -u liam-agent -- cat /srv/projects/nadia/test.txt    # works
sudo -n -H -u liam-agent -- touch /srv/projects/nadia/peer.txt  # permission denied
sudo -n -H -u liam-agent -- sh -c 'echo peer >> /srv/projects/nadia/test.txt' # denied
sudo -n -H -u liam-agent -- rm  /srv/projects/nadia/test.txt    # permission denied

sudo -n -H -u nadia-agent -- rm /srv/projects/nadia/test.txt

# Shared projects are intentionally writable by every devs member:
sudo -n -H -u nadia-agent -- touch /srv/projects/shared/test.txt
sudo -n -H -u liam-agent -- sh -c 'echo shared >> /srv/projects/shared/test.txt'
sudo -n -H -u liam-agent -- rm /srv/projects/shared/test.txt
```

## When developers need to write each other's trees

Two patterns:

**Pattern 1 — pair-coding sessions.** The lead (or another
developer) attaches to nadia's running agent via `sudo -n -H -u
nadia-agent` (TUI's superuser block). The agent writes as
`nadia-agent`, regardless of who's typing. No file-level write
sharing needed.

**Pattern 2 — shared monorepo.** Live under `/srv/projects/shared/`.
Every `*-agent` writes there as `*-agent:devs`, and the setgid
bit + default ACL preserves group writability. Use when the
project genuinely has multiple agent-driven contributors.

For one-off cases ("Liam needs to fix a typo in Nadia's tree"),
have Liam's agent commit to a branch in his own subtree and
Nadia merge — same as the human review workflow.

## Creation modes and umask

A parent default ACL governs inheritance instead of the process umask. The
creating application's requested mode still limits the result: ordinary `0666`
files lose execute bits, and explicitly private `0600` files remain private.
Applications can subsequently change permissions, so verify representative
editor/git output. See [ACL object creation](https://man7.org/linux/man-pages/man5/acl.5.html).

Without a default ACL, umask determines which requested permissions are removed.
Do not widen it fleet-wide. For a service-specific umask, `UMask=` belongs in
`[Service]`, not a user slice; Uxon does not launch workloads through a service
unit or source `.bashrc`.

## Audit footprint

Filesystem ACL changes are out of `uxon`'s audit scope —
`uxon`'s channel records *agent gestures*, not filesystem
changes. Use OS-level tools (auditd, fanotify) if you need
file-level audit.

## Caveat: `<user>-agent` reads each other's `~/.claude/`

The convention above scopes `/srv/projects/` cleanly. It does
*not* scope `<user>-agent`'s home directories — a developer's
`*-agent` can `cat /home/<other>-agent/.claude/...` if home dirs
are mode `755`.

For team setups with shared sensitive credentials:

```bash
# Tighten home-dir mode on the *-agent accounts:
for u in nadia liam ethan; do
  sudo chmod 750 "/home/${u}-agent"
  sudo chgrp "${u}-agent" "/home/${u}-agent"
done
```

Other launch users must not belong to these private home groups. Developers can
inspect their own paired account with their existing sudo grant; mode `750`
alone does not grant the shell account access.

## Common mistakes

- **Forgetting setgid (`2xxx`) on parent dirs.** New files end
  up `:nadia-agent` (the agent's primary group), not `:devs`.
  Cross-user reads fail unless ACLs catch them.
- **Giving `devs` write access on personal directories or `/srv/projects`.**
  Directory write plus traversal permits unlink/replacement even when a file
  itself is read-only.
- **Running `chmod -R` to fix permissions retroactively.**
  Wrecks executable bits on scripts, breaks `.git/` internals.
  Audit existing ACLs and change only intended entries, preserving executable
  bits and explicitly private files. Do not apply the shared-tree policy to
  personal trees.

## Related

- [`scenarios/team-1.md`](../../scenarios/team-1.md) — the scenario.
- [`explain/isolation-model.md`](../../explain/isolation-model.md) — what OS-user separation provides without ACLs.
- [`apply-resource-limits.md`](apply-resource-limits.md) — composes with these limits per UID.

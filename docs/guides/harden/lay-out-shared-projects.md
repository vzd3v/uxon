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

```
/srv/projects/                       root:root          drwxrwsr-x  (2755)
├── nadia/                           nadia-agent:devs   drwxrwsr-x  (2775)
│   ├── repo-foo/                    nadia-agent:devs   drwxrwsr-x  (2775)
│   └── repo-bar/                    nadia-agent:devs   drwxrwsr-x  (2775)
├── liam/                             liam-agent:devs     drwxrwsr-x  (2775)
└── shared/                          root:devs          drwxrwsr-x  (2775)
    └── team-monorepo/               root:devs          drwxrwsr-x  (2775)
```

Properties:

- **Per-developer subdir.** `nadia-agent` writes only under
  `/srv/projects/nadia/`. `allowed_roots = ["/srv/projects"]`
  in `config.toml` covers the whole tree; ownership prevents
  cross-developer writes.
- **`devs` group ownership.** Every developer's shell user (and
  every `*-agent`) is a member. The lead is also a member.
- **Setgid bit (`2xxx`).** New files inside inherit the parent
  directory's group, so `nadia-agent`'s commits in
  `/srv/projects/nadia/foo` end up `:devs`-readable
  automatically.
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
sudo install -d -o root        -g root -m 2755 /srv/projects
sudo install -d -o nadia-agent -g devs -m 2775 /srv/projects/nadia
sudo install -d -o liam-agent   -g devs -m 2775 /srv/projects/liam
sudo install -d -o ethan-agent -g devs -m 2775 /srv/projects/ethan
sudo install -d -o root        -g devs -m 2775 /srv/projects/shared

# Default ACLs so new files keep the convention:
sudo setfacl -d -m group:devs:rwx /srv/projects/nadia
sudo setfacl -d -m group:devs:rwx /srv/projects/liam
sudo setfacl -d -m group:devs:rwx /srv/projects/ethan
sudo setfacl -d -m group:devs:rwx /srv/projects/shared
```

`setfacl -d` sets default ACLs that new files inherit. Verify:

```bash
sudo -n -H -u nadia-agent -- touch /srv/projects/nadia/test.txt
ls -la /srv/projects/nadia/test.txt
# nadia-agent:devs, mode like rw-rw-r--

# Cross-user check — liam can read, can't write:
sudo -n -H -u liam-agent -- cat /srv/projects/nadia/test.txt    # works
sudo -n -H -u liam-agent -- rm  /srv/projects/nadia/test.txt    # permission denied

sudo -n -H -u nadia-agent -- rm /srv/projects/nadia/test.txt
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

## Umask sanity

`umask 022` (the systemd default) creates files as `rw-r--r--`
— which means `devs`-group readable but not writable. With the
default ACL above, group-writable creation requires `umask 002`
or `umask 007`.

Set it for the agent accounts only (don't widen umask
fleet-wide):

```bash
# /home/nadia-agent/.bashrc (and equivalent for every *-agent):
umask 002
```

Or, more robustly, in the systemd user-slice config:

```ini
# /etc/systemd/system/user-<uid>.slice.d/umask.conf
[Slice]
UMask=0002
```

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

This still lets each developer's *shell user* (`nadia`) read
`/home/nadia-agent/` for forensics, but blocks
`<other>-agent → /home/nadia-agent/`.

## Common mistakes

- **Forgetting setgid (`2xxx`) on parent dirs.** New files end
  up `:nadia-agent` (the agent's primary group), not `:devs`.
  Cross-user reads fail unless ACLs catch them.
- **Setting `umask 002` for shell users too.** Widens default
  permissions for everything, not just `*-agent`. Scope to the
  agent accounts.
- **Running `chmod -R` to fix permissions retroactively.**
  Wrecks executable bits on scripts, breaks `.git/` internals.
  Use `find -type d -exec chmod 2775 {} \;` and matching for
  files instead.

## Related

- [`scenarios/team-1.md`](../../scenarios/team-1.md) — the scenario.
- [`explain/isolation-model.md`](../../explain/isolation-model.md) — what OS-user separation provides without ACLs.
- [`apply-resource-limits.md`](apply-resource-limits.md) — composes with these limits per UID.

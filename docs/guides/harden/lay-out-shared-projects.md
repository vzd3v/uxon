# Lay out shared `/srv/projects` ACLs

In a `team·1` setup with paired accounts (`alice_agent`,
`bob_agent`, …), every developer's agent writes under
`/srv/projects`. Without an explicit ACL convention you end up
with one of two failure modes: too open (`chmod 777` everywhere,
so any agent can rewrite any project) or too closed (`chmod 700`
per-user, so devs can't review each other's worktrees).

This page covers a layout that lets developers review each
other while keeping write access scoped.

## Recommended layout

```
/srv/projects/                       root:root          drwxrwsr-x  (2755)
├── alice/                           alice_agent:devs   drwxrwsr-x  (2775)
│   ├── repo-foo/                    alice_agent:devs   drwxrwsr-x  (2775)
│   └── repo-bar/                    alice_agent:devs   drwxrwsr-x  (2775)
├── bob/                             bob_agent:devs     drwxrwsr-x  (2775)
└── shared/                          root:devs          drwxrwsr-x  (2775)
    └── team-monorepo/               root:devs          drwxrwsr-x  (2775)
```

Properties:

- **Per-developer subdir.** `alice_agent` writes only under
  `/srv/projects/alice/`. `allowed_roots = ["/srv/projects"]`
  in `config.toml` covers the whole tree; ownership prevents
  cross-developer writes.
- **`devs` group ownership.** Every developer's shell user (and
  every `*_agent`) is a member. The lead is also a member.
- **Setgid bit (`2xxx`).** New files inside inherit the parent
  directory's group, so `alice_agent`'s commits in
  `/srv/projects/alice/foo` end up `:devs`-readable
  automatically.
- **`shared/` root-owned.** A neutral subdir for projects that
  multiple developers' agents need to write to (a team
  monorepo). Use sparingly — most projects belong under one
  developer's subtree.

## Set it up

```bash
sudo groupadd -r devs

# Add every developer's shell user AND agent account to devs:
for u in alice bob carol; do
  sudo usermod -aG devs "$u"
  sudo usermod -aG devs "${u}_agent"
done
sudo usermod -aG devs lead       # supervisor

# Create the layout:
sudo install -d -o root        -g root -m 2755 /srv/projects
sudo install -d -o alice_agent -g devs -m 2775 /srv/projects/alice
sudo install -d -o bob_agent   -g devs -m 2775 /srv/projects/bob
sudo install -d -o carol_agent -g devs -m 2775 /srv/projects/carol
sudo install -d -o root        -g devs -m 2775 /srv/projects/shared

# Default ACLs so new files keep the convention:
sudo setfacl -d -m group:devs:rwx /srv/projects/alice
sudo setfacl -d -m group:devs:rwx /srv/projects/bob
sudo setfacl -d -m group:devs:rwx /srv/projects/carol
sudo setfacl -d -m group:devs:rwx /srv/projects/shared
```

`setfacl -d` sets default ACLs that new files inherit. Verify:

```bash
sudo -niu alice_agent touch /srv/projects/alice/test.txt
ls -la /srv/projects/alice/test.txt
# alice_agent:devs, mode like rw-rw-r--

# Cross-user check — bob can read, can't write:
sudo -niu bob_agent cat /srv/projects/alice/test.txt    # works
sudo -niu bob_agent rm  /srv/projects/alice/test.txt    # permission denied

sudo -niu alice_agent rm /srv/projects/alice/test.txt
```

## When developers need to write each other's trees

Two patterns:

**Pattern 1 — pair-coding sessions.** The lead (or another
developer) attaches to alice's running agent via `sudo -niu
alice_agent` (TUI's superuser block). The agent writes as
`alice_agent`, regardless of who's typing. No file-level write
sharing needed.

**Pattern 2 — shared monorepo.** Live under `/srv/projects/shared/`.
Every `*_agent` writes there as `*_agent:devs`, and the setgid
bit + default ACL preserves group writability. Use when the
project genuinely has multiple agent-driven contributors.

For one-off cases ("Bob needs to fix a typo in Alice's tree"),
have Bob's agent commit to a branch in his own subtree and
Alice merge — same as the human review workflow.

## Umask sanity

`umask 022` (the systemd default) creates files as `rw-r--r--`
— which means `devs`-group readable but not writable. With the
default ACL above, group-writable creation requires `umask 002`
or `umask 007`.

Set it for the agent accounts only (don't widen umask
fleet-wide):

```bash
# /home/alice_agent/.bashrc (and equivalent for every *_agent):
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

## Caveat: `<user>_agent` reads each other's `~/.claude/`

The convention above scopes `/srv/projects/` cleanly. It does
*not* scope `<user>_agent`'s home directories — a developer's
`*_agent` can `cat /home/<other>_agent/.claude/...` if home dirs
are mode `755`.

For team setups with shared sensitive credentials:

```bash
# Tighten home-dir mode on the *_agent accounts:
for u in alice bob carol; do
  sudo chmod 750 "/home/${u}_agent"
  sudo chgrp "${u}_agent" "/home/${u}_agent"
done
```

This still lets each developer's *shell user* (`alice`) read
`/home/alice_agent/` for forensics, but blocks
`<other>_agent → /home/alice_agent/`.

## Common mistakes

- **Forgetting setgid (`2xxx`) on parent dirs.** New files end
  up `:alice_agent` (the agent's primary group), not `:devs`.
  Cross-user reads fail unless ACLs catch them.
- **Setting `umask 002` for shell users too.** Widens default
  permissions for everything, not just `*_agent`. Scope to the
  agent accounts.
- **Running `chmod -R` to fix permissions retroactively.**
  Wrecks executable bits on scripts, breaks `.git/` internals.
  Use `find -type d -exec chmod 2775 {} \;` and matching for
  files instead.

## Related

- [`scenarios/team-1.md`](../../scenarios/team-1.md) — the scenario.
- [`explain/isolation-model.md`](../../explain/isolation-model.md) — what OS-user separation provides without ACLs.
- [`apply-resource-limits.md`](apply-resource-limits.md) — composes with these limits per UID.

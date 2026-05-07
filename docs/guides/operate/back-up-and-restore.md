# Back up and restore

What lives on a `uxon` host, what's worth backing up, and how to
recover when a host dies. Solo·1 hosts can usually survive on
git pushes alone; team hosts and team·N fleets benefit from a
small explicit backup policy.

## State that matters

| Path | Owner | Worth backing up? | Why |
|---|---|---|---|
| `/srv/projects/<user>/...` (or `~/projects/...`) | `<user>_agent` | **Yes** | Active developer work, often uncommitted between agent runs. |
| `~/.gitconfig` (each `<user>_agent`) | `<user>_agent` | Yes | Identity for git commits made by the agent. |
| `~/.claude/` (each `<user>_agent`) | `<user>_agent` | Optional | Cached agent config + history. Restorable via re-login. |
| `config/config.toml` (host's `uxon` config) | root or admin | **Yes** | Drives the whole host. Render from JSON if you have one. |
| `/etc/sudoers.d/uxon-*` | root | **Yes** | Per-developer grants. Easy to forget when restoring. |
| `~/.local/state/uxon/` (any user) | per-user | No | Dismissed-banner state, debug logs, metrics. Recreated. |
| `~/.local/state/uxon/remote/<peer>.json` (aggregator) | aggregator user | No | Cache fallback. Refetched on next poll. |
| journald log files (`/var/log/journal/`) | systemd | Per retention policy | Audit channel sink — see [`forward-audit-to-collector.md`](forward-audit-to-collector.md) for fleet-wide audit retention. |
| `/etc/passwd`, `/etc/shadow`, `/etc/group` | root | Yes (system-level) | The `*_agent` accounts. |

## What `uxon` itself does *not* require backing up

- The `uxon` binary / venv (`/opt/uxon/venv` or `/usr/local/bin/uxon`).
  Reinstall via [`start/install.md`](../../start/install.md).
- The runtime cache (`~/.cache/uxon/`).
- The python package's site-packages.

## Recommended backup posture

For a team·1 host with `/srv/projects` as the project root:

```bash
# Daily, off-host:
sudo tar czf /backup/srv-projects-$(date +%F).tar.gz /srv/projects/
sudo tar czf /backup/uxon-config-$(date +%F).tar.gz \
  /etc/sudoers.d/uxon-* \
  /opt/uxon/checkout/config/config.toml \
  /etc/passwd /etc/shadow /etc/group
# (or your distro's group/shadow snapshot equivalent)
```

For a fleet, fold these into your existing backup tooling
(restic, borg, rsync.net, S3 lifecycle, etc.). The shape is the
same; the content is small (typically tens of GB per host
including project trees).

## Encrypted-at-rest reminder

`/srv/projects/<user>_agent/` may contain `.env` files,
`~/.claude/` cached tokens, agent-generated session state, and
any secrets the developer copied into the project tree by hand.
The backup target must be at least as protected as the source.

## Restore: full host

After a fresh OS install:

```bash
# 1. Recreate accounts.
# Best path: re-derive from your config-management role
# (Ansible/Salt/Puppet) — useradd commands are idempotent.
#
# No config management? Capture per-account snapshots BEFORE
# the host dies and restore from those:
#
#   sudo getent passwd > /backup/getent-passwd-$(date +%F).txt
#   sudo getent group  > /backup/getent-group-$(date +%F).txt
#   sudo cp /etc/sudoers.d/uxon-* /backup/sudoers/
#
# On restore, walk the passwd snapshot and useradd each *_agent
# account with matching UID. Don't restore /etc/shadow raw —
# regenerate passwords or rely on SSH-key-only auth.

# 2. Reinstall uxon.
sudo pipx install --global uxon                # or your install flavour

# 3. Restore the project trees.
sudo tar xzf /backup/srv-projects-LATEST.tar.gz -C /
# Verify: ls -la /srv/projects/  -- ownership and modes must match.

# 4. Restore uxon config + sudoers.
sudo tar xzf /backup/uxon-config-LATEST.tar.gz -C /
sudo visudo -c                                  # syntax check
# Place config.toml under your install's config path.

# 5. Verify.
uxon doctor
# Per developer:
sudo -niu alice_agent uxon list
```

For team·N, repeat per host. There is no central state to
restore — each host stands up independently.

## Restore: a single developer's tree

When `alice_agent`'s files were corrupted (e.g. yolo run
trashed a project):

```bash
sudo tar xzf /backup/srv-projects-LATEST.tar.gz \
  -C / srv/projects/alice/specific-project
sudo chown -R alice_agent:devs /srv/projects/alice/specific-project
```

If the corruption was inside a git repo, `git reflog` /
`git fsck --lost-found` may save you without going to backup.

## Restore: aggregator

The aggregator carries minimal state — see
[`survive-aggregator-loss.md`](survive-aggregator-loss.md). The
backup-relevant pieces are `~/.ssh/config` (or its `uxon`
snippet) and the aggregator's own `config/config.toml`. Track
both in your dotfiles / infra repo rather than relying on
backups.

## Verifying backups

The "backup" you've never restored from is theoretical. Add a
quarterly drill:

```bash
# Pick a random project tree from backup.
sudo tar tzf /backup/srv-projects-2026-04-01.tar.gz | head -3

# Restore to a scratch dir, run a smoke command (git status, ls -la, etc.).
sudo mkdir -p /tmp/restore-test
sudo tar xzf /backup/srv-projects-2026-04-01.tar.gz \
  -C /tmp/restore-test srv/projects/alice
sudo find /tmp/restore-test -newer /tmp -type f | head
```

If the drill surfaces missing files, expand your backup scope
before the next incident.

## Audit retention

The audit channel itself is on the host. journald rotation +
your collector retention policy together determine how far back
queries can look. For team·N fleets see
[`forward-audit-to-collector.md`](forward-audit-to-collector.md).

## Common mistakes

- **Backing up `~/.claude/`** instead of trusting re-login on
  restore. Most agent CLIs cache state that's regenerated; the
  cache size adds up across developers and rarely pays off.
- **Skipping `/etc/sudoers.d/`.** The grants are tiny but
  central to `uxon`'s authorisation model. Losing them means
  re-deriving from `start/team-1-bootstrap.md`.
- **Restoring `/etc/sudoers.d/` without `visudo -c`.** A bad
  fragment locks you out of `sudo` on the new host.
- **Trusting `git push` as your only backup.** Uncommitted work
  in `/srv/projects/<user>/...` is lost on host failure. Yolo
  agents specifically are good at producing uncommitted churn.

## Related

- [`onboard-developer.md`](onboard-developer.md) — what creates
  the state worth backing up.
- [`offboard-developer.md`](offboard-developer.md) — what to
  preserve before deleting a `<user>_agent` home.
- [`survive-aggregator-loss.md`](survive-aggregator-loss.md) —
  aggregator-specific recovery.

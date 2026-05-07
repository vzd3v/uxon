# Onboard a developer

Add a new developer to a `team·1` or `team·N` host. Roughly 10
minutes per host.

For the initial host bootstrap (project root, lead's grant)
see [`start/team-1-bootstrap.md`](../../start/team-1-bootstrap.md).

## Prerequisites

- Root or sudo on the host(s).
- The host already running `uxon` per
  [`start/team-1-bootstrap.md`](../../start/team-1-bootstrap.md).
- Decided which `<user>_agent` name to use (convention:
  `<shellname>_agent`).

## Step 1 — Create the accounts

```bash
NEW=alice
sudo useradd -m -s /bin/bash "$NEW"
sudo useradd -m -s /bin/bash "${NEW}_agent"
```

If your team uses an LDAP / IPA / SSO directory for shell
accounts, replace `useradd` with the equivalent provisioning
step. The `*_agent` account stays a local Linux user — it's not
meant to log in interactively.

## Step 2 — Sudoers grant

```bash
echo "$NEW ALL=(${NEW}_agent) NOPASSWD: ALL" | \
  sudo tee "/etc/sudoers.d/uxon-${NEW}-agent"
sudo chmod 440 "/etc/sudoers.d/uxon-${NEW}-agent"
sudo visudo -c -f "/etc/sudoers.d/uxon-${NEW}-agent"   # syntax check
```

The grant lets `alice` become **`alice_agent`**, not the other
way round.

## Step 3 — Project workspace

```bash
sudo install -d -o "${NEW}_agent" -g devs -m 2775 "/srv/projects/$NEW"
```

Setgid (`2775`) so files inside inherit the `devs` group, which
the lead and other developers can read for review. For richer
ACL schemes see
[`guides/harden/lay-out-shared-projects.md`](../harden/lay-out-shared-projects.md).

## Step 4 — Add to `session_users`

Edit `config/config.toml` on the host:

```toml
session_users = ["alice_agent", "bob_agent", "carol_agent", "dave_agent"]

[launch_user_by_caller]
alice = "alice_agent"
bob   = "bob_agent"
carol = "carol_agent"
dave  = "dave_agent"      # add the new mapping
```

The TUI's superuser block re-probes `session_users` once per
launch — leads pick up the new user by quitting (`q`) and
re-launching `uxon`. There is no daemon, no SIGHUP.

## Step 5 — Lead's sudoers grant

Widen the lead's grant to include the new agent account:

```bash
sudo $EDITOR /etc/sudoers.d/uxon-lead-supervisor
# was:  lead ALL=(alice_agent,bob_agent,carol_agent) NOPASSWD: ALL
# now:  lead ALL=(alice_agent,bob_agent,carol_agent,dave_agent) NOPASSWD: ALL

sudo visudo -c -f /etc/sudoers.d/uxon-lead-supervisor
```

`Cmnd_Alias` / `Runas_Alias` patterns are fine if the list is
getting long — see `man sudoers`. The same grant on every
team-`N` host means the lead's reach is consistent across the
fleet.

## Step 6 — Install agents for `*_agent`

```bash
sudo -iu dave_agent
# inside dave_agent's shell:
curl -fsSL https://... | bash       # claude / codex / cursor installer
exit
```

The TUI auto-detects newly-installed agents on the next launch
and offers a one-keypress enable in the agent banner.

## Step 7 — Tell the developer

Give the new developer:

- the host(s) they have access to (`ssh dave@host1`, etc.);
- a pointer to [`scenarios/team-1.md`](../../scenarios/team-1.md)
  and [`privacy.md`](../../privacy.md) so they know what `uxon`
  records about them.

Their first run is just `uxon` — the TUI walks the rest.

## Step 8 — Verify

As `dave`:

```bash
ssh dave@host
uxon doctor             # caller=dave, launch=dave_agent, sockets+sessions ok
uxon                    # TUI opens; "New session in current folder" works
```

As the lead:

```bash
uxon                    # superuser block now lists dave_agent
                        # section header: (4/4 users reachable)
```

## On a `team·N` fleet

Repeat steps 1–6 on every host the developer needs access to.
Sudoers and `session_users` are per-host — there's no central
authority that propagates changes (deliberate property; see
[`explain/multi-host-philosophy.md`](../../explain/multi-host-philosophy.md)).

If you use config management (Ansible / Salt / Puppet) for the
fleet, add the developer to the inventory and run the role on
every host.

## Audit footprint

Every step the new developer takes is recorded in the audit
channel under `caller_user=dave`. To review their activity later:

```bash
journalctl SYSLOG_IDENTIFIER=uxon CALLER_USER=dave --since today
```

For fleet-wide queries see
[`forward-audit-to-collector.md`](forward-audit-to-collector.md).

## Common mistakes

- **Forgetting `chmod 440` on the sudoers fragment.** Files in
  `/etc/sudoers.d/` with wrong permissions are silently ignored.
- **Putting the developer's shell user in `session_users`** when
  using mode (a) (paired-account). `session_users` lists the
  *launch users* (the `*_agent` accounts), not the shell users.
- **Forgetting `[launch_user_by_caller]`** — the new developer
  ends up running as `runtime_user` (the fallback) instead of
  their own paired account.

## Related

- [`offboard-developer.md`](offboard-developer.md) — when they
  leave.
- [`back-up-and-restore.md`](back-up-and-restore.md) — what to
  back up after onboarding (their project tree on the host).
- [`rotate-credentials.md`](rotate-credentials.md) — when they're
  given a `gh` token / API key.

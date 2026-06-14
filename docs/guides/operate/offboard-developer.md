# Offboard a developer

Remove a departing developer from a `team·1` or `team·N` host
without losing audit history, breaking shared projects, or
leaving live sessions running unattended. Order matters — reap
sessions first, then revoke access, then garbage-collect.

## Prerequisites

- Root or sudo on every host the developer had access to.
- The developer's shell user (`nadia`) and paired agent account
  (`nadia-agent`) names.
- A few minutes of downtime is acceptable for live sessions
  (they'll be killed in step 1).

## Step 1 — Reap live sessions

Per host:

```bash
sudo -niu nadia-agent uxon list           # see what's running
sudo -niu nadia-agent uxon kill-all --force
# or, from the lead's TUI: select nadia-agent's rows, press d.
```

`kill-all-reachable` from the TUI works too if you want one
gesture across the whole reachable set, but the per-user
`kill-all` is more surgical when you're only reaping one
account.

Audit emits `session.kill` / `session.kill_all` per gesture —
the trail is preserved.

## Step 2 — Revoke shell access

```bash
sudo passwd -l nadia                      # lock the password
sudo usermod -s /sbin/nologin nadia       # belt-and-braces

# Remove the SSH authorized_keys (don't leave a backdoor):
sudo rm /home/nadia/.ssh/authorized_keys
```

If the developer was using SSH certificates, revoke at the CA
level instead — this is a fleet-wide action that doesn't depend
on `uxon`.

## Step 3 — Revoke uxon-side reach

Remove the sudoers fragment that lets `nadia` sudo into her
agent account:

```bash
sudo rm /etc/sudoers.d/uxon-nadia-agent
sudo visudo -c                            # verify nothing else broke
```

Update `config/config.toml`:

```diff
- session_users = ["nadia-agent", "liam-agent", "ethan-agent"]
+ session_users = ["liam-agent", "ethan-agent"]

  [launch_user_by_caller]
- nadia = "nadia-agent"
  liam   = "liam-agent"
  ethan = "ethan-agent"
```

Update the lead's grant to drop `nadia-agent`:

```bash
sudo $EDITOR /etc/sudoers.d/uxon-lead-supervisor
# remove nadia-agent from the (...) list
sudo visudo -c -f /etc/sudoers.d/uxon-lead-supervisor
```

The TUI's superuser block re-probes `session_users` once per
launch — leads quit (`q`) and re-launch to pick up the new set.

## Step 4 — Decide what to do with `nadia-agent`

You have three options. Pick one deliberately.

**(a) Keep the account, freeze it.** Useful if the project tree
under `/srv/projects/nadia/` is shared with the team and you
want it preserved in place.

```bash
sudo passwd -l nadia-agent
sudo usermod -s /sbin/nologin nadia-agent
# Files stay; nobody can sudo in (the grant is gone).
```

**(b) Transfer ownership.** When project trees should move to
another developer or a team account.

```bash
sudo chown -R liam-agent:devs /srv/projects/nadia
sudo mv /srv/projects/nadia /srv/projects/nadia-handed-to-liam
# Then delete nadia-agent (option c).
```

**(c) Delete the account and the home.**

```bash
sudo userdel -r nadia-agent              # removes home dir
sudo rm -rf /srv/projects/nadia          # iff you don't need it
```

`userdel -r` removes `/home/nadia-agent` including:

- cached `~/.claude/` tokens (revoke these out of band — see
  step 5);
- `~/.gitconfig` (developer's email, often tied to GitHub);
- `~/.ssh/` for the agent account (any keys it generated for its
  own use).

## Step 5 — Revoke external credentials

Anything `nadia` or `nadia-agent` was given but `uxon` doesn't
own:

- `gh auth` tokens — log into GitHub, revoke from `Settings →
  Developer settings → Personal access tokens` for the
  `creds_user`'s account.
- API keys for Anthropic / OpenAI / etc. — rotate or revoke at
  the provider.
- `~/.aws/credentials`, `~/.config/gcloud/`, … — revoke at the
  cloud provider.
- Any `token_file` referenced from a `[[git_remote_profiles]]`
  block where `creds_user = "nadia"` — re-issue under a new
  `creds_user` and update the profile.

This step is the same on every host. See
[`rotate-credentials.md`](rotate-credentials.md) for the
playbook.

## Step 6 — Delete the shell account

If you decided in step 4 to delete `nadia-agent`, also delete
the shell account now:

```bash
sudo userdel -r nadia
```

## Step 7 — Repeat per host (team·N)

For team·N, run steps 1–6 on every host the developer had access
to. There is no central authority that propagates the offboard;
each host is independent (deliberate, see
[`explain/multi-host-philosophy.md`](../../explain/multi-host-philosophy.md)).

If you use config management, fold the per-host changes into the
role and run it across the fleet.

## Step 8 — Verify

Per host:

```bash
sudo -niu nadia-agent uxon list 2>&1 | head -1
# Expected: account locked / nologin / non-zero exit.

grep -E '^nadia |^nadia-agent ' /etc/passwd
# Expected: no matches.

ls /etc/sudoers.d/ | grep nadia
# Expected: nothing.

ls /home/ | grep nadia
# Expected: nothing (or only handed-over directories).

journalctl SYSLOG_IDENTIFIER=uxon CALLER_USER=nadia --since today
# Expected: only the kill events from step 1; no later activity.
```

## Audit footprint

The audit trail under `caller_user=nadia` is preserved (journald
holds it according to the host's retention policy). For
compliance-shaped teams, freeze the journal export or ship it to
your central collector before deleting the account.

## Common mistakes

- **Deleting `nadia-agent` while live sessions are open.** The
  `tmux` socket at `/tmp/uxon-nadia-agent.sock` lives on; the
  agent's child processes inherit a deleted UID. Reap first,
  then delete.
- **Leaving `nadia-agent` in `session_users` after the sudoers
  fragment is gone.** The TUI shows the user as unreachable
  (`(N/M users reachable)` on the section header) — cosmetic but
  confusing. Drop both in the same change.
- **Forgetting external credential revocation.** Removing the OS
  user does not invalidate tokens stored in `~/.claude/` that
  `nadia-agent` used; those tokens authenticate to providers
  outside the host. Revoke at the provider.
- **Leaving `gh auth` cached under a `creds_user = "nadia"` git
  profile.** New developers will silently push under Nadia's
  GitHub identity. Audit `[[git_remote_profiles]]` whenever
  `creds_user` is a person.

## Related

- [`onboard-developer.md`](onboard-developer.md) — the inverse.
- [`rotate-credentials.md`](rotate-credentials.md) — the same
  step 5, expanded.
- [`back-up-and-restore.md`](back-up-and-restore.md) — what to
  preserve before deleting `/home/nadia-agent`.

# Enable `hidepid=2` correctly

`hidepid=2` mounted on `/proc` blocks every cross-user
`/proc/<pid>` read — `<user>_agent` can no longer see what
processes other agents are running, what files they have open,
or how much CPU they're consuming. SECURITY.md recommends it for
team hosts where you want to harden the OS-user boundary.

`uxon` works with `hidepid=2`, but the dashboard's CPU/RAM
columns for **other-user rows** depend on `/proc/<pid>/stat`
reads. Without the right group plumbing those columns degrade
to zeros / blanks. This page covers the right plumbing.

## When to enable

- **Team·1 / team·N** with multiple agent accounts on a host
  where you don't trust agents to introspect each other.
- Skip on **solo·1 / solo·N** — there's only one agent account,
  no inter-agent leakage risk to harden.

## What `hidepid=2` does

After remount with `hidepid=2`:

```bash
sudo -niu alice_agent ps aux
# Shows only alice_agent's own processes.

sudo -niu alice_agent cat /proc/<bob_agent's pid>/stat
# Permission denied.
```

The aggregator (root + the lead's `*_agent`-supervisor
account) needs an exception. That's what the `gid=` mount
option provides: members of a designated group keep the old
visibility.

## Recipe

```bash
# Create a group whose members can see all PIDs:
sudo groupadd -r procadm

# Add the lead's shell user (so their `uxon doctor` etc. work):
sudo usermod -aG procadm lead

# Add the dashboard's caller account if it differs from the lead:
# (typically the same user — but on team·N the aggregator's
#  SSH user lands on each peer; that user needs procadm on the peer.)
```

Mount with the group:

```bash
sudo mount -o remount,rw,hidepid=2,gid=procadm /proc

# Persist across reboots — /etc/fstab:
proc  /proc  proc  defaults,hidepid=2,gid=procadm  0  0
```

Reboot or remount; verify:

```bash
sudo -niu lead cat /proc/$(pgrep -u alice_agent -n)/stat
# Should succeed (lead is in procadm).

sudo -niu carol_agent cat /proc/$(pgrep -u alice_agent -n)/stat
# Permission denied (carol_agent is NOT in procadm).
```

## How this affects `uxon`'s dashboard

The TUI's `cpu`, `ram` columns are populated by reading
`/proc/<pid>/stat` for each pane PID. Per row:

- **Own row** (current launch user is `alice_agent`, row is
  `alice_agent`): no impact, same-UID reads always work.
- **Cross-user row** (current launch user is `lead`, row is
  `alice_agent`): works **iff `lead` is in `procadm`**. Without
  that, the columns show zeros.
- **Remote row** (peer is `vz-prod1`): the peer's `uxon list
  --json` runs as the SSH user on the peer, and that user
  needs `procadm` on the peer. The aggregator side just
  forwards the JSON — it doesn't read `/proc` for remote rows.

So the practical rule:

> Whoever's account runs `uxon list --all-users` must be in
> `procadm` for the dashboard's other-user CPU/RAM columns to
> populate.

For team·N, that's the SSH user landing on each peer — the same
account that has the lead's per-target sudo grants.

## Audit channel and `hidepid=2`

`uxon`'s audit channel writes to journald via
`/run/systemd/journal/socket`, which is independent of `/proc`
visibility. Audit emit works regardless of `hidepid=2`.

The reverse query side (`journalctl SYSLOG_IDENTIFIER=uxon`)
also doesn't depend on `/proc` — operators querying their
audit history aren't affected.

## What `hidepid=2` does *not* prevent

- **Cross-user file reads** scoped by ordinary file
  permissions. If `/srv/projects/alice/secret.txt` is mode
  `644`, `bob_agent` reads it regardless of `hidepid`. That's
  filesystem-ACL territory — see
  [`lay-out-shared-projects.md`](lay-out-shared-projects.md).
- **Network namespace introspection.** Each agent still sees
  the host's network. For network isolation you need separate
  network namespaces (containers / VMs).
- **`tmux attach` cross-user.** Sudoers + per-user tmux socket
  is the gate, not `/proc` visibility.

## Verification

```bash
# As bob_agent — should see only own processes:
sudo -niu bob_agent ps aux | wc -l
# (small number, just bob_agent's tree)

sudo -niu bob_agent ps -ef --user alice_agent
# Empty — bob_agent can't see alice_agent's PIDs.

# As lead (in procadm) — should see everyone:
ps -ef --user alice_agent
# alice_agent's processes listed.

# Dashboard sanity:
uxon                 # As lead. Other-user CPU/RAM columns populated.
```

## Common mistakes

- **Forgetting `gid=procadm` in `/etc/fstab`.** First reboot
  after enabling `hidepid=2` and the dashboard goes dark for
  cross-user rows because the lead falls out of the
  `procadm`-bypass group at boot.
- **Adding the lead to `procadm` on the wrong host.** team·N
  fleets need this on every peer where the SSH user runs the
  enumeration. Forgetting one peer makes that peer's HOST
  column show zeros for cross-user CPU/RAM.
- **Setting `hidepid=invisible` (Linux 5.8+).** Stricter than
  `hidepid=2` — even `gid=` can't see other PIDs on some
  kernels. Use `hidepid=2` unless you specifically need the
  invisible mode.

## Composing with everything else

`hidepid=2` is orthogonal to:

- the OS-user model
  ([`explain/isolation-model.md`](../../explain/isolation-model.md));
- per-UID resource limits
  ([`apply-resource-limits.md`](apply-resource-limits.md));
- `/srv/projects` ACLs
  ([`lay-out-shared-projects.md`](lay-out-shared-projects.md));
- read-only attach
  ([`configure-readonly-attach.md`](configure-readonly-attach.md)).

Apply whichever match your threat model. None of them depend
on the others.

## Related

- [`SECURITY.md`](../../../SECURITY.md) — the threat-model
  recommendation.
- [`apply-resource-limits.md`](apply-resource-limits.md) —
  systemd slice approach to per-UID resource caps; sees the
  same UIDs regardless of `/proc` visibility.

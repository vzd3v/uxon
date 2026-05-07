# Apply per-UID resource limits

`uxon` does not enforce CPU / RAM / FD limits — agents and their
child processes consume what the host gives them. On a shared
team host, one runaway can OOM the others. This page covers the
two practical enforcement layers (`systemd` slices and
`pam_limits`) that compose with the OS-user model.

For the planning-numbers context see
[`explain/sizing-a-host.md`](../../explain/sizing-a-host.md).

## When to bother

- **Solo·1**: usually skip. Your single agent gets the host.
- **Team·1 with 3+ active developers**: worth doing. The 3 GB
  agent that turned into a 60 GB Java heap is a quarterly event.
- **Team·N**: per-host setup, applied via your
  config-management role. Each host is independent.

## Recipe — `systemd` per-UID slice

`systemd` v245+ supports per-user slices that automatically
contain all of a UID's processes. Limit RAM and CPU weight
without naming individual services.

Create `/etc/systemd/system/user-.slice.d/50-uxon-agents.conf`:

```ini
[Slice]
# Apply to every per-user slice (user-1234.slice etc.).
# Soft RAM cap; processes can exceed but are reclaim-pressured first.
MemoryHigh=8G
# Hard RAM cap; OOM-kill at this point before touching other users.
MemoryMax=12G
# CPU share — relative to other slices. Default is 100; lower for
# agent accounts so an interactive shell user always gets priority.
CPUWeight=50
# Cap concurrent processes per UID. Default unlimited.
TasksMax=2048
```

Reload:

```bash
sudo systemctl daemon-reload
# Existing user sessions don't pick up changes automatically;
# either log out + log in, or:
sudo systemctl restart 'user@*.service'
```

Verify:

```bash
systemctl status user-$(id -u alice_agent).slice
# look for Memory.high / Memory.max / CPUWeight values.
```

This applies to **every** user uniformly. For per-`*_agent`
overrides — say, give `alice_agent` more RAM because she works
on a memory-heavy project — drop in
`/etc/systemd/system/user-<uid>.slice.d/override.conf`:

```ini
[Slice]
MemoryHigh=16G
MemoryMax=20G
```

Use `id -u alice_agent` to find the UID.

## Recipe — `pam_limits` (file-descriptor and process count)

`systemd` slices don't cap per-process limits like `nofile`
(file descriptors) or `nproc`. For those, `/etc/security/limits.d/`:

```bash
sudo tee /etc/security/limits.d/50-uxon-agents.conf <<'EOF'
# Per-user-agent file descriptor caps.
@uxon_agents soft nofile 4096
@uxon_agents hard nofile 16384
@uxon_agents soft nproc  512
@uxon_agents hard nproc  2048
EOF
```

Then make `*_agent` accounts members of an `uxon_agents` group:

```bash
sudo groupadd -r uxon_agents
sudo usermod -aG uxon_agents alice_agent
sudo usermod -aG uxon_agents bob_agent
```

`pam_limits` is applied at PAM session start (i.e. at
`sudo -iu <user>_agent` time). Existing sessions don't pick up
limit changes; new sessions do.

## What to size

Reasonable starting values for Node / Python / Go agent work
without heavy local services:

| Limit | Suggested |
|---|---|
| `MemoryHigh=` | 6–8 GB per `<user>_agent` |
| `MemoryMax=` | 10–12 GB per `<user>_agent` |
| `CPUWeight=` | 50 (vs. default 100 for interactive users) |
| `TasksMax=` | 2048 |
| `nofile` (soft / hard) | 4096 / 16384 |
| `nproc` (soft / hard) | 512 / 2048 |

These are starting points. Watch `systemctl status user-<uid>.slice`
for `Memory.high reached count: N` and the journal for
`oom_kill_process` records — bump if you see them in normal
operation.

## What gets killed

When a slice exceeds `MemoryMax=`, `systemd-oomd` (or the
kernel) picks a process inside that slice to kill. Other slices
are untouched. The killed process generates an audit-trail
entry **at the OS level** (`auth.log` / journald), but `uxon`'s
own audit channel only sees `session.ended` after the fact —
the agent's `tmux` pane closes, the wrapper subprocess returns
non-zero, and `session.ended` lands with `outcome = error` and
the rc.

Operators chasing OOM events should join `uxon`'s
`session.ended outcome=error` events with the kernel's
`MemoryCgroup OOM` records by `_PID` / timestamp.

## Composes with `hidepid=2`

`hidepid=2` mounted on `/proc` blocks cross-user `/proc/<pid>`
reads, which the dashboard's CPU/RAM columns rely on. The
slice-resource view in `systemctl` doesn't depend on `/proc`
visibility — both measurements compose, but the dashboard's
columns degrade. See
[`enable-hidepid-correctly.md`](enable-hidepid-correctly.md) for
the interaction.

## Verification

Synthetic load test once limits are in place:

```bash
sudo -niu alice_agent bash -c 'stress-ng --vm 4 --vm-bytes 4G --timeout 60s'
# In another shell:
systemctl status user-$(id -u alice_agent).slice
# Expect: memory.high reached, processes throttled rather than OOM'd
# unless they exceed MemoryMax.
```

## Common mistakes

- **Setting `MemoryMax=` too low.** Many AI agent invocations
  legitimately spike to 8–12 GB during big tool-use chains.
  Setting `MemoryMax=4G` means routine work OOMs.
- **Forgetting `daemon-reload`.** Slice config changes only
  apply after reload + new session.
- **Mixing global `*.slice.d/` overrides with per-UID
  `user-<uid>.slice.d/` overrides.** systemd's merge order is
  documented but easy to misread; prefer one or the other for a
  given limit.

## Related

- [`explain/sizing-a-host.md`](../../explain/sizing-a-host.md) — planning numbers.
- [`enable-hidepid-correctly.md`](enable-hidepid-correctly.md) — `/proc` interaction.
- [`scenarios/team-1.md`](../../scenarios/team-1.md) — the scenario this is for.

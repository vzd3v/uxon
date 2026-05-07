# Diagnose multi-host issues

When a peer shows up errored, stale, or `(own only)` in the
TUI's `HOST` column, work through the layers from cheapest to
most invasive.

## Layer 1 — `uxon doctor --remote`

```bash
uxon doctor --remote
```

Per-peer status with reachability, latency, and session count.
If everything is `ok`, the issue is local (cache stale, TUI
needs `r` to refresh). If a peer reports an error, the next
layers narrow it down.

## Layer 2 — bare SSH

```bash
ssh <ssh_alias> uxon list --all-users --json | head
```

Replicates exactly what `uxon`'s collector does. Check:

- Does SSH connect at all? (Network / firewall / known_hosts.)
- Does it land on the right peer? (`hostname` after the
  command works.)
- Does the peer have `uxon` on PATH? Or do you need
  `remote_uxon = "/usr/local/bin/uxon"`?

```bash
ssh <ssh_alias> 'which uxon && uxon --version'
```

## Layer 3 — peer's own `uxon doctor`

```bash
ssh <ssh_alias> uxon doctor
```

Read the audit-channel line, agent paths, sessions, and any
detected config issues on the peer side. If the peer's
`config.toml` has a parse error, peer-side `uxon list --json`
fails first thing and the aggregator only sees an opaque error
on stderr.

## Layer 4 — error tags

Stable error tags on stderr disambiguate the failure mode:

| Tag | Meaning | Fix |
|---|---|---|
| `uxon-error: not-reachable` | Caller cannot `sudo -niu <user>` | Add per-target NOPASSWD on the peer. |
| `uxon-error: all-users-disabled` | Peer's `enable_all_users_list = false` | Set `true` on peer (and confirm sudoers for `session_users`). |

Other stderr → generic SSH/peer failure → falls to cache
fallback (`(stale)` in TUI, `from_cache=True` in JSON).

## Layer 5 — wire-schema mismatch

```bash
uxon --version          # aggregator
ssh <ssh_alias> uxon --version   # peer
```

Major-version mismatch (e.g. aggregator on 3.x, peer on 2.x or
4.x) breaks the wire. The aggregator emits a parse error,
peer-side `uxon` keeps working locally. Roll the upgrade:
[`../operate/roll-fleet-upgrade.md`](../operate/roll-fleet-upgrade.md).

## Layer 6 — circuit breaker open

After three consecutive failures the breaker opens for one
interval. The peer shows as errored even after the underlying
issue is resolved, until the next half-open attempt. Wait
~30–60 s and re-check, or restart the TUI to reset the
breaker.

## Layer 7 — SSH multiplex socket stale

A stale `ControlPath` socket (`~/.ssh/cm-*` from a previous
session) can refuse new connections after a network drop. Fix:

```bash
ssh -O exit <ssh_alias>          # close stale master
# Then refresh the TUI (r).
```

If multiplexing is more trouble than it's worth in your
environment:

```toml
ssh_multiplex = "off"
```

You'll pay 200–500 ms per tick instead of 5–20 ms.

## Layer 8 — peer-side audit channel

If audit events appear locally but not on the peer (or vice
versa), check:

```bash
ssh <ssh_alias> uxon doctor | grep audit
# audit:    enabled, sink=journald-native
```

If `sink=no-sink` on the peer, the peer has no journald and no
`/dev/log` — exotic environments. Audit events are silently
dropped on that peer. Fix: add a syslog daemon or run on a
systemd-equipped host.

## Layer 9 — cache file inspection

```bash
ls -la ~/.local/state/uxon/remote/
cat ~/.local/state/uxon/remote/<peer>.json | jq .
```

The cache file's `mtime` is the snapshot age. Fresh successful
fetches overwrite atomically; failed polls don't. Manually
deleting the file is fine — next successful fetch recreates.

## Layer 10 — metrics

```bash
UXON_METRICS=1 uxon list --all-hosts --json > /dev/null
cat ~/.local/state/uxon/metrics.jsonl | tail -10
```

One JSON line per fetch attempt with timing breakdown. Useful
when chasing intermittent slowness.

## Common patterns

- **Peer in HOST column shows `(own only)`** → see
  [`add-second-host.md`](../../start/add-second-host.md) step
  4. Peer's `enable_all_users_list = false`.
- **Peer reports `[err: all-users-disabled]`** → same fix.
- **Peer reports `[err: not-reachable]`** → SSH user on peer
  lacks NOPASSWD. Add to peer's sudoers.
- **Every peer suddenly stale** → aggregator's network is down
  or its SSH key was rotated. Bare `ssh` to verify.
- **One peer always slow** → bump that peer's `interval` /
  `connect_timeout` / `total_timeout` per
  [`../customise/override-ssh-per-peer.md`](../customise/override-ssh-per-peer.md).

## Related

- [`use-uxon-doctor.md`](use-uxon-doctor.md) — the diagnostic
  command itself.
- [`../../explain/multi-host-philosophy.md`](../../explain/multi-host-philosophy.md)
  — read-mostly, fail-soft, no central authority.
- [`../operate/roll-fleet-upgrade.md`](../operate/roll-fleet-upgrade.md)
  — when version mismatch is the underlying issue.

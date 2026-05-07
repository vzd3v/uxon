# Roll a fleet upgrade

`uxon`'s peer protocol requires that all hosts in a connected
fleet run the **same major version**. Rolling out an upgrade
without coordinating means the aggregator's `list --all-hosts`
starts failing on whichever side is behind. This page is the
procedure that avoids that.

## When this applies

- Any version bump that mentions a wire-schema or
  `--audit-correlation-id`-shaped change in CHANGELOG.
- Any major-version bump (`3.x` → `4.0`).

Patch / minor bumps within a major usually don't need this
careful drain; the schema commitment is not to remove or rename
fields within a major. But running the procedure regardless costs
nothing and catches the corner cases.

## What "same version" means in practice

- The aggregator (your laptop) runs `uxon X.Y.Z`.
- Every peer in the aggregator's `[[remote_hosts]]` runs
  `uxon X.Y.Z`.
- The aggregator's `--audit-correlation-id` flag is recognised
  by every peer (peers without the flag reject the SSH
  invocation outright; silent fallback would lose the
  correlation property).

## Plan: canary one host, then rest, then aggregator

Drain order matters because the aggregator connects to peers,
not the other way round. Upgrade peers first (they keep working
locally even when out of sync with the aggregator), then the
aggregator last.

### Step 1 — Pick the canary

A non-critical peer with little/no live workload. If you don't
have one, schedule a maintenance window where the busiest
peer's developers can tolerate a 5-minute drain.

### Step 2 — Drain the canary's live sessions (optional)

Heavy `--dsp` runs can lose work on host restart if the agent
binary upgrade pulls a different glibc-incompatible build. For
production-critical sessions:

```bash
ssh canary
uxon list                       # snapshot of what's running
# Coordinate with developers to detach + reattach later, or:
uxon kill <important-session>   # if reaping is fine
```

For dev hosts where sessions are routinely restarted, skip this.
Audit captures the kill events regardless.

### Step 3 — Upgrade the canary

Whatever your install path is:

```bash
ssh canary
sudo pipx upgrade --global uxon                     # for pipx --global installs
# or:
sudo python3 install/install_uxon.py \
  --repo-dir /opt/uxon/checkout \
  --install-path /usr/local/bin/uxon \
  --reinstall                                      # for the bundled installer
# or:
sudo uv tool upgrade uxon                          # for uv-based installs

uxon --version    # confirm
uxon doctor       # confirm no new config errors at this version
```

### Step 4 — Verify aggregator still works (mismatched version)

```bash
# On the aggregator (still on the old version):
uxon doctor --remote
# Expect: canary shows as reachable BUT may emit a wire-schema
# version mismatch error if the major version changed.
```

If `--remote` reports success, the patch/minor bump didn't break
the wire. Proceed. If it reports a schema mismatch, you've
confirmed the canary needs the aggregator upgrade order to
reverse — see "Major-version-flip variant" below.

### Step 5 — Roll the rest of the peers

Repeat steps 2–3 on every other peer. Concurrency is fine for
patch/minor (peers don't talk to each other). For major-version
bumps, do them one at a time so a partial rollout is recoverable.

After each peer:

```bash
# On the aggregator:
uxon doctor --remote        # spot-check the just-upgraded peer
uxon list --all-hosts       # confirm sessions still listed
```

### Step 6 — Upgrade the aggregator last

```bash
# On the aggregator (your laptop or operator workstation):
sudo pipx upgrade --global uxon       # or whatever flavour applies
uxon --version
uxon doctor --remote                  # full fleet probe — expect all green
```

## Major-version-flip variant (4.0 etc.)

When the wire schema bumps:

- Peers running the old major **cannot** be polled by an
  aggregator running the new major.
- The aggregator's first `list --all-hosts` after upgrade will
  show those peers as errored.
- Recovery is just upgrading those peers — the cache fallback
  serves stale data in the meantime, with `(stale)` markers.

Procedure:

1. Upgrade peers first, one at a time, with the aggregator
   *still on the old version*.
2. While at least one peer is on the new major and aggregator
   is on the old, the aggregator will see the new-major peer as
   schema-mismatch errored. The peer keeps working locally.
3. Aggregate over both old and new only by upgrading the
   aggregator last. Plan the full upgrade window so this
   awkward middle phase is short.

For a fleet with no maintenance window, run two parallel
aggregators (one old, one new) and migrate peers between them
— but this is only worth doing for fleets ≥ 10 hosts.

## Rollback

If the new major regresses something for your fleet:

```bash
# Pin to the previous version on every peer:
sudo pipx install --global --force uxon==<prev-version>
```

Rollback is symmetric to forward-roll: peers first, then
aggregator. Operations between the rollback start and end will
miss correlation pairs across the version boundary, which is the
unavoidable cost of any rolling change.

## Verification per step

```bash
uxon --version                              # binary version
uxon doctor                                 # local config + sockets
uxon doctor --remote                        # peer fleet probe (aggregator only)
uxon list --all-hosts --json | head         # wire-schema sanity
journalctl SYSLOG_IDENTIFIER=uxon -n 3      # audit channel still emitting
```

## Common mistakes

- **Upgrading the aggregator first.** All peers immediately
  appear broken until they're all upgraded — your dashboard
  goes dark during the rollout instead of partially staying up.
- **Skipping `uxon doctor` between peers.** Picks up
  config-loader regressions one host at a time, not all at the
  end.
- **Mixing `pipx --global` and `uv tool` installs across the
  fleet.** Both work, but the upgrade command differs — script
  your rollout to match per-host install flavour.
- **Upgrading during a release-class moment** (incident
  response, on-call window). Schedule rollouts to quiet hours.

## Related

- [`survive-aggregator-loss.md`](survive-aggregator-loss.md) —
  what happens when the aggregator dies.
- [`forward-audit-to-collector.md`](forward-audit-to-collector.md)
  — `correlation_id` joining requires version match across the
  fleet *and* a working collector.
- [CHANGELOG.md](../../../CHANGELOG.md) — read before every
  bump; mentions which releases need the careful drain.

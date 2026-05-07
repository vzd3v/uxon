# Survive aggregator loss

The "aggregator" is the host running the TUI that polls every
peer over SSH — typically your laptop or the operator's
workstation. If it dies (laptop stolen, disk fails, kernel
panic), peers keep working but you lose situational awareness
until you stand up a new aggregator. This page covers what's
lost, what's recoverable, and how to set up a backup aggregator
preemptively.

## What is *not* lost when the aggregator dies

By design, peers are independent. The aggregator only **reads**
peer state via `uxon list --json`. So:

- **Live agent sessions** on every peer continue running. `tmux`
  on each peer doesn't care that the aggregator went dark.
- **Audit events** on each peer continue emitting to that peer's
  journald. Per-host audit history is intact.
- **Per-peer dashboards** still work — SSH into the peer
  directly and run `uxon` there for that host's view.
- **Per-session destructive operations** still work — same SSH +
  `uxon kill` from any aggregator (current or replacement).

## What *is* lost

The aggregator carries:

- `~/.ssh/config` aliases for the fleet (re-derive from infra
  repo / config-management).
- `~/.ssh/cm-*` control sockets (cosmetic — recreated on next
  ssh).
- `~/.local/state/uxon/remote/<peer>.json` snapshots — the cache
  fallback for the TUI when a peer is briefly unreachable. This
  is **safe to lose**: a fresh aggregator just refetches.
- `~/.local/state/uxon/dismissed.json` — TUI banner state.
  Cosmetic.
- The **caller side** of `correlation_id` pairs for in-flight
  remote gestures — orphaned. The peer-side `*.remote.in` event
  carries the same id but has no matching `*.remote.out` to join
  against. Acceptable for incident triage; impossible to
  reconstruct after the fact unless you had central log
  forwarding catching both sides on the way out.

## Standing up a replacement aggregator

The aggregator is just any machine with `uxon` installed and an
`ssh_config` that resolves your peer aliases. There's no
registration, no shared secret, no peer-side change required.

```bash
# On the new machine:
uv tool install uxon                          # match the fleet's version
# Restore (or recreate) the aggregator config:
cp /backup/uxon-aggregator/.ssh/config ~/.ssh/
cp /backup/uxon-aggregator/.ssh/id_ed25519* ~/.ssh/
chmod 600 ~/.ssh/id_ed25519*
mkdir -p ~/uxon-aggregator
cp /backup/uxon-aggregator/config.toml ~/uxon-aggregator/config/

# Verify:
cd ~/uxon-aggregator
uxon doctor --remote        # full fleet probe
uxon                        # TUI opens; HOST column lists every peer
```

If the SSH key on the dead aggregator is irrecoverable (for
example, a hardware key was lost), you have to issue a new key
and add it to every peer's `authorized_keys` — that's an SSH
provisioning task, not a `uxon` task.

## Decommission a peer

Inverse of [`add-second-host.md`](../../start/add-second-host.md):
when a peer is going away (host retired, contract ended,
project moved). Order matters — drain sessions first, then
remove the peer from the aggregator's view.

```bash
# 1. On the peer (or via the aggregator's kill --host while it's
#    still reachable): reap any lingering sessions you don't
#    want abandoned.
ssh <peer> uxon kill-all --force      # if you own the peer
# (or coordinate with the developers using it)

# 2. Remove the [[remote_hosts]] block from the aggregator's
#    config.toml. The TUI picks it up on next refresh (r) or
#    relaunch.

# 3. Delete the cached snapshot — it's safe to leave but tidy
#    not to:
rm -f ~/.local/state/uxon/remote/<name>.json

# 4. Remove the SSH alias from ~/.ssh/config (or leave it if
#    you keep it for ad-hoc SSH).

# 5. If the peer host stays running for other purposes,
#    optionally uninstall uxon there:
ssh <peer> sudo pipx uninstall --global uxon
```

The peer's audit history is preserved on the peer's journald
(or your central collector if you're forwarding). Nothing on
the aggregator deletes it.

## Recovering from corrupt aggregator-side cache

A `kill -9` to the TUI mid-write or a full disk on the
aggregator can leave half-written cache files. The fail-soft
loader prefers to ignore a malformed file rather than crash,
but if the dashboard shows confusing data after a crash:

```bash
rm -rf ~/.local/state/uxon/remote/
```

Safe nuclear option. Next refresh repopulates from live
fetches, and the dashboard shows `(loading)` briefly per peer
until that lands. No peer-side state is touched.

## Multiple operators, multiple aggregators

The peer protocol has no "active aggregator" concept — peers
just answer `uxon list --json` over SSH for any caller they're
configured for. Two simultaneous aggregators are fine:

- Two operators each running their own aggregator dashboard
  hits each peer twice as often, but the SSH multiplex
  (`ssh_multiplex = "auto"`) and per-host
  `interval` make this cheap.
- Each operator sees their own correlation_id namespace —
  there is no global "operation log" the aggregators consult.

This is also the answer to "what if the lead is on call and the
backup lead also needs visibility?" — both run aggregators.

## Pre-emptive: prevent loss

The cheapest mitigation is to keep the aggregator's config
recoverable:

- Track `~/.ssh/config` (or the `~/.ssh/config.d/uxon` snippet)
  in your infra repo / dotfiles repo.
- Keep the aggregator's `config/config.toml` in version control
  if it's nontrivial. The `[[remote_hosts]]` blocks can be
  rendered from JSON via
  `install/render_uxon_config.py`.
- Use a hardware-backed SSH key rather than one on disk
  (Secretive on macOS, YubiKey FIDO2 on Linux/cross-platform)
  — when the laptop dies the key dies with it; when only the
  laptop dies and you replace the disk, the key survives the
  rebuild. See [`docs/clients.md`](../../clients.md).

## Verifying the new aggregator catches up

```bash
uxon doctor --remote
```

Each peer should show `ok` with a recent latency. If a peer is
slow:

```bash
ssh -v <alias> uxon list --all-users --json | head -3
# (timing the connection)
```

If a peer's session set looks "lighter" than expected, check
`enable_all_users_list` on the peer — the new aggregator's SSH
user might lack passwordless sudo to some `*_agent`. The
`(own only)` badge in the TUI's HOST column is the signal.

## Common mistakes

- **Treating the aggregator as production infrastructure.** It's
  not. Peers are. Restoring an aggregator is a 10-minute laptop
  setup, not a runbook.
- **Forgetting to pin the same `uxon` version across the
  fleet** when standing up a new aggregator — see
  [`roll-fleet-upgrade.md`](roll-fleet-upgrade.md).
- **Building a "central authority" service** because the
  aggregator died. The whole point of the multi-host model is
  there is no central authority. Add a backup aggregator
  instead.

## Related

- [`explain/multi-host-philosophy.md`](../../explain/multi-host-philosophy.md)
  — the "no central authority" property this page rests on.
- [`forward-audit-to-collector.md`](forward-audit-to-collector.md)
  — central audit forwarding decouples observability from
  aggregator life-cycle.
- [`back-up-and-restore.md`](back-up-and-restore.md) — for the
  peer side.

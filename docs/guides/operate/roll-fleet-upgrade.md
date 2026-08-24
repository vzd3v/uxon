# Roll a fleet upgrade

Uxon peers interoperate within one wire major. Patch and minor upgrades can roll
host by host; a major upgrade requires a coordinated cutover because mixed wire
majors reject each other.

## Before either rollout

1. Read the target release in [`CHANGELOG.md`](../../../CHANGELOG.md).
2. Back up `/etc/uxon/config.toml` and any shared `launch_record_dir`.
3. Run `uxon doctor --json` and `uxon list --all-hosts --json` from the
   aggregator.
4. Drain sessions when the release changes execution backends, workload
   runtimes, tmux socket semantics, or launch-record semantics.

## Patch or minor release within one major

Upgrade a canary peer, verify it from the still-old aggregator, then roll the
remaining peers and the aggregator. Different patch/minor versions within the
same major are supported by the additive wire contract.

```bash
# On each peer:
sudo pipx upgrade --global uxon
uxon --version
uxon doctor --json

# From the aggregator after each peer:
uxon doctor --remote --json
uxon list --all-hosts --json >/dev/null

# Upgrade the aggregator last:
sudo pipx upgrade --global uxon
uxon doctor --remote --json
```

If a release note requires a drain despite an unchanged major, complete the
drain before upgrading that host.

## Major release

Schedule a maintenance window. Old and new aggregators cannot poll peers across
the wire-major boundary; cache entries may provide stale display data but are
not a compatibility mechanism.

1. Stop fleet mutations and record the active sessions.
2. Drain sessions required by the migration guide.
3. Upgrade every peer to the new major. Each upgraded peer remains usable
   locally, but the old aggregator reports it as a schema mismatch.
4. Upgrade the aggregator immediately after the last peer.
5. Apply the new config and run the verification below.

For Uxon 4.0, follow [`docs/migrations.md`](../../migrations.md) before
re-enabling launches.

## Verification

```bash
uxon --version
uxon doctor --json
uxon doctor --remote --json
uxon list --all-hosts --json >/dev/null
journalctl SYSLOG_IDENTIFIER=uxon -n 3
```

Start one disposable session for each execution-backend/workload-runtime
combination, then verify list, attach, kill, launch-record cleanup, and audit
events.

## Rollback

Rollback follows the same boundary: within a major, roll host by host; across a
major, use another coordinated maintenance window.

```bash
sudo pipx install --global --force uxon==<previous-version>
```

Do not mix installation mechanisms across hosts in one automation path; `pipx`
and `uv tool` use different upgrade commands.

## Related

- [`survive-aggregator-loss.md`](survive-aggregator-loss.md)
- [`forward-audit-to-collector.md`](forward-audit-to-collector.md)
- [`docs/migrations.md`](../../migrations.md)

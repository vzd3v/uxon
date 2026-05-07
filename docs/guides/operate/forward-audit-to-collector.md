# Forward audit events to a central collector

`uxon`'s audit channel pays off operationally only when both
sides of a cross-host gesture are queryable from one console.
This page covers the "from one console" half — shipping per-host
audit events to a central log collector.

For the per-event field reference see
[`reference/audit-events.md`](../../reference/audit-events.md);
for the design rationale see
[`explain/audit-channel-design.md`](../../explain/audit-channel-design.md).

## When to bother

- **Solo·1 / solo·N**: usually not worth it. Per-host
  `journalctl` works fine for one operator.
- **Team·1**: optional. The audit channel is local; you can
  query it on the host directly. Forwarding helps if the host
  is one of many that you correlate against (e.g. dev box +
  CI host).
- **Team·N (3+ hosts)**: this is where forwarding is essential.
  Without it, chasing a `correlation_id` across 5+ peers at 3am
  means SSH'ing to each, grepping per-host journals, and joining
  by hand. With it, one `journalctl … CORRELATION_ID=<uuid>`
  query returns everything.

## On-host basics first

Before forwarding, make sure each host's audit channel is
healthy:

```bash
uxon doctor | grep audit
# audit:    enabled, sink=journald-native    (or sink=syslog / sink=no-sink)

journalctl SYSLOG_IDENTIFIER=uxon -n 5
```

If `sink=no-sink`, the host has no journald and no `/dev/log`
either — exotic; investigate before bothering with forwarding.

## Pattern A — `systemd-journal-upload` (journald → journald)

The simplest pattern for fleets that already run systemd. Each
peer pushes journal entries to a central host via HTTPS.

On the central collector:

```bash
sudo apt install systemd-journal-remote                       # Debian/Ubuntu
sudo systemctl enable --now systemd-journal-remote.service
# Listens on :19532 by default; configure TLS in
# /etc/systemd/journal-remote.conf.
```

On each peer:

```bash
sudo apt install systemd-journal-remote                       # provides journal-upload
sudo $EDITOR /etc/systemd/journal-upload.conf
# URL=https://collector.example.org:19532
# TrustedCertificateFile=/etc/ssl/uxon-collector-ca.pem
# ServerKeyFile=...
# ServerCertificateFile=...
sudo systemctl enable --now systemd-journal-upload.service
```

Query against the collector:

```bash
journalctl --directory=/var/log/journal/remote/ \
  SYSLOG_IDENTIFIER=uxon \
  CORRELATION_ID=<uuid>
```

Pros: no schema translation, full structured field support.
Cons: requires systemd on every host, TLS certificate
management.

## Pattern B — rsyslog forwarding (`/dev/log` → central rsyslog)

For hosts where the audit channel falls through to `/dev/log`
syslog, or for fleets that already run rsyslog centrally.

On each peer (`/etc/rsyslog.d/50-uxon.conf`):

```
# Forward only uxon events to the central collector:
:syslogtag, isequal, "uxon:" @@(o)collector.example.org:514;RSYSLOG_SyslogProtocol23Format
& stop
```

On the collector:

```
# Receive on :514:
module(load="imtcp")
input(type="imtcp" port="514")

# Filter and stash uxon events:
:syslogtag, isequal, "uxon:" /var/log/uxon/audit.log
& stop
```

Query: `grep '@cee:' /var/log/uxon/audit.log | jq …`. The
`@cee:` payload is one JSON object per line — every envelope
field reachable as a JSON key.

Pros: works without systemd; integrates with anything that
already speaks syslog.
Cons: lossier metadata; no native field selectors (you `jq` the
JSON body).

## Pattern C — Vector / Fluent Bit / Loki

For teams already running an observability stack:

- **Vector** has a `journald` source and outputs to nearly
  anything (Loki, Elasticsearch, S3, Kafka, …). One agent per
  peer; collector-side is whatever your stack already runs.
- **Fluent Bit** with the `systemd` input has the same shape;
  small footprint.
- **Promtail** (Loki's agent) is fine if Loki is your
  destination.

Filter at the source: only `_SYSTEMD_UNIT=uxon.service`-shaped
records (or simply `SYSLOG_IDENTIFIER=uxon`) — you do not need
to ship every journal entry to get `uxon`'s audit trail.

```toml
# Vector example
[sources.uxon_audit]
type = "journald"
include_matches.SYSLOG_IDENTIFIER = ["uxon"]

[sinks.loki]
type = "loki"
inputs = ["uxon_audit"]
endpoint = "https://loki.example.org"
labels.job = "uxon"
```

Pros: composes with the rest of your observability. Cons: one
more moving piece per host.

## What "central queries" look like

Once events are forwarded, the typical queries become:

```bash
# Everything one operator did today across the fleet:
journalctl --directory=/var/log/journal/remote/ \
  SYSLOG_IDENTIFIER=uxon CALLER_USER=alice --since today

# Cross-host correlation pair:
journalctl --directory=/var/log/journal/remote/ \
  SYSLOG_IDENTIFIER=uxon CORRELATION_ID=8f3c2d4e-...

# All denied/errored gestures fleet-wide:
journalctl --directory=/var/log/journal/remote/ \
  SYSLOG_IDENTIFIER=uxon -o json | \
  jq -c 'select(.OUTCOME != "ok") | {host:.HOST, ts:.TS, event:.EVENT, outcome:.OUTCOME, caller:.CALLER_USER, target:.TARGET_USER, session:.SESSION}'

# kill-all gestures and what they hit, last week:
journalctl --directory=/var/log/journal/remote/ \
  SYSLOG_IDENTIFIER=uxon EVENT=session.kill_all --since "7 days ago" -o json | \
  jq -c '{host:.HOST, ts:.TS, caller:.CALLER_USER, users:.TARGET_USERS, killed:.KILLED_COUNT, dry_run:.DRY_RUN}'
```

For Vector / Loki / Elastic stacks, the same queries are
expressible in LogQL / KQL / Lucene with `SYSLOG_IDENTIFIER=uxon`
as the entry filter.

LogQL examples (Loki / Grafana):

```logql
# Everything uxon emitted in the last 24h:
{job="uxon"}

# One operator's gestures across the fleet:
{job="uxon"} | json | CALLER_USER="alice"

# Cross-host correlation pair:
{job="uxon"} | json | CORRELATION_ID="8f3c2d4e-..."

# Anything that didn't go through:
{job="uxon"} | json | OUTCOME!="ok"
```

The `json` parser stage exposes every envelope field as a label
(uppercased — same convention as journald native).

## Retention

The audit channel inherits the host's journald rotation policy.
Defaults:

- `/var/log/journal/` capped at 10 % of the filesystem (via
  `SystemMaxUse=` in `journald.conf`).
- No per-identifier retention — set fleet-wide journald rotation
  to whatever your team's policy requires.

For compliance-shaped retention (90 days, 1 year, 7 years),
ship to S3 / object storage from your collector. journald itself
is not a compliance-grade archive.

## Privacy

Forwarding events to a central collector means `caller_user`
values for every developer's gestures end up in one place. Some
teams need to disclose this — see
[`privacy.md`](../../privacy.md) for a one-page disclosure
operators can share.

## Common mistakes

- **Querying the user journal (`journalctl --user`) instead of
  the system journal.** `audit.py` connects to
  `/run/systemd/journal/socket` regardless of caller; it never
  writes to the per-user systemd-journal namespace. `--user`
  silently returns zero rows.
- **Forgetting to filter at the source.** Shipping every journal
  entry from every host to a central collector "for safety"
  multiplies your storage cost by 100x without paying off.
  Filter on `SYSLOG_IDENTIFIER=uxon` (and your own
  service-specific identifiers).
- **Trusting a one-host `journalctl` for fleet incident
  response.** That's exactly the failure mode this page exists
  to prevent. Set up forwarding before the next incident, not
  during it.

## Related

- [`reference/audit-events.md`](../../reference/audit-events.md)
  — per-event reference.
- [`explain/audit-channel-design.md`](../../explain/audit-channel-design.md)
  — why journald and `correlation_id`.
- [`respond-to-rogue-agent.md`](respond-to-rogue-agent.md) — the
  scenario that makes central forwarding worth the setup cost.

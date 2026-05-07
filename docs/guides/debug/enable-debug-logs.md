# Enable debug logs

`uxon` ships two developer-facing instrumentation channels —
`debug` (topic-gated JSONL) and `metrics` (per-fetch latency
JSONL). Both are off by default and have zero overhead when not
enabled.

These are **separate from the audit channel**, which is on by
default and goes to journald / syslog (see
[`../../explain/audit-channel-design.md`](../../explain/audit-channel-design.md)).

## When to enable

- Hunting an intermittent TUI bug.
- Confirming a refresh-cadence theory.
- Profiling per-peer SSH cost on a slow link.
- Filing a `uxon` bug report — debug logs add the context the
  maintainer needs.

## `UXON_DEBUG=<topics>`

```bash
UXON_DEBUG=tui,startup uxon
```

Comma-separated topic names. Each topic gates a small set of
instrumentation points. Unknown topics are ignored.

Available topics:

| Topic | What it logs |
|---|---|
| `startup` | Startup phases (`mount_started`, `first_paint`, `first_data_landed`). |
| `tui` | TUI state transitions, focus changes, key routing decisions. |
| `tui-table` | Dashboard reconcile diff ops (one log line per applied mutation; zero on a no-op tick). |
| `refresh` | Pluggable refresh-source registry events per source per tick. |

Output goes to `${state_dir}/uxon-debug.jsonl` (one JSON line
per event). Default `state_dir` is
`${XDG_STATE_HOME:-~/.local/state}/uxon`. Override with
`UXON_LOG_DIR=/path`.

## `UXON_METRICS=1`

```bash
UXON_METRICS=1 uxon list --all-hosts --json > /dev/null
cat ~/.local/state/uxon/metrics.jsonl | tail
```

Writes one JSON line per remote-collector fetch attempt with
timing breakdown:

- `peer_name`, `ssh_alias`
- `connect_ms`, `command_ms`, `parse_ms`, `total_ms`
- `outcome` (`ok` / `cache_fallback` / `error` / …)

Rotated at 1 MiB, capped at 3 files.

## Reading the JSONL

```bash
# Pretty-print:
jq . ~/.local/state/uxon/uxon-debug.jsonl | less

# Filter by topic:
jq -c 'select(.topic == "tui-table")' ~/.local/state/uxon/uxon-debug.jsonl

# Histogram fetch latencies:
jq -r '.total_ms' ~/.local/state/uxon/metrics.jsonl | \
  awk '{bucket=int($1/100)*100; count[bucket]++} END {for (b in count) print b "ms", count[b]}'
```

## What's *not* in debug logs

- Audit events. Those go to journald / syslog, queried via
  `journalctl SYSLOG_IDENTIFIER=uxon`.
- Process-level traces. For those, attach `py-spy` / `perf` /
  `strace` to a running `uxon` PID.
- The agent binary's own logs. The agent writes wherever it
  writes (`~/.claude/logs/`, etc.); `uxon` doesn't capture it.

## When you're done

```bash
unset UXON_DEBUG UXON_METRICS UXON_LOG_DIR
```

Or just close the shell. The channels stop writing immediately;
existing JSONL files remain on disk. Delete them:

```bash
rm ~/.local/state/uxon/uxon-debug.jsonl
rm ~/.local/state/uxon/metrics.jsonl*
```

## Common patterns

**TUI startup is slow:**

```bash
UXON_DEBUG=startup uxon
# After it opens, quit. Inspect:
jq -c '.event' ~/.local/state/uxon/uxon-debug.jsonl
# Look for the time delta between mount_started and first_data_landed.
```

**Dashboard repaints feel laggy:**

```bash
UXON_DEBUG=tui-table uxon
# Run for a minute. Each line = one cell mutation.
wc -l ~/.local/state/uxon/uxon-debug.jsonl
# A no-op tick should be 0 lines; non-zero means rows are diffing
# unnecessarily.
```

**Per-peer SSH cost across the fleet:**

```bash
UXON_METRICS=1 uxon list --all-hosts --json > /dev/null
jq -c '{peer: .peer_name, total: .total_ms}' \
  ~/.local/state/uxon/metrics.jsonl
```

## Related

- [`use-uxon-doctor.md`](use-uxon-doctor.md) — the read-only diagnostic.
- [`diagnose-multi-host.md`](diagnose-multi-host.md) — multi-host-specific patterns.
- [`../../explain/architecture.md`](../../explain/architecture.md) — three logging channels overview.

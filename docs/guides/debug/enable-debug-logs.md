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
UXON_DEBUG=keys,startup uxon
```

Comma-separated topic names. Each topic gates a small set of
instrumentation points. Unknown topics are ignored.

Available topics:

| Topic | What it logs |
|---|---|
| `startup` | Startup phases (`mount_started`, `first_paint`, `first_data_landed`). |
| `keys` | Per-keypress trace (every key before binding dispatch), cursor / host-navigation, refresh-dashboard timing, and the event-loop stall watchdog. The primary channel for dropped-keystroke and freeze investigation. |
| `refresh` | Pluggable refresh-source registry events per source per tick (fan-out, per-source spawn / skip). |
| `tui` | Sparse lifecycle markers — app-quit reason and unknown-column-id on config load. |
| `probes` | Host-stats probe failures during `list --json` collection. |

Output goes to `${state_dir}/tui-debug-{user}-{YYYYMMDD}.log` (one
JSON line per event; one file per launch user per day). Default
`state_dir` is `${XDG_STATE_HOME:-~/.local/state}/uxon`. Override
with `UXON_LOG_DIR=/path`.

## `UXON_METRICS=1`

```bash
UXON_METRICS=1 uxon list --all-hosts --json > /dev/null
cat ~/.local/state/uxon/metrics.jsonl | tail
```

Writes one JSON line per fetch attempt (the local context rebuild
plus one per remote peer) with timing:

- `source_id` (`main_ctx_rebuild`, or `remote:<host>` for a peer)
- `elapsed_ms` (wall-time of the fetch attempt)
- `error` (first-line error string, or `null` on success)
- `from_cache` (`true` when the result was served from the
  on-disk snapshot cache)
- `attempted_at` (optional epoch seconds, when the caller supplies it)

Goes to `${state_dir}/metrics.jsonl`, rotated at 1 MiB, capped at
3 files.

## Reading the JSONL

```bash
# Today's debug log (substitute the launch user / date):
DBG=~/.local/state/uxon/tui-debug-$(id -un)-$(date +%Y%m%d).log

# Pretty-print:
jq . "$DBG" | less

# Filter by topic:
jq -c 'select(.topic == "keys")' "$DBG"

# Histogram fetch latencies:
jq -r '.elapsed_ms' ~/.local/state/uxon/metrics.jsonl | \
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
rm ~/.local/state/uxon/tui-debug-*.log
rm ~/.local/state/uxon/metrics.jsonl*
```

## Common patterns

**TUI startup is slow:**

```bash
UXON_DEBUG=startup uxon
# After it opens, quit. Inspect:
DBG=~/.local/state/uxon/tui-debug-$(id -un)-$(date +%Y%m%d).log
jq -c '.at' "$DBG"
# Look for the time delta between mount_started and first_data_landed.
```

**Keystrokes feel dropped, or the dashboard freezes:**

```bash
UXON_DEBUG=keys uxon
# Reproduce the stutter, then quit. Inspect:
DBG=~/.local/state/uxon/tui-debug-$(id -un)-$(date +%Y%m%d).log
# Every key the app saw, plus any event-loop stall the watchdog caught:
jq -c 'select(.topic == "keys")' "$DBG"
```

See [`render-performance.md`](render-performance.md) for the full
dropped-keystroke / idle-CPU playbook.

**Per-peer SSH cost across the fleet:**

```bash
UXON_METRICS=1 uxon list --all-hosts --json > /dev/null
jq -c '{source: .source_id, ms: .elapsed_ms, cached: .from_cache}' \
  ~/.local/state/uxon/metrics.jsonl
```

## Related

- [`use-uxon-doctor.md`](use-uxon-doctor.md) — the read-only diagnostic.
- [`diagnose-multi-host.md`](diagnose-multi-host.md) — multi-host-specific patterns.
- [`../../explain/architecture.md`](../../explain/architecture.md) — three logging channels overview.

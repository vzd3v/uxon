# Tune refresh cadence

Defaults: TUI refreshes every 2 s, the SSH-link probe (only
shown inside an SSH session) every 10 s. On a high-latency link,
slow both down to keep the screen calm. On a fast LAN with a
busy fleet, you might want them faster.

```toml
tui_refresh_interval_seconds      = 5.0
tui_ssh_refresh_interval_seconds  = 30.0
```

## What each key controls

- **`tui_refresh_interval_seconds`** — the local-`tmux` poll
  cadence. The TUI re-enumerates own + cross-user sessions and
  repaints the dashboard at this interval.
- **`tui_ssh_refresh_interval_seconds`** — the SSH-driven
  cadence. Two streams use it: the `ssh-link` RTT probe in the
  bottom server-status block (only visible when you're inside
  an SSH session), and the per-peer remote-sessions poller
  (when `[[remote_hosts]]` is configured).

## Per-peer override

Each `[[remote_hosts]]` entry can override the global cadence:

```toml
[[remote_hosts]]
name      = "lab-fast"
ssh_alias = "lab-fast"
interval         = "3s"        # poll this peer faster than the rest
connect_timeout  = "1s"
total_timeout    = "5s"
```

Use this when peers have wildly different latency or workload
shape (a high-traffic prod box vs. a quiet scratch VM).

## Cost

The local-tmux poll is cheap — it's a `tmux list-sessions` call
on the per-user socket. 0.5 s would work; the 2 s default
balances responsiveness against the dashboard flicker that very
fast repaints produce.

The SSH poll is meaningfully more expensive — even with
`ControlMaster` warm, each tick spawns `ssh` to run a remote
command, parses JSON, and writes the snapshot cache. 10 s is
fine for ~10 peers on a LAN; bump to 30 s for ~50 peers or for
peers across a slow link.

## Related

- [`override-ssh-per-peer.md`](override-ssh-per-peer.md) — full per-peer transport override.
- [`../../reference/configuration.md`](../../reference/configuration.md) — `tui_refresh_interval_seconds`, `tui_ssh_refresh_interval_seconds`, `interval` per peer.

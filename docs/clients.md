# Connect to a Uxon host

Connect to a Linux host over SSH, then start the dashboard:

```bash
ssh dev-host
uxon
```

An alias from your existing `~/.ssh/config` can also be used in a
[configured peer](reference/configuration.md#remote_hosts-table-array).

Sessions live in tmux on the host. Losing the terminal connection does not
stop their workloads. Reconnect, run `uxon`, and select the session to attach.

If you need the interactive connection itself to reconnect automatically,
consider [Eternal Terminal](https://eternalterminal.dev/) or
[Mosh](https://mosh.org/), following its installation and security documentation.
Uxon's peer collector uses its configured transport independently of the
terminal connection. SSH authentication and network access are managed outside Uxon.

# Migrate from a previous session prefix

You changed `session_prefix` and have running sessions under the
old value that you don't want to lose track of.

```toml
session_prefix          = "uxon-"
legacy_session_prefixes = ["old-"]
```

`list`, `attach`, `kill`, and `kill-all` recognise both
prefixes. New sessions are *always* created under
`session_prefix`. `uxon` never *creates* a session under a
legacy prefix.

## Workflow

1. Set `legacy_session_prefixes` to the previous value.
2. Existing sessions remain reachable via `list` / `attach` /
   `kill`.
3. New sessions go under the new `session_prefix`.
4. Once you've migrated or reaped every legacy session,
   remove `legacy_session_prefixes` (or drop the entry from the
   array) so `uxon doctor` stops reporting legacy presence.

## What `uxon doctor` shows

```
sessions on dedicated socket:
  uxon-newproj@claude   ...
sessions matching legacy_session_prefixes on default socket:
  old-myproj@claude     ...
```

Legacy sessions on the *default* `tmux` socket (rather than the
dedicated one) are also surfaced — these are pre-`session_prefix`
sessions that pre-date `uxon`'s socket isolation. `uxon` won't
create new ones there, but reaches them for `list` / `attach` /
`kill` so they remain manageable.

## Reference

- [`../../reference/configuration.md`](../../reference/configuration.md) — `session_prefix`, `legacy_session_prefixes`.
- [`../../reference/cli.md`](../../reference/cli.md) — identifier resolution.

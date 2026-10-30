# Change the session prefix

Keep an old prefix recognized while new sessions use a new one:

```toml
session_prefix = "uxon-"
legacy_session_prefixes = ["old-"]
```

Prefixes apply to sessions on the configured dedicated socket. Existing
sessions retain their names; changing a prefix does not move sessions between sockets.

Inspect sessions with `uxon list`. Remove an old prefix after its sessions have
been drained. Removal still requires captured launch identity and protected
records; a recognized name alone is insufficient.

`uxon doctor` also reports old default-socket sessions. Uxon does not silently
manage them through its dedicated socket. Drain or migrate them deliberately
in their original environment before changing socket policy.

See the [configuration reference](../../reference/configuration.md#top-level-keys)
and [migration guide](../../migrations.md).

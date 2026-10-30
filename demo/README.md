# Dashboard demos

Synthetic YAML scenarios render into current Uxon wire envelopes. Demo session
collection reads those envelopes instead of querying real tmux sessions or SSH
peers. The YAML scenario format has its own version; the output wire version is
imported from the source tree.

## Interactive preview

```bash
./demo/uxon-demo list
./demo/uxon-demo run team-n
```

The runner uses the checkout's `.venv/bin/uxon`, creating the environment with
`uv sync` when possible. The renderer uses `uv` with its declared PyYAML
script dependency. Output lives under
`${XDG_CACHE_HOME:-$HOME/.cache}/uxon-demo/build/`.

Built-in `solo-1`, `solo-n`, `team-1` and `team-n` scenarios include local
sessions through their `local:` lists. Local rows are filtered by the requested
launch user, so an interactive preview's user configuration must match those
fictional accounts. This mode replaces session data, not all host configuration
or diagnostics. Do not use live action keys for synthetic sessions.

## Reproduce the README image

From the development environment:

```bash
uv run --with pyyaml python demo/screenshot.py demo/scenarios/readme.yaml docs/images/uxon-full-team-n.svg
```

This runs the actual Textual application headlessly at a fixed terminal size
and exports its native SVG. It supplies fictional context and decoded scenario
snapshots, disables host probes and refresh sources, and performs no launch,
attach or removal operations. No local usernames, paths or host diagnostics are
used. The displayed Uxon version comes from the current source.
The export uses the normal color palette even when the shell sets `NO_COLOR`.

## Author a scenario

Copy a file from `demo/scenarios/`. `local:` supplies local session records;
`hosts:` supplies named peer records. Each session identifies a fictional user,
project slot and coding tool. Optional CPU/RAM, timestamps and host metrics
exercise the corresponding display. Keep paths and identities synthetic.

`demo/render.py` documents the fields. Generated PIDs are deterministic; times
are relative to rendering. Records omit destructive identity authority and are
for display only.

The implementation is in `uxon.infra.demo`: it creates synthetic peers with the
`__uxon_demo__:` sentinel, reads remote snapshots through the production decoder,
and loads `_local.json` for local session collection. Files beginning with `_`
are not peers. Missing `_local.json` means an empty local session list.

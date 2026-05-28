# `demo/` — screenshot & article fixtures

Reproducible demo data for screenshots, GIFs, and articles. Each
scenario is a YAML file that renders into a directory of wire envelopes;
the TUI reads them when `UXON_DEMO_HOSTS` points at that directory,
bypassing SSH entirely.

No second machine, no real `ssh` config, no peer install: any host with
`uv` and a copy of this repo can produce the screenshots.

## Available scenarios

| Scenario  | Hosts | Users | Sessions | What it shows |
|-----------|-------|-------|----------|---------------|
| `solo-1`  | 1 (local)         | 1 | 3 | One dev on his own box. Three sessions — attached / idle / stale — so the LAST column shows every tint. |
| `solo-n`  | 2 (local + 1 SSH) | 1 | 4 | Same person across his primary dev box (local) and a GPU box he leases for ML. Multi-host HOST column; abandoned work sits on the remote. |
| `team-1`  | 1 (local)         | 4 | 4 | The "shared dev host" case from the article — four engineers on one box under low-priv `-agent` accounts. The lead opens uxon and sees all four via sudo. |
| `team-n`  | 5 (local + 4 SSH) | 6 | 9 | Headline: lead opens uxon on his own box, sees teammates' personal dev boxes + a small `dev-common` (the article's *exception*) + a GPU box. Mix of agents, a couple of abandoned sessions. |

## Run it

```bash
# List scenarios.
./demo/uxon-demo list

# Render + launch the TUI in one go.
./demo/uxon-demo run team-n

# Render only — print the export line for shell use.
./demo/uxon-demo render team-n
UXON_DEMO_HOSTS=demo/build uxon

# eval form for one-liner setup.
eval "$(./demo/uxon-demo env team-n)"; uxon

# Wipe the render cache.
./demo/uxon-demo clean
```

Requirements: `uv` (auto-installs `pyyaml` for the renderer in a one-shot
script venv) and `uxon` on `$PATH` (or `uv run uxon` from the repo).

The render output lives under `${XDG_CACHE_HOME:-$HOME/.cache}/uxon-demo/build/`
(per-user, never committed). On a shared dev box each OS user gets
their own copy and there's no permission collision.

## How it works

1. `demo/uxon-demo` reads a scenario YAML and invokes `demo/render.py`.
2. `render.py` writes one `<host>.json` wire envelope per host to
   `$XDG_CACHE_HOME/uxon-demo/build/` — same schema as `uxon list --json` emits on a peer.
3. With `UXON_DEMO_HOSTS=demo/build` set:
   * `uxon._demo.synthesize_remote_hosts` creates one synthetic
     `RemoteHost` per `<host>.json` envelope (alias prefixed with
     `demo:`). Files starting with `_` are reserved and skipped.
   * `uxon.remote_collector.fetch_remote_snapshot` short-circuits on
     the `demo:` sentinel and loads the per-peer envelope from disk
     instead of calling `ssh`.
   * `uxon.cli.collect_sessions_for_user` short-circuits on the env
     var and reads the optional `_local.json` envelope (same wire
     schema as a peer file), filtered by the requested OS user.
     Absent file ⇒ empty local section. tmux is never invoked.

That's the entire mechanism: one env var, one private module
(`src/uxon/_demo.py`), three short patches at the data-source seams
(remote-hosts registry, remote fetch, local rebuild).

## The local section

By default, in demo mode the local section is **empty** — `_local.json`
is not rendered by any built-in scenario, the collector finds no file,
and tmux is never touched. That's what you want on a multi-tenant dev
box where the caller's tmux server is full of unrelated real sessions.

To show synthetic local sessions for a screenshot (e.g. a one-host
"solo" demo where the local section is the whole point), render a
`_local.json` envelope alongside the peer files. Same wire schema as a
peer envelope, with `data.sessions[*].user` set to the OS user the
viewer will be logged in as. Authoring a renderer-level YAML field for
this is left as a follow-up — for now, hand-write the JSON or copy a
peer envelope as a template.

## Authoring a new scenario

Copy an existing YAML (`team-n.yaml` covers most fields), tweak. The
renderer (`render.py`) documents every supported key in its
docstring. Useful patterns:

* `attached: true` → session is currently held by a client.
* `attached_min_ago: <N>` → session was last attached N minutes ago.
  Combine with `cpu_pct: 0.0` and low `rss_mib` for "abandoned" feel.
* Omit `attached_min_ago` entirely for a never-attached session.
* `agent` ∈ `claude | codex | cursor` — the AGENT column reads this.
* `windows: <N>` shows in the WIN column; bump it for "busy" sessions.
* `host_stats` (optional, per host) populates the host status bar.
* `color` (optional, per host) pins a Rich color; otherwise the TUI
  auto-assigns from `tui.color_palette`.

PIDs are generated deterministically from `(host, user, slot)` so the
rendered JSON diffs cleanly when you tweak a scenario.

## Why this is checked into the repo

Contributors changing the TUI need a way to re-take screenshots
without setting up a real cross-host fleet. The envelope files also
double as living examples of the wire schema (`src/uxon/wire_schema.py`).

What is *not* checked in: the render output (under
`$XDG_CACHE_HOME/uxon-demo/build/`) is per-user cache and never lands
in the repo. Generated PNG / GIF screenshots for the README live under
`docs/images/`, not here.

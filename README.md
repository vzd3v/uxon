# uxon

[![PyPI](https://img.shields.io/pypi/v/uxon)](https://pypi.org/project/uxon/)
[![CI](https://github.com/vzd3v/uxon/actions/workflows/ci.yml/badge.svg)](https://github.com/vzd3v/uxon/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![Platform: Linux](https://img.shields.io/badge/platform-Linux-lightgrey)

Session manager for development teams using terminal AI coding
tools on one or more Linux servers. Configure any tool's executable,
arguments and permission modes; Claude Code, Codex and Cursor CLI come with
built-in presets. Team visibility via OS accounts, cross-host visibility
via SSH, supervision via sudoers.

![uxon dashboard — a lead sees the whole team's agent sessions across hosts](docs/images/uxon-full-team-n.svg)

## When to use uxon

Use `uxon` when terminal AI coding agents are a runtime someone
else may need to see, attach to, or stop. Four shapes of
deployment, one tool:

- **One developer, one host.** Persistent TUI over `tmux`;
  agents optionally run in a separate low-privilege `<user>-agent`
  account, with access bounded by that account's permissions.
- **One developer, several hosts.** Aggregate everything into
  one TUI with a `HOST` column; locals first, then peers
  grouped by host.
- **A team sharing one host.** Each developer runs as their
  paired `<user>-agent`. The lead's TUI sees everyone via
  `sudo`. Cross-user supervision without impersonation —
  the lead never becomes the developer.
- **A team across several hosts.** Same supervision property
  per host; per-peer authority (each host's `sudoers` is the
  authority on that host); cross-host audit correlation via
  UUID `correlation_id` joining initiating-host and target-host
  events.

Aggregation is client-side, with [configurable peer polling](docs/reference/configuration.md#remote_hosts-table-array).
The lead's TUI fans out over SSH; each host stays independently authorised.
No daemon, database or central server is required.

Launch profiles choose the coding tool, launch account and optional workload
runtime, including containers. Execution backends can place target-user commands
and tmux inside an operator-managed boundary. Shared supervision requires
[protected launch records](docs/reference/configuration.md#launch-records).

## Install

Requires **Python 3.11+**, Linux, and a maintained `tmux` build (3.8 recommended;
see [requirements](docs/start/install.md#requirements)).

```bash
# Team / shared host (recommended): one root-owned binary in
# /usr/local/bin/uxon. Operator owns the version and the install
# path; launch users can append audit events but cannot edit
# the binary or the trail.
sudo pipx install --global uxon

# Solo / single-owner: each OS user manages their own copy.
uv tool install uxon              # or:  pipx install uxon

uxon                              # launch the TUI; it self-diagnoses
```

For the bundled installer and unreleased builds, see
[`docs/start/install.md`](docs/start/install.md).

## Documentation

Start with your scenario:

- [Solo on a single host](docs/scenarios/solo-1.md)
- [Solo on multiple hosts](docs/scenarios/solo-n.md)
- [Team on a single host](docs/scenarios/team-1.md)
- [Team on multiple hosts](docs/scenarios/team-n.md)

The [documentation index](docs/index.md) also organizes tutorials, how-tos,
reference and explanation by [Diátaxis](https://diataxis.fr).
See [client setup](docs/clients.md), [privacy](docs/privacy.md),
[upgrade notes](docs/migrations.md), [security](SECURITY.md),
[changes](CHANGELOG.md), and [contributing](CONTRIBUTING.md).

## Quick TUI tour

`uxon` (no args, on a TTY) opens a full-screen picker:

- **Open project** — browse and pick a directory at any depth under
  `new_project_root` and launch.
- **Create new project** — prompt for a name, create
  `<new_project_root>/<name>`, optionally create a GitHub repo,
  launch the agent.
- **Open in this folder** — choose a launch profile, permission mode and
  workspace. Available after the background access check succeeds.

The dashboard combines your sessions, authorized other-user sessions and live
data from configured SSH peers. Failed polls show errors. Search configured
session fields, then attach or stop a session with confirmation.
Terminal titles follow the active session and return to `uxon` in the dashboard.
See [TUI keys](docs/reference/keybindings.md)
and [dashboard recipes](docs/guides/customise/customise-dashboard.md).

Failed processes keep their output visible with an on-screen return hint,
without stopping other running terminals.
See [failed-process controls](docs/reference/keybindings.md#failed-process-terminal).

## Coding tools

The coding-tool catalog is extensible through configuration. Add a tool's
executable and modes, then enable a launch profile for it; no Uxon code change
is needed. See [add a coding tool](docs/guides/customise/choose-launch-profile.md#add-a-coding-tool).

The built-in presets are examples you can use or override:

| Preset | Executable |
|--------|------------|
| `claude` | `claude` |
| `codex` | `codex` |
| `cursor` | `cursor-agent` |

For example, expose two built-in launch profiles in `/etc/uxon/config.toml`:

```toml
[launch]
enabled_profiles = ["claude", "codex"]
default_profile = "claude"
```

Fleet automation can validate and render the full JSON form with
`uxon config render --config-json config.json`; install the reviewed TOML at
`/etc/uxon/config.toml` with an explicit root-owned `sudo install`.

Uxon manages worktrees independently of the agent. See the
[profile/mode reference](docs/reference/cli.md#--mode-id)
and [worktree guide](docs/guides/customise/worktrees.md).

`uxon doctor` probes the configured catalog and prints each tool's path,
version, and status.

## Versioning

`uxon` follows [SemVer](https://semver.org/). `uxon --version`
prints the version and short git commit (with `-dirty` when the
checkout has uncommitted changes).

Fleet peers require compatible [wire schemas](docs/reference/wire-schema.md#versioning).
Only live session data is displayed; failed peers show a compact status below the table.

## License

[MIT](LICENSE) © 2026 Vasily Zakharov.

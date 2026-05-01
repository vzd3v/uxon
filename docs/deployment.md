# Deployment

This document is for operators running `uxon` on one or more shared
hosts. For a single-user laptop install, the
[Install](../README.md#install) section in `README.md` is enough.

## Single-host install

```bash
git clone https://github.com/vzd3v/uxon.git
cd uxon
sudo python3 install/install_uxon.py \
  --repo-dir "$(pwd)" \
  --install-path /usr/local/bin/uxon

cp config/config.example.toml config/config.toml
$EDITOR config/config.toml         # set allowed_roots, session_users, agents
uxon doctor                         # verify
```

## Multi-host topology

When `uxon` runs on more than one host, decide up front:

- **Canonical checkout location.** Pick one path, e.g. `/opt/uxon`
  or `/srv/apps/uxon`, and use it on every host. `/usr/local/bin/uxon`
  stays a symlink into that checkout.
- **One source of config truth per host.** The repo ships
  `config/config.example.toml` as a starting point; host-local
  `config/config.toml` is gitignored and operator-owned.
- **Pinned ref.** Deploy a tag or commit, not `main`, when you want
  determinism. Verify with `uxon --version`.

The infra repo / Ansible / Salt / whatever you use may:
- clone or update this repo on each host;
- pick a target ref;
- hand a host-specific JSON payload to
  `install/render_uxon_config.py` to generate `config.toml`;
- own ACLs on the editable checkout (group-writable for admins,
  read-only for everyone else).

The infra repo **must not** become a second canonical location for:
- the `uxon` executable;
- the config schema;
- config-rendering logic.

## Runtime dependencies

- **Python ≥ 3.11.** Stdlib `tomllib` is used for config reads.
- **`tomlkit`.** Required for config writes (TUI Settings screen).
  Install via `apt install python3-tomlkit` or `pip install tomlkit`
  into the Python environment `uxon` runs under. Without it, TUI saves
  fail; CLI subcommands (`list`, `doctor`, `run`, `new`, `attach`,
  `kill`) keep working.
- **`textual >= 0.80, < 9`.** Required for the interactive TUI.
  Lazy-imported inside `do_interactive`, so non-TUI subcommands run
  without it.
- **`gh` CLI.** Required on hosts that use `auth = "gh"` git-remote
  profiles. Run `gh auth login` once as the configured `creds_user`.

## Config contract

These keys steer rollout behaviour and deserve explicit values per
host:

- `repeat_noninteractive_mode` — `fail` (default), `attach`, or
  `new`. Keep `fail` unless the host explicitly wants unattended
  attach/new.
- `tmux_socket_template` — absolute per-user socket template
  (default `/tmp/uxon-{user}.sock`; supports `{user}` and `{uid}`).
  Keep the default unless a different absolute path is required.
- `allowed_roots` — declare every directory under which agents may
  be launched. The launch user's home is always implicitly allowed.
- `new_project_root` — base directory for `uxon new <name>` (default
  `~/projects`). Must be inside an `allowed_roots` entry.

### Git-remote profiles

`git_create_enabled`, `default_git_remote_profile`, and
`[[git_remote_profiles]]` are **hand-edited** in `config.toml` —
they are intentionally not part of the
`install/render_uxon_config.py` JSON-to-TOML flow because profiles
reference `creds_user` and `token_file`, and infra shouldn't
hard-code those across hosts. The TUI shows them read-only. See
[`README.md` § Git remote on new project](../README.md#git-remote-on-new-project)
for field reference and examples.

## Verification checklist

Run after each rollout:

1. `uxon --version` — matches the deployed ref.
2. `uxon doctor` — clean (includes per-profile status for any
   configured `[[git_remote_profiles]]`, read-only probe).
3. Plain `uxon -n <throwaway>` — creates project, attaches.
4. Worktree `uxon -n <throwaway> -w <branch>` — succeeds when the
   directory already contains a git repo with that branch.
5. `uxon kill-all --dry-run` — prints the plan; `uxon kill-all` (with
   confirmation) actually kills.
6. Reported dedicated socket path matches the deployed config.
7. If git-remote profiles are enabled:
   `uxon -n <throwaway> --git-remote <profile> --dry-run` prints the
   full command plan without executing.

## Migration notes

### 1.x → 2.0

- **Defaults moved.** `allowed_roots` defaults to `[]` and
  `new_project_root` defaults to `~/projects`. Existing deployments
  override both — no action required if your `config.toml` already
  sets them.
- **Log directory default.** TUI events default to
  `${XDG_STATE_HOME:-~/.local/state}/uxon`. Set
  `UXON_LOG_DIR=/old/path/here` in the launch user's environment to
  preserve the previous location.
- **Internal agent material untracked.** `AGENTS.md`, `CLAUDE.md`,
  `.claude/`, `docs/plans/`, `docs/superpowers/`, `docs/prototypes/`
  are no longer tracked. Operators do not need to do anything.

### Multi-agent config schema (1.3)

The flat `default_claude_args` key is removed. Config uses nested
tables:

```toml
[agents]
enabled = ["claude", "cursor"]
default = "claude"

[agents.claude]
default_args = []

[agents.codex]
default_args = []

[agents.cursor]
default_args = []
```

Manual migration per host: replace the flat
`default_claude_args = [...]` line with the nested `[agents]`
tables, include only agents installed on that host in `enabled`,
then run `uxon doctor` to verify.

If the legacy flat key is present on load, `uxon` fails with a clear
error pointing here.

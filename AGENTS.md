# AGENTS.md — rules for agents working in this repo

These are project-local rules on top of `~/.claude/CLAUDE.md` global rules.
Keep this file tight; don't duplicate README content — link to it instead.

## Scope

`ccw` is a multi-user `tmux` wrapper for `claude` on a shared VPS. User-facing
behavior, commands, flags, TUI, configuration, and rollout docs all live in
[README.md](README.md). Deployment topology notes live in
[docs/deployment.md](docs/deployment.md).

## Code layout

- `bin/ccw` — CLI entrypoint (wires subcommands and builds the TUI context).
- `lib/ccw_tui.py` — TUI main loop and main-screen rendering
  (requires `blessed`; imported lazily).
- `lib/ccw_tui_widgets.py` — stateless, reusable TUI primitives
  (`dim`, `text_input`, `confirm_phrase`, `confirm_yn`, `flash_error`).
  Prefer adding shared widgets here over re-implementing them in a screen.
- `lib/ccw_tui_mouse.py` — SGR-1006 mouse enable/disable, sequence parser,
  `read_input()` helper that returns either a blessed Keystroke or a
  `MouseEvent`, and `HitRegion` / `hit_test` for row routing.
- `lib/ccw_tui_modals.py` — `run_modal(t, modal)` dispatcher and
  `MenuModal` list-selection helper. Callers must re-render their own
  screen after a modal dismisses; the dispatcher does not save/restore.
- `lib/ccw_tui_settings.py` — superuser settings sub-screens.
  Knows nothing about file I/O; all side effects go through a
  `SettingsCallbacks` bundle provided by `bin/ccw`.
- `lib/ccw_settings.py` — settings schema (the single list of known keys),
  layer resolution (default / repo / project), round-trip TOML writer
  (via `tomlkit`, preserves comments and formatting), and safe repo-file
  persistence (with `sudo` fallback).
- `lib/ccw_git_profiles.py` — pure-data schema for
  `[[git_remote_profiles]]`: dataclass, validation, URL/API-base helpers.
- `lib/ccw_git_backend_gh.py` — `gh`-CLI backend: preflight + `gh repo
  create` under `creds_user`.
- `lib/ccw_git_backend_token.py` — fine-grained-PAT backend: reads
  `token_file` under `creds_user`, calls the REST API via `urllib`, and
  guarantees the token never leaves memory or appears in any log/dry-run
  output.
- `lib/ccw_git_create.py` — orchestrator for the git-remote-on-new-project
  pipeline. Dispatches to the matching backend, drives local git under
  launch_user, raises `CreationError(stage=...)` on failure.
- `install/` — installer and config renderer.
- `tests/` — `unittest`, discovered via `python3 -m unittest`.
- `config/` — host-local, gitignored. Source of truth for a running host.
- `VERSION` — human-owned release tag.

### Module boundaries

- `bin/ccw` may import from `lib/*`. `lib/*` modules never import from
  `bin/ccw` — the CLI assembles the pieces, not vice versa.
- UI files (`lib/ccw_tui*.py`) must not import `subprocess`, `pwd`, or
  touch the filesystem directly; push those through callbacks in
  `TuiContext` / `SettingsCallbacks`.
- `lib/ccw_settings.py` is pure data + TOML I/O. No blessed, no TUI.
- When adding a new screen/widget, decide first: is it generic
  (→ `ccw_tui_widgets.py`), domain-specific reusable
  (→ its own `ccw_tui_<feature>.py` module), or one-off main-screen
  behavior (→ `ccw_tui.py`).

## Hard rules

- **No `claude` invocations added outside of `launch_in_tmux`.** `ccw` is the
  single place that builds the `claude` command line.
- **Heavy/UI-only deps are imported lazily** so non-UI paths (`ccw list`,
  `ccw doctor`, `ccw version`) keep working without them installed.
  `blessed` is imported lazily by TUI files; same rule for any future
  dependency that's not needed on every code path.
- **Config writes require `tomlkit`.** The round-trip TOML writer in
  `lib/ccw_settings.py` imports it lazily; CLI read paths stay on stdlib
  `tomllib`. Installer must ensure `tomlkit` is available in the Python
  env `ccw` runs under (`python3-tomlkit` on Debian/Ubuntu, or
  `pip install tomlkit`).
- **Dedicated tmux socket stays per-user.** Don't add code paths that fall
  back to the default socket silently; fail with a hint instead (see
  `repeat_guardrail_for_legacy_socket`).
- **`--dsp` is the canonical short form.** `--dap`, `-dap`, `-dsp` are legacy
  aliases — keep them accepted, don't add new ones.
- **Session naming is stable.** `cc-<stem>` / `cc-<stem>-N`. Changing the
  scheme breaks every operator's muscle memory and existing sessions.
- **Passwordless-sudo detection must stay fast.** `detect_passwordless_sudo`
  has a 0.5 s timeout; don't add probes that can exceed it.

## When you change user-visible behavior

1. Bump `VERSION` (semver-ish: minor for new features, patch for fixes).
2. Update [README.md](README.md) — a single section, no duplication.
3. Add/adjust `tests/` coverage.
4. Run the local checks below.
5. Mention the change in the commit message.

## Local checks (always run before committing)

```bash
python3 -m py_compile bin/ccw lib/ccw_tui.py lib/ccw_tui_widgets.py \
  lib/ccw_tui_settings.py lib/ccw_tui_mouse.py lib/ccw_tui_modals.py \
  lib/ccw_settings.py \
  lib/ccw_git_profiles.py lib/ccw_git_backend_gh.py \
  lib/ccw_git_backend_token.py lib/ccw_git_create.py \
  tests/test_ccw.py tests/test_ccw_tui.py tests/test_ccw_settings.py \
  tests/test_ccw_tui_mouse.py tests/test_ccw_tui_modals.py \
  tests/test_ccw_git_profiles.py tests/test_ccw_git_backend_gh.py \
  tests/test_ccw_git_backend_token.py tests/test_ccw_git_create.py \
  install/install_ccw.py install/render_ccw_config.py
python3 -m unittest discover -s tests -p 'test_*.py'
```

CI runs the same two commands. If CI catches something local checks miss,
add a test for it.

## Config

- Two layers: repo-level `config/config.toml` (rendered from JSON, edited
  directly or via the TUI Settings screen) and the nearest project-level
  `.ccw.toml` within an `allowed_roots` entry. The TUI never writes
  `.ccw.toml`.
- When adding a config key:
  1. Extend `DEFAULT_CONFIG` + `Config` + `load_config` in `bin/ccw`.
  2. Add validation if the value space is constrained.
  3. Add a matching `SettingSpec` in `lib/ccw_settings.py::SETTINGS_SPECS`
     so it shows up in the TUI Settings screen.
  4. Document it in the README config table.
  5. Add a `load_config` test in `tests/test_ccw.py` and a round-trip test
     in `tests/test_ccw_settings.py` if the value has non-trivial encoding.

## Docs

- README.md: user-facing — what the tool does, commands, TUI, config.
- docs/deployment.md: operator-facing — host topology, rollout contract.
- AGENTS.md (this file): agent-facing — rules, boundaries, workflow.
- CLAUDE.md: pointer to AGENTS.md (Claude Code convention).

Don't split user-facing content across README + docs/. One place, no
duplication. Refactor when sections start overlapping.

## Git workflow

- Commit with descriptive subject + short body (why, not just what).
- Bump `VERSION` in the same commit as the behavior change.
- Never skip hooks (`--no-verify`) or amend published commits.

# AGENTS.md — rules for agents working in this repo

These are project-local rules on top of `~/.claude/CLAUDE.md` global rules.
Keep this file tight; don't duplicate README content — link to it instead.

## Scope

`ccw` is a multi-user `tmux` wrapper for AI coding agents (`claude`, `codex`,
`cursor-agent`) on a shared VPS. User-facing behavior, commands, flags, TUI,
configuration, and rollout docs all live in [README.md](README.md). Deployment
topology notes live in [docs/deployment.md](docs/deployment.md).

## Code layout

- `bin/ccw` — CLI entrypoint (wires subcommands and builds the TUI context).
- `lib/ccw_tui/` — textual app. Every screen declares ``BINDINGS``; CSS
  lives in ``styles.tcss``; launch TTY handoff uses ``App.exit()`` + outer
  re-create loop (see ``app.py::run``). Sub-modules:
    - ``context.py`` — pure data (``TuiContext``, ``TuiSession``,
      ``LaunchRequest``, ``Item``, ``build_items``, ``CallbackError``).
      No textual / no blessed.
    - ``events.py`` — best-effort JSONL event log.
    - ``launch.py`` — fork-and-wait launch helper + failure pause banner.
      Runs **outside** the textual App between round-trips.
    - ``hints.py`` — ``TEXTUAL_MISSING_HINT`` install guidance.
    - ``app.py`` — ``CcwApp(App)`` + ``run(ctx)`` outer loop.
    - ``screens/`` — one module per screen (``main`` + modals):
        ``agents_unavailable.py`` — modal shown when every enabled agent is missing.
    - ``widgets/`` — ``ActionRow``, ``SessionTable`` (only two custom
      widgets; everything else is stock textual).
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
- `lib/ccw_agents.py` — pure-data `AgentSpec` catalog (`CATALOG`), per-agent
  `PermissionMode` definitions, `AgentAvailability`, and the parallel
  `probe_agents(...)` availability probe. No textual, no TUI.
- `install/` — installer and config renderer.
- `tests/` — `unittest.TestCase`; run via `pytest tests/ -n auto`
  (stdlib `python3 -m unittest discover -s tests` still works as fallback).
- `config/` — host-local, gitignored. Source of truth for a running host.
- `VERSION` — human-owned release tag.

### Module boundaries

- `bin/ccw` may import from `lib/*`. `lib/*` modules never import from
  `bin/ccw` — the CLI assembles the pieces, not vice versa.
- UI files (under `lib/ccw_tui/`) must not import `subprocess`, `pwd`,
  or touch the filesystem directly; push those through callbacks on
  `TuiContext` / `SettingsCallbacks`.
- `lib/ccw_settings.py` is pure data + TOML I/O. No textual, no TUI.
- When adding a new screen, drop a module under
  ``lib/ccw_tui/screens/``, declare ``BINDINGS`` there, and wire the
  push from ``MainScreen`` (or the relevant existing screen).
- Custom widgets go under ``lib/ccw_tui/widgets/``. Resist the urge —
  most cases should compose stock textual widgets.

## Hard rules

- **No agent invocations added outside the launch builder
  (`_build_tmux_launch_request`).** `ccw` is the single place that builds
  agent command lines. Adding direct agent exec calls anywhere else is
  forbidden — add them to the launch builder, which consults `ccw_agents.CATALOG`.
- **Textual is imported lazily inside ``do_interactive``.** Non-TUI
  subcommands (``ccw list``, ``ccw doctor``, ``ccw version``) never
  import ``ccw_tui`` and therefore do not require textual.
- **All key handling goes through ``BINDINGS``.** No ``on_key`` overrides
  on screen classes — the drift guard in
  ``tests/test_ccw_tui_bindings.py`` refuses the PR otherwise. Destructive
  bindings (``kill*``) MUST have ``show=True`` + a non-empty description
  so the footer reflects them.
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
- **Session naming is stable.** New sessions: `ccw-<stem>@<agent>` /
  `ccw-<stem>@<agent>-N` (index after the agent suffix). Legacy `cc-<stem>` /
  `cc-<stem>-N` sessions are read-only as `claude`; `ccw` never creates new
  `cc-*` sessions. The canonical prefix for new sessions is `ccw-` (hardcoded).
  Do not change this scheme without updating `parse_session_name` and
  `candidate_session_name` in unison.
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
python3 -m py_compile bin/ccw \
  lib/ccw_tui/__init__.py lib/ccw_tui/app.py lib/ccw_tui/context.py \
  lib/ccw_tui/events.py lib/ccw_tui/launch.py lib/ccw_tui/hints.py \
  lib/ccw_tui/widgets/__init__.py lib/ccw_tui/widgets/action_row.py \
  lib/ccw_tui/widgets/session_table.py \
  lib/ccw_tui/screens/__init__.py lib/ccw_tui/screens/main.py \
  lib/ccw_tui/screens/confirm.py lib/ccw_tui/screens/launch_options.py \
  lib/ccw_tui/screens/new_project.py lib/ccw_tui/screens/git_profile.py \
  lib/ccw_tui/screens/existing.py lib/ccw_tui/screens/settings.py \
  lib/ccw_tui/screens/git_remotes.py \
  lib/ccw_tui/screens/agents_unavailable.py \
  lib/ccw_settings.py lib/ccw_agents.py \
  lib/ccw_git_profiles.py lib/ccw_git_backend_gh.py \
  lib/ccw_git_backend_token.py lib/ccw_git_create.py \
  tests/test_ccw.py tests/test_ccw_tui.py tests/test_ccw_settings.py \
  tests/test_ccw_tui_screens.py tests/test_ccw_tui_widgets_textual.py \
  tests/test_ccw_tui_bindings.py tests/test_ccw_tui_logging.py \
  tests/test_ccw_git_profiles.py tests/test_ccw_git_backend_gh.py \
  tests/test_ccw_git_backend_token.py tests/test_ccw_git_create.py \
  tests/test_ccw_agents.py tests/test_ccw_tui_agents_unavailable.py \
  install/install_ccw.py install/render_ccw_config.py
pytest tests/ -n auto
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

# Contributing to ccw

Thanks for taking the time. This guide covers everything you need
to make and ship a change.

## Local setup

`ccw` is a Python script tool. There is no `pip install ccw` step —
you run the script directly out of a checkout.

```bash
git clone https://github.com/vzd3v/vz_devagent_cli_tool.git
cd vz_devagent_cli_tool
python3 -m pip install -e .[dev]   # ruff, pyright, pytest, textual, tomlkit
# or, minimal: python3 -m pip install textual tomlkit pytest pytest-xdist
./bin/ccw --version
```

`ccw` requires Python 3.11+. The TUI needs `textual >=0.80,<9`. Config
writes need `tomlkit`. Both are optional for non-TUI subcommands.

## Local checks

Before opening a PR, run:

```bash
python3 -m py_compile $(git ls-files '*.py' bin/ccw)
python3 -m pytest tests/ -n auto
ruff check .
ruff format --check .
pyright
```

CI runs the same. If something passes locally but breaks CI, please
add a test that catches it.

## Branching and commits

- Branch off `main`. Open PRs against `main`.
- One logical unit per commit. No drive-by refactors mixed in with a
  bug fix; split them into separate commits.
- Commit-message subject in imperative mood ("add", "fix",
  "rewrite") with a short body explaining *why*. Conventional Commits
  (`feat:`, `fix:`, `docs:`, `chore:`, `refactor:`) is the existing
  style — match it.
- Never bypass git hooks (`--no-verify`) and never amend a published
  commit.

## When to bump `VERSION`

Bump `VERSION` in the same commit as any user-visible behaviour
change. Semver:
- **Major** for breaking changes to the CLI surface, the config
  schema, or the on-disk session-naming scheme.
- **Minor** for new features that don't break existing config or
  session names.
- **Patch** for bug fixes and doc-only changes that affect users.

Then add a matching entry to [`CHANGELOG.md`](CHANGELOG.md) under
`## [Unreleased]`.

## Architectural rules a contributor will hit

These are non-negotiable — see [`docs/architecture.md`](docs/architecture.md)
for the full picture. Quick list:

- **`textual` is imported lazily inside `do_interactive`.** Non-TUI
  subcommands (`ccw list`, `ccw doctor`, `ccw version`) must run
  without `textual` installed.
- **All key handling goes through `BINDINGS`.** No `on_key` overrides
  on screen classes; a drift guard test refuses the PR otherwise.
- **Config writes use `tomlkit`** — round-trip preserves comments and
  formatting. CLI read paths stay on stdlib `tomllib`.
- **One launch builder.** `ccw` is the single place that builds agent
  command lines (`_build_tmux_launch_request` in `bin/ccw`). Don't
  add direct agent exec calls anywhere else.
- **Module boundaries.** `bin/ccw` may import from `lib/*`; nothing
  in `lib/*` may import from `bin/ccw`. UI files under
  `lib/ccw_tui/` must not import `subprocess`/`pwd` or touch the
  filesystem directly — push that through callbacks on `TuiContext`.

## Tests

- Prefer pure tests in `lib/ccw_tui/state.py` for branchy UI logic.
  Pilot/pty tests are reserved for Textual wiring (mounting, key
  routing, `ListView`/`DataTable` events, async workers,
  `call_later`).
- Don't add a new `App.run_test()` case when a pure state helper can
  cover the behaviour and a smoke test already proves the wiring.
- Batch compatible Pilot scenarios via
  `tests/harness/textual_scenarios.py::run_screen_scenarios` —
  amortise the Textual lifecycle.
- Run the minimum sufficient subset while iterating
  (`pytest tests/test_specific.py -k name`); always run the full
  suite (`-n auto`) before pushing.

## Adding a config key

1. Extend `DEFAULT_CONFIG`, `Config`, and `load_config` in
   `bin/ccw`.
2. Add validation if the value space is constrained.
3. Add a matching `SettingSpec` in
   `lib/ccw_settings.py::SETTINGS_SPECS` so the TUI Settings screen
   exposes it.
4. Document it in the README config table.
5. Add a `load_config` test in `tests/test_ccw.py` and a
   round-trip test in `tests/test_ccw_settings.py` if the value
   has non-trivial encoding.

## Reporting bugs and proposing features

Please use the issue templates (bug reports include `ccw doctor`
output and the exact command). For security issues, see
[`SECURITY.md`](SECURITY.md).

## License

By contributing, you agree that your contributions are licensed
under the [MIT License](LICENSE).

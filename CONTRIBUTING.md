# Contributing to uxon

Thanks for taking the time. This guide covers everything you need
to make and ship a change. By participating you agree to the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Local setup

`uxon` is a regular Python package (`pyproject.toml`, hatchling backend).
For development, install editable into a venv. Two equivalent paths:

```bash
git clone https://github.com/vzd3v/uxon.git
cd uxon

# A) uv (recommended; faster, shared dep cache)
uv venv
uv pip install -e ".[dev]"
source .venv/bin/activate

# B) plain venv + pip
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e ".[dev]"

uxon --version
```

Both pull `ruff`, `pyright`, `pytest`, `textual`, `tomlkit`
automatically.

`uxon` requires Python 3.11+. The TUI uses `textual >=0.80,<9`; config
writes use `tomlkit`. Both are hard runtime dependencies (pulled in by
`pip install` / `pipx install` / `uv tool install`).

## Local checks

Before opening a PR, run:

```bash
python3 -m py_compile $(git ls-files '*.py')
python3 -m pytest tests/ -n auto
ruff check .
ruff format --check .
pyright
python -m build               # smoke-test the wheel/sdist
twine check dist/*            # README rendering on PyPI
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

## When to bump `__version__`

Bump `__version__` in [`src/uxon/__init__.py`](src/uxon/__init__.py)
in the same commit as any user-visible behaviour change. Hatch reads
the same string at build time, so wheels and the in-tree CLI always
agree. Semver:
- **Major** for breaking changes to the CLI surface, the config
  schema, or the on-disk session-naming scheme.
- **Minor** for new features that don't break existing config or
  session names.
- **Patch** for bug fixes and doc-only changes that affect users.

Then add a matching entry to [`CHANGELOG.md`](CHANGELOG.md) under
`## [Unreleased]`.

## Architectural rules a contributor will hit

These are non-negotiable — see
[`docs/explain/architecture.md`](docs/explain/architecture.md) for the
full picture. Quick list:

- **`textual` is imported lazily inside `do_interactive`.** Non-TUI
  subcommands (`uxon list`, `uxon doctor`, `uxon version`) must run
  without `textual` installed.
- **All key handling goes through `BINDINGS`.** No `on_key` overrides
  on screen classes; a drift guard test refuses the PR otherwise.
- **Config writes use `tomlkit`** — round-trip preserves comments and
  formatting. CLI read paths stay on stdlib `tomllib`.
- **One launch builder.** `uxon` is the single place that builds agent
  command lines (`_build_tmux_launch_request` in `src/uxon/cli.py`).
  Don't add direct agent exec calls anywhere else.
- **Module boundaries.** `src/uxon/cli.py` may import from sibling
  modules in `src/uxon/*`; those modules never import from `cli`. UI
  files under `src/uxon/tui/` must not import `subprocess`/`pwd` or
  touch the filesystem directly — push that through callbacks on
  `TuiContext`.

## Tests

- Prefer pure tests in `src/uxon/tui/state.py` for branchy UI logic.
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
   `src/uxon/cli.py`.
2. Add validation if the value space is constrained.
3. Add a matching `SettingSpec` in
   `src/uxon/settings.py::SETTINGS_SPECS` so the TUI Settings screen
   exposes it.
4. Document it in [`docs/reference/configuration.md`](docs/reference/configuration.md)
   (use case + the reference table).
5. Add a `load_config` test in `tests/test_uxon.py` and a
   round-trip test in `tests/test_uxon_settings.py` if the value
   has non-trivial encoding.

## Reporting bugs and proposing features

Please use the issue templates (bug reports include `uxon doctor`
output and the exact command). For security issues, see
[`SECURITY.md`](SECURITY.md).

## License

By contributing, you agree that your contributions are licensed
under the [MIT License](LICENSE).

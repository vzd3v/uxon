# AGENTS.md — rules for agents working in this repo

These are project-local rules on top of `~/.claude/CLAUDE.md` global rules.
Keep this file tight; don't duplicate README content — link to it instead.

## Scope

`ccw` is a multi-user `tmux` wrapper for `claude` on a shared VPS. User-facing
behavior, commands, flags, TUI, configuration, and rollout docs all live in
[README.md](README.md). Deployment topology notes live in
[docs/deployment.md](docs/deployment.md).

## Code layout

- `bin/ccw` — CLI entrypoint (single Python file, no deps beyond stdlib).
- `lib/ccw_tui.py` — interactive TUI (requires `blessed`; imported lazily).
- `install/` — installer and config renderer.
- `tests/` — `unittest`, discovered via `python3 -m unittest`.
- `config/` — host-local, gitignored. Source of truth for a running host.
- `VERSION` — human-owned release tag.

## Hard rules

- **No `claude` invocations added outside of `launch_in_tmux`.** `ccw` is the
  single place that builds the `claude` command line.
- **No third-party runtime deps in `bin/ccw`.** stdlib only. The TUI file may
  import `blessed`, but only lazily (`ccw list`, `ccw doctor`, etc. must keep
  working without it installed).
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
python3 -m py_compile bin/ccw lib/ccw_tui.py tests/test_ccw.py \
  tests/test_ccw_tui.py install/install_ccw.py install/render_ccw_config.py
python3 -m unittest discover -s tests -p 'test_*.py'
```

CI runs the same two commands. If CI catches something local checks miss,
add a test for it.

## Config

- Two layers: repo-level `config/config.toml` (rendered from JSON) and the
  nearest project-level `.ccw.toml` within an `allowed_roots` entry.
- When adding a config key:
  1. Extend `DEFAULT_CONFIG` + `Config` + `load_config`.
  2. Add validation if the value space is constrained.
  3. Document it in the README config table.
  4. Add a `load_config` test in `tests/test_ccw.py`.

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

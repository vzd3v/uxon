# Use a per-project `.uxon.toml`

Project-level overrides go in a `.uxon.toml` at the project's
root (or any parent inside an `allowed_roots` entry). `uxon`
walks up from `cwd` to find the nearest one and merges it on top
of the host's `config.toml`. Later wins.

The TUI **never writes** `.uxon.toml` — it's a read-only
project-side override.

## When to use

Per-project overrides are useful when one project's needs
differ from the host's defaults. Common cases:

- **Pin a specific agent for this project.** "This codebase has
  cursor-specific tooling; always launch with cursor here."
- **Pin a model.** "All work in this repo runs on
  claude-sonnet-4-6, even if the host default is opus."
- **Repo-specific yolo policy** — for projects where you
  routinely accept the broader blast radius (a sandboxed test
  repo, a doc-only repo).
- **Project-specific dashboard layout.** "This repo runs many
  parallel sessions; show the `path` column."

## Pin a specific agent

```toml
# /srv/projects/alice/cursor-project/.uxon.toml
[agents]
default = "cursor"
```

`uxon run` from inside that directory tree uses cursor as the
default agent without `--agent`.

## Pin model / args per agent

```toml
# .uxon.toml
[agents.claude]
default_args = ["--model", "claude-sonnet-4-6", "--max-tokens", "8192"]

[agents.codex]
default_args = ["--reasoning-effort", "high"]
```

These are prepended to every invocation when launching from
this project tree.

## Project-specific dashboard layout

```toml
# .uxon.toml in a multi-session worktree project
[tui.table]
columns = ["name", "agent", "path", "cpu", "ram", "last"]
default_sort_by = "path"
```

The TUI re-reads `.uxon.toml` when navigating into the project
tree.

## Project-specific allowed_roots? (no)

`allowed_roots` is **host-wide** — `.uxon.toml` cannot widen it.
The point of `allowed_roots` is the operator-set perimeter; per-
project widening would defeat that. If you need a wider
perimeter, edit the host config.

## Discovery

Inside a project subdir:

```bash
uxon doctor
# prints:
#   project config: /srv/projects/alice/myproj/.uxon.toml
```

If `uxon doctor` shows no project config when you have a
`.uxon.toml`, check that the directory is inside an
`allowed_roots` entry. Files outside `allowed_roots` are not
discovered as project config — by design.

## Editing

The TUI's ⚙ Settings screen edits the host `config.toml` only,
not project `.uxon.toml`. Edit project-level files directly.
`tomlkit` round-trip is not enforced for `.uxon.toml` (no TUI
writer); preserving comments is your responsibility.

## Reference

- [`../../reference/configuration.md`](../../reference/configuration.md) — config layers, every key.
- [`../../explain/architecture.md`](../../explain/architecture.md) — config resolution.

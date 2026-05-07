# Sizing a host for a team

`uxon` does not enforce per-user resource limits — agents and
their child processes consume what the host gives them. This page
collects the planning numbers operators have asked for.

For real per-UID enforcement (cgroups slices, `pam_limits`), see
[`guides/harden/apply-resource-limits.md`](../guides/harden/apply-resource-limits.md).

## Rule-of-thumb numbers

For Node / Python / Go-shaped projects without heavy local
services:

- **~2 GB RAM per active agent session** (the agent CLI, its tool
  invocations, and one or two child processes it leaves running).
- **A disciplined developer keeps about 3 sessions open in
  parallel** (one writing a feature, one fixing tests, one
  investigating a bug or doing a refactor).
- **Add headroom for project dev-services** (DBs, watchers, build
  caches), plus 20–30 % for spikes during test runs and builds.

Translating that:

| Team shape | RAM (Node / Python) | Notes |
|---|---|---|
| 1 developer, light services | 10–16 GB | Daily-driver laptop class. |
| 2–3 developers on one host | 32 GB | Comfortable; tighter on monorepos. |
| 3–6 developers on one host | 64 GB | Recommended for a shared dev-server. |
| 6+ developers, or heavy stacks (Java, large Docker, big tests, monorepos) | 128 GB+ or per-developer hosts | Re-do the math against your stack. |

CPU: budget **2–4 vCPU per active developer** for light backend /
web work; more for heavy builds, integration tests, or
container-heavy workflows.

Disk: **NVMe only**. Each developer ends up with several copies of
project trees (worktrees, scratch checkouts), `node_modules` /
`.venv` / Docker layers, agent state, and logs. Start at
**100–200 GB** on a small server; do not economise here.

## What `uxon` itself costs

`uxon` is a thin wrapper. The Python process is short-lived (the
TUI persists, but per-user it sits at ~50–80 MB RSS), agent
binaries are forked once per session, and the rest is `tmux`
overhead — counted in MB, not GB.

The dominant cost is the **agent itself** plus whatever it spawns
(`node_modules` install, `pytest -n auto`, a Docker compose run).
Plan for those, not for `uxon`.

## Multi-host sizing

For team·N, allocate per-host like above and pick a topology that
matches the work shape:

- **One beefy box per team** (single host, team·1) — simplest
  operationally, most expensive RAM-wise.
- **Several smaller boxes, peers aggregated** (team·N) — smaller
  failure domain (one host's runaway doesn't OOM the others), but
  more SSH plumbing and per-host onboarding.
- **One host per developer** — strongest isolation, costs scale
  linearly with the team. The team-N model still applies (the
  lead's laptop aggregates everyone's box).

## Hard limits

If the rough numbers above don't hold (one developer running
multi-GB-model evals locally, another running Postgres + Redis +
Kafka + Elasticsearch), don't try to plan it on paper —
instrument with `htop` / `dstat` for a week and use the actual
shape.

When you do need limits enforced (so one runaway can't OOM the
rest), see
[`guides/harden/apply-resource-limits.md`](../guides/harden/apply-resource-limits.md)
for the per-UID `systemd` slice and `pam_limits` recipes.

## Related

- [`scenarios/team-1.md`](../scenarios/team-1.md) — the scenario hub.
- [`guides/harden/apply-resource-limits.md`](../guides/harden/apply-resource-limits.md) — `MemoryHigh=` / `CPUWeight=` per UID.
- [`guides/operate/respond-to-rogue-agent.md`](../guides/operate/respond-to-rogue-agent.md) — when the planning failed and a session is eating the host alive.

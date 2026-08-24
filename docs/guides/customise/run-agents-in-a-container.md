# Run agents in a container

You want the agent process inside a container (project deps pinned,
a stronger escape boundary than UID separation alone) while keeping
`uxon`'s paired-account model. The two compose — the container runs
*as* `<user>-agent`. There are two ways to wire it; pick one.

> Read [`../../explain/isolation-model.md`](../../explain/isolation-model.md)
> and [`SECURITY.md`](../../../SECURITY.md) first if you have not — a
> container is an isolation *gain*, but it does not relocate the
> credential exposures, and a rootful daemon hands the launch user
> host root. Both recipes below assume **rootless** docker/podman.

## Recipe 1 — a PATH wrapper (no `uxon` config)

The lowest-friction approach needs zero `uxon` changes: put an
executable named like the agent binary (`claude`) early on the
launch user's `PATH` that re-execs into the container. `uxon` launches
the agent through the login shell (`sudo -iu` loads it), so the
wrapper is what gets found.

```bash
# ~/.local/bin/claude  (on the launch user's PATH, ahead of the real binary)
#!/usr/bin/env bash
exec docker exec -i -w "$PWD" my-project-container claude "$@"
```

```bash
chmod +x ~/.local/bin/claude
```

Mechanically: `uxon` builds the same launch command it always does
and runs `claude`; the shell resolves `claude` to this wrapper; the
wrapper hands off to `docker exec` in the already-running container.
`uxon` is unaware of the container — it sees a normal agent process.
Because of that, `uxon` cannot ready a stopped container for you, and —
since `uxon` does not build the exec here — the `stop_command` teardown
cannot apply either, so the agent orphans on kill (the omit-`stop_command`
case under [Teardown](#teardown--reap-the-agent-on-kill)).
Install the wrapper on your own host and run a session to confirm it
resolves before relying on it.

This is the right recipe when you want containerisation for **one**
agent on **one** account without touching shared config.

## Recipe 2 — native container profiles

Container profiles let `uxon` itself wrap selected launch profiles,
resolve the container per (launch user, project), and — when you
permit it — start or create a stopped/absent container. The full key reference
(types, defaults, the trust boundary, the probe-semantics gotcha) is
in
[`../../reference/configuration.md`](../../reference/configuration.md#runtimesid-table);
this guide only shows a working shape.

Define the container with a `compose.yml` (or a devcontainer) rather
than a long `docker run` line — that keeps the container *definition*
in one reviewed file and lets `create_command` stay a one-liner. Keep
that file on an **operator-owned path outside the bind-mounted repo**
and reference it with an explicit `-f`:

```toml
[launch]
enabled_profiles = ["claude_workbox"]
default_profile = "claude_workbox"

[launch.profiles.claude_workbox]
agent = "claude"
runtime = "workbox"

[runtimes.workbox]
kind = "command"
resource_scope = "per_user"
resource_name_template = "uxon-{user}-{launch_profile}-{project_slug}"
exec_prefix = ["docker", "exec", "-w", "{runtime_dir}", "-i", "{resource}"]
telemetry = "cgroup"

[runtimes.workbox.readiness]
ready_command  = ["docker", "top", "{resource}"]
exists_command = ["docker", "container", "inspect", "{resource}"]
on_missing     = "create"          # fail | start | create
approval       = "prompt"          # prompt | auto
start_command  = ["docker", "start", "{resource}"]
create_command = ["docker", "compose", "-f", "/operator/uxon/compose.yml", "up", "-d"]

[runtimes.workbox.identity]
resolve_command = ["docker", "inspect", "--format", '{"id":"{{.Id}}","host_pid":{{.State.Pid}},"epoch":"{{.State.StartedAt}}"}', "{resource}"]

[runtimes.workbox.session]
stop_command = ["docker", "exec", "{resource}", "sh", "-c", "kill $(cat {pidfile}) 2>/dev/null; rm -f {pidfile}"]

[runtimes.workbox.path_map]
"/srv/projects" = "/work"
```

The definition must **not** live inside the bind-mounted repo: a file
the agent can write is a file a yolo or prompt-injected agent can edit
to grant itself host access at the next rebuild. Keep it operator-owned
and outside the mount — the full rationale and the rest of the
lockdown are in
[`../harden/harden-a-container.md`](../harden/harden-a-container.md#the-container-definition-must-not-be-agent-writable).
`ready_command` must exit non-zero unless the container is *running* —
`docker top` does this; `docker inspect` does not (it exits 0 for a
stopped container too), which is why `exists_command` is the inspect call.

### Rootless by default; podman is one string

The commands above target **rootless docker** as written — the CLI
and `docker compose` invocations are byte-for-byte identical to the
rootful ones; only the daemon and socket differ (run as the user,
socket under `$XDG_RUNTIME_DIR`). To use **podman** instead, swap the
binary name in every template (`docker` → `podman`,
`docker compose` → `podman compose`). No other change.

Run rootless. Driving a **rootful** daemon needs docker-group
membership (or rootful-socket access), which is root-equivalent on
the host — a yolo agent in such an account can escalate to host root
via the daemon and defeat the paired-account sandbox. Rootless keeps
the sandbox intact at zero template cost. It is necessary but not
sufficient: a `--privileged`, host-namespace (`--network=host` /
`--pid=host`), broad-bind (`-v /:/host`), or socket-mounting
container *definition* re-grants host access, and `uxon` cannot
prevent that — the `create_command` is opaque to it. Harden the
definition yourself:
[`../harden/harden-a-container.md`](../harden/harden-a-container.md)
covers a hardened template, default-deny egress, file-based secrets,
and the rest.

### The agent need not be installed on the host

With a containerized launch profile, the agent is provisioned
**inside** the container (its image or `create_command`), so `uxon`
does **not** require the agent binary on the host PATH and does
**not** fail the launch when it is absent. Host-only launch profiles
still require the agent binary on the launch user's host PATH. `uxon
doctor` reports a host-absent agent as expected for containerized
profiles, not as a fault.

Project-owned `.uxon.toml` files are not read. Container selection,
path mapping, and all executed runtime templates are operator-owned
config in `config/config.toml`.

## Observability

A container session is **not** a blind spot. `uxon list` and the
dashboard report the agent's real **in-container** CPU and RAM (read
from the container's cgroup, not the near-idle host-side exec client),
the `cmd` column shows the resolved agent id rather than `docker`/`sh`,
and a stopped container renders a distinct `down` indicator instead of
a silent idle `0`/`-`. The lifecycle is audited too —
`runtime.prepare` when `uxon` starts/creates the container and
`runtime.session_stop` when it reaps the agent. See
[`customise-dashboard.md`](customise-dashboard.md) and
[`../../reference/audit-events.md`](../../reference/audit-events.md).

## Teardown — reap the agent on kill

`tmux kill-session` only severs `uxon`'s client-side exec; the
in-container agent does **not** die on that disconnect under docker or
podman — it orphans. An orphaned `--dangerously-skip-permissions` agent
keeps running and consuming resources. (It is still **visible** — `uxon
list` and the dashboard report a container session's real in-container
CPU/RAM, and the kill itself is audited — but a still-running agent
after a kill is a containment failure regardless.) The `stop_command`
above closes this:

- **At launch** `uxon` wraps the agent so it records its in-container
  PID into a per-session pidfile (`{pidfile}`, a path `uxon` supplies).
  One pidfile per session, so a single shared container hosting many
  sessions (indexed re-runs, worktrees, different agents) is handled
  precisely — never a blunt `pkill`.
- **On kill** `uxon` kills the session, then runs `stop_command` to
  terminate exactly that PID. The container itself is left running
  (it is a shared resource; `uxon` never stops or removes it). If the
  container restarted since launch, `uxon` recognises that the recorded
  PID is no longer the agent and **skips** the stop command (audited as
  `outcome=error reason=stale_identity`) rather than killing an unrelated process.

Teardown is **best-effort**: if it fails (no `sh` in the image, daemon
unreachable, …) `uxon` prints a note and the kill still completes. If
you **omit** `stop_command`, the agent orphans as before and `uxon`
appends a reminder at every kill; in that case treat the container path
as requiring an explicit "also stop the container" step when responding
to a rogue agent — see
[`../operate/respond-to-rogue-agent.md`](../operate/respond-to-rogue-agent.md#container-path-also-stop-the-container).

## Reference

- [`../../reference/configuration.md`](../../reference/configuration.md#runtimesid-table)
  — `[runtimes.<id>]`: every key, the trust boundary, validation.
- [`../../explain/isolation-model.md`](../../explain/isolation-model.md)
  — how the container layer composes with the paired account.
- [`SECURITY.md`](../../../SECURITY.md) — rootful-vs-rootless,
  secrets-not-relocated, the operator caveat on container definitions.

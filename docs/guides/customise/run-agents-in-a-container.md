# Run agents in a container

You want the agent process inside a container (project deps pinned,
a stronger escape boundary than UID separation alone) while keeping
`uxon`'s paired-account model. The two compose — the container runs
*as* `<user>_agent`. There are two ways to wire it; pick one.

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
Because of that, `uxon` cannot ready a stopped container for you, and
the kill caveat below applies in full. Install it on your own host
and run a session to confirm the wrapper resolves before relying on
it.

This is the right recipe when you want containerisation for **one**
agent on **one** account without touching shared config.

## Recipe 2 — native `[container]` support

The `[container]` block lets `uxon` itself wrap the launch, resolve
the container per (launch user, project), and — when you permit it —
start or create a stopped/absent container. The full key reference
(types, defaults, the trust boundary, the probe-semantics gotcha) is
in
[`../../reference/configuration.md`](../../reference/configuration.md#container-table);
this guide only shows a working shape.

Define the container with a `compose.yml` next to the project (or a
devcontainer) rather than a long `docker run` line — that keeps the
container *definition* in one reviewed file and lets `create_template`
stay a one-liner:

```toml
[container]
enabled         = true
exec_template   = ["docker", "exec", "-w", "{dir}", "-i", "{name}"]
name_template   = "uxon-{user}-{project_slug}"
is_running_cmd  = ["docker", "top", "{name}"]
exists_cmd      = ["docker", "container", "inspect", "{name}"]
on_missing      = "create"          # off | start | create
on_missing_mode = "prompt"          # prompt | auto
start_template  = ["docker", "start", "{name}"]
create_template = ["docker", "compose", "up", "-d"]

[container.path_map]
"/srv/projects" = "/work"
```

`create_template` runs in the **host** project directory, so a
`compose.yml` there is found without an explicit `-f`. `is_running_cmd`
must exit non-zero unless the container is *running* — `docker top`
does this; `docker inspect` does not (it exits 0 for a stopped
container too), which is why `exists_cmd` is the inspect call.

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
prevent that — the `create_template` is opaque to it. Harden the
definition yourself.

### A project `.uxon.toml` may only name the container

From an untrusted project `.uxon.toml`, only `container.name` and
`container.path_map` are honoured — every executed or policy key
(`exec_template`, `create_template`, `on_missing`, …) stays
operator-only. The boundary and the validation rules are documented
in
[`../../reference/configuration.md`](../../reference/configuration.md#container-table).

## Kill caveat (both recipes)

`uxon` kills a session with `tmux kill-session`, which reaps the
client-side exec process. **Whether the in-container agent stops on
disconnect is runtime-dependent and has not been verified for this
release.** A runtime that orphans the in-container process leaves a
`--dangerously-skip-permissions` agent running, invisible to
`uxon list` / the TUI / the audit trail — a containment failure. Until
you have confirmed reaping for your own runtime, treat the container
path as requiring an explicit "also stop the container" step when
responding to a rogue agent — see
[`../operate/respond-to-rogue-agent.md`](../operate/respond-to-rogue-agent.md#container-path-also-stop-the-container).

## Reference

- [`../../reference/configuration.md`](../../reference/configuration.md#container-table)
  — the `[container]` table: every key, the trust boundary, validation.
- [`../../explain/isolation-model.md`](../../explain/isolation-model.md)
  — how the container layer composes with the paired account.
- [`SECURITY.md`](../../../SECURITY.md) — rootful-vs-rootless,
  secrets-not-relocated, the operator caveat on container definitions.

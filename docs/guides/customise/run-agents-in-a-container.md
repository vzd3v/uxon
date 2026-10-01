# Run agents in a container

You want the agent process inside a container (project deps pinned,
a stronger escape boundary than UID separation alone) while keeping
`uxon`'s paired-account model. The two compose — the container runs
*as* `<user>-agent`. There are two ways to wire it; pick one.

> Read [`../../explain/isolation-model.md`](../../explain/isolation-model.md)
> and [`SECURITY.md`](../../../SECURITY.md) first if you have not — a
> container is an isolation *gain*, but it does not relocate the
> credential exposures, and a rootful daemon hands the launch user
> host root. Both recipes below require a **rootless** runtime.

## Recipe 1 — a PATH wrapper (no `uxon` config)

The lowest-friction approach needs zero `uxon` changes: put an
executable named like the agent binary (`claude`) early on the
launch user's `PATH` that re-execs into the container. `uxon` launches
the agent with the execution backend's inherited `PATH`; it does not source a
login shell. Either make that PATH explicit in the backend or configure the
wrapper's absolute path as `agents.claude.binary`.

```bash
# /opt/uxon/wrappers/claude
#!/usr/bin/env bash
exec docker exec -i -w "$PWD" my-project-container claude "$@"
```

```bash
chmod +x /opt/uxon/wrappers/claude
```

Mechanically: `uxon` builds the same launch command it always does
and runs the configured binary; the execution environment resolves it to this wrapper; the
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

## Recipe 2 — a command workload runtime

A command runtime lets `uxon` itself wrap selected launch profiles,
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
and reference it with an explicit `-f`.

### Prepare the image and definition

This recipe uses rootless Docker and Compose v2 running as the launch user,
with host `sha256sum` and `cut` from coreutils.
The image must contain the selected agent, `sh`, `cat`, and Python 3.11+ with
Linux `pidfd_open` / `pidfd_send_signal` support. Provision credentials inside
the runtime; Uxon does not copy host credentials into it.

From the matching Uxon source checkout, install the reviewed
[workload stopper](../../../install/runtime_stop.py) on an operator-owned path,
outside every project mount:

```bash
sudo install -d -o root -g root -m 0755 /operator/uxon
sudo install -o root -g root -m 0644 install/runtime_stop.py \
  /operator/uxon/runtime_stop.py
```

Write `/operator/uxon/compose.yml` as root, with mode `0644`. Replace the image
with your own pinned, provisioned image:

```yaml
services:
  agent:
    image: registry.example/uxon-agent@sha256:<digest>
    container_name: ${UXON_RESOURCE:?set UXON_RESOURCE}
    user: "0:0"
    init: true
    read_only: true
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    environment:
      HOME: /tmp/uxon-home
    tmpfs:
      - /tmp:size=512m,mode=1777
    volumes:
      - type: bind
        source: ${UXON_PROJECT:?set UXON_PROJECT}
        target: ${UXON_RUNTIME_DIR:?set UXON_RUNTIME_DIR}
        bind:
          create_host_path: false
      - /operator/uxon/runtime_stop.py:/usr/local/libexec/uxon-runtime-stop.py:ro
    working_dir: ${UXON_RUNTIME_DIR:?set UXON_RUNTIME_DIR}
    command: [sh, -c, 'mkdir -p "$$HOME" && exec sleep infinity']
```

Container UID 0 maps to the rootless daemon owner's host UID, not host root.
The launch user must own that daemon and be able to write the project. See
[UID mapping](../harden/harden-a-container.md#fix-the-rootless-uid-mapping-footgun).
Image layers, the definition and the stopper remain read-only to the workload.

### Configure the launch profile

Install this complete TOML in `/etc/uxon/config.toml`, or merge its tables into
your existing operator configuration:

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
create_command = ["sh", "-c", 'project=uxon-$(printf %s "$1" | sha256sum | cut -c 1-20); export UXON_RESOURCE="$1" UXON_RUNTIME_DIR="$2" UXON_PROJECT="$PWD"; exec docker compose -p "$project" -f /operator/uxon/compose.yml up -d', "uxon-create", "{resource}", "{runtime_dir}"]

[runtimes.workbox.identity]
resolve_command = ["docker", "inspect", "--format", '{{"id":"{{{{.Id}}}}","host_pid":{{{{.State.Pid}}}},"epoch":"{{{{.State.StartedAt}}}}"}}', "{resource}"]

[runtimes.workbox.session]
stop_command = ["docker", "exec", "{resource}", "python3", "/usr/local/libexec/uxon-runtime-stop.py", "{pidfile}", "--timeout", "8"]

[runtimes.workbox.timeouts]
stop_seconds = 10.0

[runtimes.workbox.path_map]
"/srv/projects" = "/work"
```

Preparation runs in the selected host project directory. The create command
passes that directory, its mapped runtime path and the resolved resource name
to Compose. For `/srv/projects/nadia/repo`, the bind target and exec working
directory are both `/work/nadia/repo`; `container_name` matches every probe.
The Compose project name is derived stably from the resource's hash, satisfying
Compose's narrower name grammar even when the container name has uppercase
letters or dots. It separates independently created resources. Use unique
project basenames per launch user/profile with this naming template, or choose
an explicit profile-specific resource name for colliding basenames.

Literal braces must be escaped for Uxon's template renderer. The identity
argument above renders to Docker's JSON/Go template:

```text
{"id":"{{.Id}}","host_pid":{{.State.Pid}},"epoch":"{{.State.StartedAt}}"}
```

The stopper's wait budget is below `stop_seconds`, leaving time for Docker exec
and result delivery. It refuses missing, malformed or reused-PID records,
signals through a stable pidfd, and removes the record only after confirmed
termination. A workload that ignores TERM produces a failure, not a success.

The definition must **not** live inside the bind-mounted repo: a file
the agent can write is a file a yolo or prompt-injected agent can edit
to grant itself host access at the next rebuild. Keep it operator-owned
and outside the mount — the full rationale and the rest of the
lockdown are in
[`../harden/harden-a-container.md`](../harden/harden-a-container.md#the-container-definition-must-not-be-agent-writable).
`ready_command` must exit non-zero unless the container is *running* —
`docker top` does this; `docker inspect` does not (it exits 0 for a
stopped container too), which is why `exists_command` is the inspect call.

### Verify the rootless setup

The commands above target **rootless docker** as written — the CLI
and `docker compose` invocations are byte-for-byte identical to the
rootful ones; only the daemon and socket differ (run as the user,
socket under `$XDG_RUNTIME_DIR`). Verify the context before launching:

```bash
docker info --format '{{json .SecurityOptions}}'  # must include rootless
# Validate Compose without creating a container:
UXON_RESOURCE=uxon-example UXON_PROJECT=/srv/projects/nadia/repo \
  UXON_RUNTIME_DIR=/work/nadia/repo \
  docker compose -f /operator/uxon/compose.yml config --quiet
```

From the project directory, launch the profile, check `uxon list`, then kill
the session and verify the workload exited while the container remains running.
Also verify ownership of a file written through the bind mount before relying
on this setup. See the [hardening checks](../harden/harden-a-container.md).

Podman requires its own reviewed runtime adapter, Compose provider and UID
mapping. Its [`keep-id` mapping](https://docs.podman.io/en/latest/markdown/podman-run.1.html#userns-mode)
is different from Docker rootless; changing binary names alone is insufficient.

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
config in `/etc/uxon/config.toml`.

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
  PID and Linux process start ticks into a nonce-keyed pidfile (`{pidfile}`, a
  path `uxon` supplies). Each launch has a globally unique nonce, so a shared container hosting
  sessions (indexed re-runs, worktrees, different agents) is handled
  precisely — never a blunt `pkill`.
- **On kill** `uxon` kills the session, then runs `stop_command` to
  verify the saved process identity and terminate it. The container itself is left running
  (it is a shared resource; `uxon` never stops or removes it). If the
  container restarted since launch, `uxon` recognises that the recorded
  PID is no longer the agent and **skips** the stop command (audited as
  `outcome=skipped reason=stale_identity`) rather than killing an unrelated process.

Teardown is **best-effort**: if it fails (missing helper, daemon
unreachable, …), Uxon reports the failure and does not count the operation as
successful workload cleanup. The tmux session may already be gone. If
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

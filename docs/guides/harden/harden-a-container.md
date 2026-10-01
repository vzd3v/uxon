# Harden a container

You have a working command-runtime container setup
([`run-agents-in-a-container.md`](../customise/run-agents-in-a-container.md))
and want to tighten it. The minimal recipe there gets the agent
running inside a container; it does **not** lock it down. This page is
the hardening reference — a hardened template plus the operator-facing
runtime config that turns a rootless container into real
defense-in-depth.

> **What this buys you, honestly.** A hardened rootless container is
> defense-in-depth that makes a yolo agent on a *trusted* repo
> tolerable — it is **not** a guarantee against a malicious repo or a
> prompt-injected agent. For genuinely untrusted code you need a
> stronger boundary than a shared kernel (gVisor / Kata / a microVM);
> see [Don't weaken the defaults](#dont-weaken-the-defaults) and
> [`../../explain/isolation-model.md`](../../explain/isolation-model.md).

Everything below is **operator-facing runtime config**, not `uxon`
behaviour — `uxon` executes the configured adapter and stays runtime-agnostic.
This page continues the rootless Docker recipe. Review Podman's UID mapping
and Compose provider separately before adapting it.

## The container definition must not be agent-writable

**This is the one that turns every other setting into theatre if you
get it wrong.** Keep the container *definition* — the
`compose.yml` / `Dockerfile` / `.devcontainer` that `create_command`
builds from — on an **operator-owned path outside the bind-mounted
repo**, and reference it with an explicit `-f`:

```text
/operator/uxon/compose.yml      # operator-owned, root:root, 0644 — outside any mount
/srv/projects/<repo>/           # bind-mounted into the container; agent-writable
```

Use the explicit create command in the [setup recipe](../customise/run-agents-in-a-container.md#configure-the-launch-profile).
A definition inside the bind mount would let the workload rewrite it.
A yolo or prompt-injected agent that can rewrite the
definition can add `-v /:/host`, `--privileged`, a socket mount, or a
devcontainer `initializeCommand` / `onCreateCommand` — and those
**run on the host** at the next rebuild. That is a full host escape,
and `uxon` cannot prevent it because the template is opaque to it. The
operator owns the definition; the agent never touches it.

`uxon doctor` flags the common case where the definition path resolves
under a `path_map` host prefix (i.e. inside the mount), but treat that
as a backstop, not your guarantee — verify the path yourself.

## A hardened run template

Keep the setup recipe's resource and mount wiring, then add resource limits:

```yaml
# /operator/uxon/compose.yml — operator-owned, outside the repo mount
services:
  agent:
    image: registry.example/uxon-agent@sha256:<digest>   # pin by digest, not :latest
    container_name: ${UXON_RESOURCE:?set UXON_RESOURCE}
    user: "0:0"                     # rootless daemon owner's host UID
    init: true                       # PID 1 reaps zombies (docker run: --init)
    read_only: true                  # read-only root filesystem
    cap_drop: [ALL]                  # drop every Linux capability
    security_opt:
      - no-new-privileges:true       # no setuid escalation
    pids_limit: 512                  # cap process count (fork-bomb guard)
    mem_limit: 8g
    cpus: "2.0"
    environment:
      HOME: /tmp/uxon-home
    tmpfs:
      - /tmp:size=512m,mode=1777     # writable scratch on a read-only root
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

The `docker run` equivalent of the security flags, for reference:

```bash
docker run --cap-drop=ALL --security-opt=no-new-privileges \
  --pids-limit=512 --memory=8g --cpus=2 --init --read-only \
  --tmpfs /tmp:size=512m ...
```

**Never** do the opposite of these on a container that runs a yolo
agent:

- **Never mount the runtime socket** (`/var/run/docker.sock`,
  the Podman socket). A rootful socket grants host-root authority; a rootless
  socket grants the daemon owner's authority over host files and containers.
- **Never `--privileged`.** It disables almost every isolation
  control at once.
- **Never share host namespaces** — `--network=host`, `--pid=host`,
  `--ipc=host` all dissolve the boundary you are paying for.

## Default-deny egress

The template above does not implement an egress allowlist. Enforce one through
operator-controlled networking or a logging proxy outside the workload; allow
only the model API, package registry and git remote it needs. An offline
workload can use `network_mode: none`. Do not grant the workload `NET_ADMIN`
to run its own firewall: the template drops that capability, and a workload
able to rewrite its firewall can bypass it.

Block the **cloud metadata endpoint** explicitly — `169.254.169.254`
and the link-local `169.254.0.0/16` range. An agent that can reach it
can read the host's instance-IAM credentials (an SSRF straight to your
cloud account). Apply this denial in the external network policy, ahead of
allow rules, and deny direct routes that bypass the proxy.

**Check it.** From inside the container, the metadata endpoint must
fail:

```bash
docker exec <name> curl -sS --max-time 3 http://169.254.169.254/  # must time out / fail
```

Also isolate the container from the host's *other* services and from
other tenants — a flat bridge network lets the agent reach a database
or a neighbour's container. Put it on its own network.

> **Residual risk: DNS exfiltration.** A domain allowlist still lets
> the agent smuggle data out through DNS queries (`secret.attacker.com`
> lookups) even with all other egress dropped. A plain `iptables` drop
> is also **silent** — you see nothing when the agent *tries* to reach
> a blocked host. The stronger tier is a **logging egress proxy**
> (the agent's only route out): it gives you visibility into attempted
> exfil and can police DNS, which a blind drop cannot. Name this as the
> upgrade when the threat model warrants it.

## Fix the rootless UID-mapping footgun

Docker and Podman have different rootless mapping options:

- **Docker rootless:** container UID/GID 0 maps to the daemon owner's host
  UID/GID. Positive container IDs map to subordinate IDs. This recipe runs
  as `0:0` inside the namespace with capabilities dropped; that is not host
  root. Passing the host numeric UID to `--user` does not preserve its host
  ownership. See [Docker UID/GID mapping](https://docs.docker.com/engine/security/rootless/uid-gid-mapping/).
- **Podman rootless:** `--userns=keep-id` maps the invoking user's identity to
  the same container UID/GID and selects that user unless explicitly
  overridden. Review the image and adapter for that identity; see
  [Podman user namespaces](https://docs.podman.io/en/latest/markdown/podman-run.1.html#userns-mode).

```bash
# Podman only; review the remaining runtime flags separately:
podman run --userns=keep-id ...
```

**Check it.** Have the agent write a file in the repo, then confirm
the launch user owns it and can delete it without `sudo`:

```bash
docker exec -w /work/nadia/repo <name> touch .uxon-ownership-probe
ls -ln /srv/projects/nadia/repo/.uxon-ownership-probe  # launch user's host UID
rm /srv/projects/nadia/repo/.uxon-ownership-probe     # as launch user, without sudo
```

## File-based secrets

Pass credentials as **files**, never baked into the image and never on
the argv:

- Mount secrets at `/run/secrets/<name>` (compose `secrets:` /
  `--mount=type=secret`), not via `-e`. Env vars leak into
  `/proc/<pid>/environ`, audit logs, and crash dumps; **env-files**
  (`--env-file`) have the same exposure plus the file lingers.
- **Never** put a secret in an image layer or in argv — both are
  visible to anyone who can read the image or list processes.
- Prefer **short-lived, repo-scoped** tokens (a git token scoped to the
  one repo, expiring in hours) over long-lived broad credentials. This
  ties to the provisioning principle: the operator provisions auth
  *into* the container; `uxon` does not forward host credentials for
  you. See [Provision auth safely](#provision-auth-safely).

## Pin and provenance the image

- **Pin by digest**, not `:latest` — `image@sha256:<digest>`. A
  floating tag means a rebuild can pull a changed (or compromised)
  image under you. Pin the agent CLI version too.
- On a **shared host**, disable the agent's in-container
  **auto-update**: an update channel is an unreviewed code path into a
  container many developers share.
- **Scan** the image and keep an **SBOM** so you know what shipped.
- **Never** put secrets in `ARG` or `ENV` — both are baked into the
  image layers forever and survive `docker history`. For build-time
  secrets use BuildKit `--mount=type=secret`, which never lands in a
  layer.

## Cap resource exhaustion beyond CPU/RAM

The CPU/RAM caps in the template are not the whole story:

- **Disk / volume quotas.** An agent can fill the host disk through a
  writable volume. Quota the volume (or back it with a sized
  filesystem) so it can't.
- **File descriptors:** `--ulimit nofile=<soft>:<hard>` — an fd leak
  otherwise climbs until it starves the host.
- **The inotify / fd cross-tenant hazard.** Under rootless containers
  sharing one backing UID, one tenant exhausting
  `fs.inotify.max_user_instances` / `max_user_watches` (file watchers
  are per-*UID*, kernel-wide) can block every other tenant's watches.
  Give each launch user a separate rootless daemon and non-overlapping
  subuid/subgid ranges. Container UID 0 still counts against that daemon
  owner's UID; adding more containers under one daemon does not separate it.

This composes with the OS-level per-UID limits in
[`apply-resource-limits.md`](apply-resource-limits.md) — the container
caps the agent's subtree; the slice caps the launch user as a whole.

## Provision auth safely

The default is **short-lived, narrowly-scoped** credentials provisioned
*into* the container by the operator:

- **Never** bake keys into image layers, **never** `ARG`/`ENV` them at
  build time, and **never** bind-mount the host's `~/.ssh`, `~/.aws`,
  `~/.config/gh`, or other cloud-cred files into a yolo container — a
  mount is reachable inside exactly as it is on the host.
- Prefer `/run/secrets`, a **repo-scoped git token**, or
  workload-identity / OIDC where the agent talks to a cloud model.

This is the same principle as [File-based secrets](#file-based-secrets)
— credential passthrough is the operator's job, done with the
shortest-lived, narrowest credential that works.

## Don't weaken the defaults

The runtime ships sane defaults — **keep them**:

- Leave the default **seccomp** profile on. Never
  `--security-opt seccomp=unconfined` for a yolo agent — it removes the
  syscall filter that blocks whole classes of kernel-attack surface.
- Leave **AppArmor / SELinux** on (don't run `--security-opt
  apparmor=unconfined` / `label=disable`). They are a free second layer.

These defaults plus everything above are **defense-in-depth on a
shared kernel** — strong for a trusted repo, not a guarantee against
genuinely untrusted code. For that, the boundary you want is a
**stronger-than-shared-kernel sandbox** — gVisor, Kata Containers, or a
microVM (Firecracker). That tier is **out of `uxon`'s scope** (it is
your runtime choice), but it is the honest answer when the code itself
is the adversary.

## Related

- [`../customise/run-agents-in-a-container.md`](../customise/run-agents-in-a-container.md)
  — the working-setup guide this hardens.
- [`apply-resource-limits.md`](apply-resource-limits.md) — per-UID
  OS-level limits the container caps compose with.
- [`../../explain/isolation-model.md`](../../explain/isolation-model.md)
  — how the container layer composes with the paired account, and what
  it does and does not buy you.
- [`../../reference/configuration.md`](../../reference/configuration.md#runtimesid-table)
  — `[runtimes.<id>]`: every key, the trust boundary, validation.
- [`../operate/respond-to-rogue-agent.md`](../operate/respond-to-rogue-agent.md#container-path-also-stop-the-container)
  — reaping a rogue agent on the container path.

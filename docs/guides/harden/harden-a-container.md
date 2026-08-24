# Harden a container

You have a working container-profile setup
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
behaviour — `uxon` only execs the prefix you give it and stays
runtime-agnostic. The `docker` / `podman` commands are examples of
what an operator puts in the `create_command` / image; swap the
binary name for your runtime (`docker` → `podman`, `docker compose`
→ `podman compose`).

## The container definition must not be agent-writable

**This is the one that turns every other setting into theatre if you
get it wrong.** Keep the container *definition* — the
`compose.yml` / `Dockerfile` / `.devcontainer` that `create_command`
builds from — on an **operator-owned path outside the bind-mounted
repo**, and reference it with an explicit `-f`:

```toml
[runtimes.workbox.readiness]
create_command = ["docker", "compose", "-f", "/operator/uxon/compose.yml", "up", "-d"]
```

```text
/operator/uxon/compose.yml      # operator-owned, root:root, 0644 — outside any mount
/srv/projects/<repo>/           # bind-mounted into the container; agent-writable
```

The minimal guide's `compose.yml`-next-to-the-project shape is a
footgun: that file lives **inside** the bind mount, so the agent can
edit it. A yolo or prompt-injected agent that can rewrite the
definition can add `-v /:/host`, `--privileged`, a socket mount, or a
devcontainer `initializeCommand` / `onCreateCommand` — and those
**run on the host** at the next rebuild. That is a full host escape,
and `uxon` cannot prevent it because the template is opaque to it. The
operator owns the definition; the agent never touches it.

`uxon doctor` flags the common case where the definition path resolves
under a `path_map` host prefix (i.e. inside the mount), but treat that
as a backstop, not your guarantee — verify the path yourself.

## A hardened run template

Start from drop-everything and add back only what the agent needs:

```yaml
# /operator/uxon/compose.yml — operator-owned, outside the repo mount
services:
  agent:
    image: registry.example/uxon-agent@sha256:<digest>   # pin by digest, not :latest
    user: "1000:1000"                # non-root inside the container
    init: true                       # PID 1 reaps zombies (docker run: --init)
    read_only: true                  # read-only root filesystem
    cap_drop: [ALL]                  # drop every Linux capability
    security_opt:
      - no-new-privileges:true       # no setuid escalation
    pids_limit: 512                  # cap process count (fork-bomb guard)
    mem_limit: 8g
    cpus: "2.0"
    tmpfs:
      - /tmp:size=512m,mode=1777     # writable scratch on a read-only root
    volumes:
      - /srv/projects/<repo>:/work   # the repo, the only writable bind
    working_dir: /work
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
  the podman socket) — socket access is host-root-equivalent.
- **Never `--privileged`.** It disables almost every isolation
  control at once.
- **Never share host namespaces** — `--network=host`, `--pid=host`,
  `--ipc=host` all dissolve the boundary you are paying for.

## Default-deny egress

Give the container **no** outbound network by default and allow back
only the few hosts the agent legitimately needs (the model API, your
package registry, your git remote). The Anthropic `init-firewall`
pattern is a good template: bring the firewall up as the container's
first action, resolve the allowlisted domains, and drop everything
else.

Block the **cloud metadata endpoint** explicitly — `169.254.169.254`
and the link-local `169.254.0.0/16` range. An agent that can reach it
can read the host's instance-IAM credentials (an SSRF straight to your
cloud account):

```bash
# inside the container's firewall init, before any agent work — and
# ahead of any allowlist ACCEPT rules (iptables is first-match).
iptables -A OUTPUT -d 169.254.169.254 -j DROP   # the IMDS IP, kept explicit
iptables -A OUTPUT -d 169.254.0.0/16   -j DROP   # the whole link-local range
```

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

Under rootless docker/podman, files the agent writes in the bind mount
land owned by a **mapped** UID, not the launch user — so the developer
often cannot read back or delete what the agent created without
`sudo`. Map the container user to the host user:

```bash
# podman: map the container's user to the host launch user
podman run --userns=keep-id ...
# docker rootless: run as the host UID:GID directly
docker run --user "$(id -u):$(id -g)" ...
```

**Check it.** Have the agent write a file in the repo, then confirm
the launch user owns it and can delete it without `sudo`:

```bash
docker exec <name> sh -c 'touch /work/.uxon-ownership-probe'
ls -ln /srv/projects/<repo>/.uxon-ownership-probe   # UID should be the launch user's
rm /srv/projects/<repo>/.uxon-ownership-probe        # must succeed without sudo
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
  Give each user a **non-overlapping subuid/subgid range** so the
  kernel accounts them separately.

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

# Review a workload container's boundary

Start from the canonical [container setup](../customise/run-agents-in-a-container.md).
Uxon invokes the adapter; the runtime owner controls isolation and resource policy.

## Protect the definition and control interface

Keep the Compose definition, image build inputs and stop adapter on
operator-owned paths outside every workload-writable project mount. A workload
that can rewrite its next launch definition can request broader access.

Do not expose a rootful runtime socket or grant the launch account rootful
daemon access. A rootless socket still grants the daemon owner's authority over
files and containers. Neither socket belongs inside the workload.

Review mounts, namespaces and privileges before enabling creation. Uxon treats
runtime argv templates as trusted configuration, not as sandbox policy it can verify.

## Verify identity and lifecycle

Use the rootless Docker mapping and ownership checks in the
[setup guide](../customise/run-agents-in-a-container.md#verify-the-rootless-setup).
A host UID passed as a container UID does not generally preserve ownership.
Podman requires its own reviewed mapping and adapter.

Verify that killing one disposable session stops only its workload, preserves
other sessions, and reports cleanup failures. The protected launch record and
`session.stop_command` are necessary for managed teardown.

## Provision the workload

Install the coding tool and credentials inside the workload through your normal
provisioning system. Uxon does not forward host credentials. Keep credentials
out of images and command arguments; expose only required files and services.

Set resource and network limits in the runtime or execution boundary where the
process actually runs. Changing a Linux UID alone does not place a process in
a particular resource-control group.

The recipe is a starting integration, not a guarantee against untrusted code.
Follow the runtime's security documentation, such as
[Docker rootless](https://docs.docker.com/engine/security/rootless/), and
[Uxon's threat model](../../../SECURITY.md).

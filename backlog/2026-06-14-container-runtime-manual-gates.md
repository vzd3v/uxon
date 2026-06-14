where: tests/integration/container/

why: Two container behaviours can only be confirmed against a real
docker/podman daemon, which the development environment that landed the
harness did not have reachable — so they remain unobserved here.

done when: the `pytest -m container` suite has been run once on a host
with a working rootless docker AND once with podman, and both observed to
pass (or a podman limitation documented with its mitigation).

## What is unobserved

The harness under `tests/integration/container/` encodes both checks, but
verifying them requires a reachable runtime:

1. **In-container exec.** Launching with `[container]` enabled runs the
   agent command *inside* the container — proven by the marker file the
   bind-mounted stub touches on the shared volume.

2. **Kill-reaping.** After `tmux kill-session`, the in-container agent
   process is gone (`docker top` / `podman top` shows nothing). A kill
   that orphaned the agent would be a security regression, not just a
   reliability issue — so this must be confirmed per runtime.

## Procedure (per runtime)

On a host with a working rootless docker or podman daemon:

```
pip install -e ".[dev]"
pytest -m container -rs
```

The suite parametrizes over docker and podman and self-skips whichever
runtime is absent or has an unreachable daemon, so run it once on a
docker host and once on a podman host (or one host with both). A skip is
not a pass — both must report `passed` for the gate to close. If podman
orphans the agent on kill, capture the configuration and the mitigation
(signal-forwarding init / `--sig-proxy`) and mark that configuration
unsuitable for the rogue-agent workflow.

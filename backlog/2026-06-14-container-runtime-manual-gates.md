where: tests/integration/container/

why: Two container behaviours can only be confirmed against a per-user
container daemon. The only runtime reachable where the harness landed was
a rootful production daemon hosting unrelated live containers, which was
deliberately not exercised; no rootless docker or podman was available. So
both checks remain unobserved — deferred by decision, not by missing code.

done when: the `pytest -m container` suite has been run on a box with a
rootless docker daemon AND on one with podman (the recommended posture for
this workflow — not a shared rootful production daemon), and both observed
to pass (or a podman limitation documented with its mitigation).

## What is unobserved

The harness under `tests/integration/container/` encodes both checks, but
verifying them requires a reachable per-user runtime:

1. **In-container exec.** Launching with `[container]` enabled runs the
   agent command *inside* the container — proven by the marker file the
   bind-mounted stub touches on the shared volume.

2. **Kill-reaping.** After `tmux kill-session`, the in-container agent
   process is gone (`docker top` / `podman top` shows nothing). A kill
   that orphaned the agent would be a security regression, not just a
   reliability issue — so this must be confirmed per runtime.

## Procedure (per runtime)

On a host with a working rootless docker or podman daemon (a per-user
daemon, not a shared rootful production one):

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

# Concurrency & direct control

The `Sandbox` facade runs ONE program per `run()`. To launch MANY sandboxes, or to reach knobs the facade never exposes (docker-in-docker, a disk-backed image graph, per-launch egress on the spec), drop to the building blocks the facade is built on — `SandboxLaunchSpec` and `drive_node`, plus the `Admission` seam for bounding how many run at once. All are exported from `resoluto.sandbox`.

The snippet assumes a `runtime` (a `SandboxRuntime` — e.g. `KataNerdctlSandboxRuntime.from_env(...)` for local or `K8sSandboxRuntime(...)` for k8s) and a `conduit` (a `Conduit` — e.g. `store_from_env()`), wired exactly as the [`local`](https://deepbluecoding.github.io/resoluto-sandbox/0.1.0/backends/#local) / [`k8s`](https://deepbluecoding.github.io/resoluto-sandbox/0.1.0/backends/#k8s) backends build them.

## Bounded concurrency — the `Admission` seam

The sandbox does not pool or schedule; it is a dumb executor. Concurrency limits belong to the CALLER, and the substrate defines exactly one seam for them: [`Admission`](https://deepbluecoding.github.io/resoluto-sandbox/0.1.0/api/pool/index.md) — a protocol whose `acquire(spec)` decides whether/when a launch is allowed and returns a [`Lease`](https://deepbluecoding.github.io/resoluto-sandbox/0.1.0/api/pool/index.md) (the granted slot; closing it destroys the sandbox). Hand any implementation to `drive_node(..., admit=...)` and the drive parks until admitted; `admit=None` launches immediately.

Orchestrators bring their own admitter (a FIFO slot/RAM-budget pool, a cluster quota, a queue) — resoluto-engine's `SandboxPool` is one such implementation, living where scheduling policy belongs.

> An admitter bounds SUBSTRATE admission (how many sandboxes exist at once), NOT workload liveness. A slow-but-alive sandbox holds its slot as long as it keeps emitting — there is no wall-clock cap.

## Advanced: the direct `drive_node` path

`Sandbox` / `SubstrateBackend` always launch a single `flavor="plain"` step. Three capabilities live ONLY on the raw `SandboxLaunchSpec` + `drive_node` path and are **NOT reachable through the facade**:

- **`flavor="dind"` + `privileged=True`** — docker-in-docker inside the Kata guest (privileged is guest-scoped under Kata, not a host escape).
- **`graph_backend="block"` + `dind_graph=...`** — put the inner docker image graph on a disk-backed volume instead of tmpfs, so image layers stay off RAM. `graph_backend` is ignored by non-dind steps.
- **`egress_allow` / `egress_public_https` ON THE SPEC** — per-launch egress the `k8s` runtime renders into the pod's NetworkPolicy. Distinct from `Sandbox.run(egress=...)` (the `local` SNI-proxy path) and from a runtime-level `EgressConfig`.

```python
import asyncio
from resoluto.sandbox.contracts import Resources, SandboxLaunchSpec
from resoluto.sandbox.driver import drive_node

spec = SandboxLaunchSpec(
    image="<registry>/resoluto-sandbox-base:0.1.0",
    flavor="dind", privileged=True,                       # docker-in-docker under Kata
    store_prefix="run/demo/nodes/build/sandbox-0",
    resources=Resources.from_quantities(
        memory="8Gi", cpu="4",
        dind_graph="30Gi", graph_backend="block",         # image graph on a 30 GiB disk-backed volume, not RAM
    ),
    egress_allow=["registry.npmjs.org"],                  # this launch reaches only npm (+ DNS + store)
    egress_public_https=False,
    args=["python", "-m", "resoluto.sandbox.runner_main"],
)
result = asyncio.run(drive_node(runtime, conduit, spec))  # NodeResult; tails chunks, reaps the sandbox
```

`drive_node(runtime, store, spec, *, admit=None, on_event=None, dead_after_s=120.0)` launches, tails the Conduit chunks, and returns a `NodeResult`. Pass `admit=` any `Admission` implementation to bound concurrency; pass `on_event=` to receive each `SpanEvent` live.

> These are the substrate's own building blocks — a host that drives many sandboxes builds on exactly this surface. Prefer the `Sandbox` facade for single-shot runs; reach here only when you need many sandboxes, dind, a disk-backed graph, or per-spec egress.

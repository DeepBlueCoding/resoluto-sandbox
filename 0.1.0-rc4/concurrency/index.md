# Concurrency & direct control

The `Sandbox` facade runs ONE program per `run()`. To launch MANY sandboxes under a bounded RAM/slot budget, or to reach knobs the facade never exposes (docker-in-docker, a disk-backed image graph, per-launch egress on the spec), drop to the building blocks the facade is built on — `SandboxPool`, `SandboxLaunchSpec`, and `drive_node`. All three are exported from `resoluto.sandbox`.

Both snippets assume a `runtime` (a `SandboxRuntime` — e.g. `KataNerdctlSandboxRuntime.from_env(...)` for local or `K8sSandboxRuntime(...)` for k8s) and, for `drive_node`, a `conduit` (a `Conduit` — e.g. `store_from_env()`), wired exactly as the [`local`](https://deepbluecoding.github.io/resoluto-sandbox/0.1.0-rc4/backends/#local) / [`k8s`](https://deepbluecoding.github.io/resoluto-sandbox/0.1.0-rc4/backends/#k8s) backends build them.

## Bounded concurrency — `SandboxPool`

`SandboxPool` admits launches FIFO under two independent limits: a slot count (`max_concurrent`) and an optional RAM budget (`mem_budget_bytes`, summed from each spec's `resources.memory_bytes`). A held lease occupies a slot plus its RAM until closed; closing the lease destroys the sandbox.

```python
import asyncio
from resoluto.sandbox.contracts import Resources, SandboxLaunchSpec
from resoluto.sandbox.pool import SandboxPool

pool = SandboxPool(runtime, max_concurrent=4, mem_budget_bytes=16 * 1024**3)  # ≤4 at once, ≤16 GiB total

async def one(prefix: str) -> None:
    spec = SandboxLaunchSpec(
        image="<registry>/resoluto-sandbox-base:0.1.0",
        store_prefix=prefix,
        resources=Resources.from_quantities(memory="4Gi", cpu="2"),
    )
    async with await pool.acquire(spec) as lease:   # parks here until a slot + budget free up
        ...  # work against lease.handle (e.g. drive_node below); the sandbox is destroyed on exit

await asyncio.gather(*(one(f"run/demo/nodes/n{i}/sandbox-0") for i in range(10)))  # 10 queued, 4 run at once
```

- `pool.available` / `pool.live_count` report free slots / live sandboxes.
- `acquire(spec, *, on_wait=None)` — `on_wait(amount, available)` fires once if the caller parks on the RAM budget.
- `SandboxPool` also satisfies the `Admission` protocol, so you can hand it straight to `drive_node(..., admit=pool)` to pool-admit a driven node (below).

> The pool bounds SUBSTRATE admission (how many sandboxes exist at once), NOT workload liveness. A slow-but-alive sandbox holds its slot as long as it keeps emitting — there is no wall-clock cap.

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

`drive_node(runtime, store, spec, *, admit=None, on_event=None, dead_after_s=120.0)` launches, tails the Conduit chunks, and returns a `NodeResult`. Pass `admit=pool` to admit through a `SandboxPool`; pass `on_event=` to receive each `SpanEvent` live.

> These are the substrate's own building blocks — a host that drives many sandboxes builds on exactly this surface. Prefer the `Sandbox` facade for single-shot runs; reach here only when you need pooling, dind, a disk-backed graph, or per-spec egress.

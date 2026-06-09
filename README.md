# resoluto-sandbox

Store-mediated, Kata-isolated, cloud-agnostic sandbox runtime for Resoluto lanes.

The orchestrator and the sandbox **never hold a connection**. The sandbox is passive:
it opens no inbound port and keeps no long-lived stream. It writes append-only,
immutable JSONL chunk objects to a durable object store; the orchestrator launches it,
tails the store, and reaps it. This is the design that kills the long-lived-stream
wedge that sank the previous (CubeSandbox) substrate — there is no stream to wedge.

## The three interfaces

Everything hangs off three small contracts (`contracts.py`):

| Interface | Role | Implementations |
|-----------|------|-----------------|
| `SandboxRuntime` | the ONE platform-specific surface — launch / status / destroy / sweep / logs | `K8sSandboxRuntime` (Kata via k8s `runtimeClassName`) |
| `ObjectStore`    | the durable rendezvous — put / get / list_prefix | `LocalFsObjectStore`, `S3ObjectStore` (minio / any S3), `GcsObjectStore` |
| `SandboxPool`    | platform-independent FIFO admission + concurrency cap | `SandboxPool` |

Porting to a new cloud is two adapters (`SandboxRuntime` + `ObjectStore`), nothing else.

## How a lane runs

```
host (orchestrator)                         object store            sandbox (Kata pod)
─────────────────────                       ────────────            ──────────────────
pool.acquire(spec) ───────── launch ───────────────────────────────▶ runner_main
drive_node():                                                          run_node_in_sandbox:
  ChunkReader.poll() ◀──── events-000000.jsonl ◀──── ChunkShipper ──── setup → workload → cleanup
  forward SpanEvents                                  (append-only)     (each step = a span)
  runtime.status() ─ terminal? ─▶ read result.json ◀── result.json ─── write result + _manifest
  lease.release() ──────────── destroy ──────────────────────────────▶ (pod reaped)
```

- **Liveness** = monotonic chunk arrival. A silently-dead substrate (the guest can't
  report its own death) is **time-bounded** by the reader's death window and captured
  with host-side forensics (`drive_node` returns a `substrate_logs` failure).
- **Verdict** is derived **orchestrator-side** (§12.12) — the in-guest exit code in
  `result.json` is work product, not a trust decision.
- **Telemetry** is a span tree (run → node → setup/workload/cleanup) via `SpanEvent`,
  redacted on egress (`redact.py`).

### Step lifecycle hooks

`run_node_in_sandbox` exposes injectable, observable hooks (env `RESOLUTO_SETUP_ARGV` /
`RESOLUTO_CLEANUP_ARGV`):

- `setup_argv` — runs before the workload; a non-zero exit fails the node.
- `cleanup_argv` — runs after the workload, **always** (success, failure, or staging
  error), best-effort. The place to free temp/resources between gates, e.g.
  `docker builder prune -f`, `docker compose down -v`.

## Storage on Kata (read before touching the dind path)

The Kata guest's `/var/lib/docker` is on **virtiofs (FUSE)**. For `dind` lanes the inner
dockerd must use **kernel `overlay2` on a RAM-backed tmpfs graph** — `K8sSandboxRuntime`
mounts the graph as `emptyDir{medium: Memory, sizeLimit: spec.docker_graph_size}`.

The alternatives are dead ends, proven on a full multi-image build:

- **vfs** copies every layer's files → ~1.5M files for a full compose stack → exhausts
  *virtiofsd's host-side* file handles → `too many open files` while the guest itself
  uses <40 fds (a misleading errno — not a guest fd limit).
- **overlay2 directly on virtiofs** → `failed to mount overlay: invalid argument`.
- **fuse-overlayfs** → initializes, then **deadlocks the guest** (D-state on FUSE).

tmpfs is RAM, so a `dind` lane's graph counts against pod memory (the image bytes must
fit). Only `tier-2 + docker_compose` lanes pay this; pure-compute lanes use no docker.

## Usage

```python
from resoluto_sandbox import (
    SandboxLaunchSpec, SandboxPool, drive_node,
)
from resoluto_sandbox.runtime.k8s import K8sSandboxRuntime
from resoluto_sandbox.objectstore.s3 import S3ObjectStore

runtime = K8sSandboxRuntime(namespace="resoluto-sandboxes")
pool = SandboxPool(runtime, max_concurrent=4)
store = S3ObjectStore("resoluto-lanes", endpoint_url="http://minio:9000", ...)

spec = SandboxLaunchSpec(
    image="resoluto-lane:dev", flavor="dind", privileged=True, runtime_class="kata",
    cpu="8", memory="24Gi", docker_graph_size="18Gi",
    store_prefix="run/<run_id>/nodes/<node_id>",
    args=["python", "-m", "resoluto_sandbox.runner_main"],
    env={...},  # RESOLUTO_STORE_*, RESOLUTO_WORKLOAD_ARGV, ...
)

result = await drive_node(pool, store, spec, on_event=print)
```

The in-sandbox half is `python -m resoluto_sandbox.runner_main`, configured entirely
from env (`store_from_env()` + `RESOLUTO_WORKLOAD_ARGV` / `RESOLUTO_WORKSPACE_DIR` /
`RESOLUTO_SETUP_ARGV` / `RESOLUTO_CLEANUP_ARGV` / `RESOLUTO_OUTPUT_PATHS`).

## Install

```bash
uv pip install -e ".[k8s,s3]"   # extras: [k8s] kubernetes-asyncio, [s3] aioboto3, [gcs] gcloud-aio-storage
```

## Testing

```bash
uv run pytest                    # 28 unit tests (default; integration deselected)
uv run pytest -m integration     # live: needs k3s + Kata runtimeClass + minio
```

Integration tests assume the dev box: k3s, Kata runtimeClass `kata`, a minio on
`:9100`. The canonical end-to-end proof is `resoluto-worker`'s
`test_resoluto_selftest_in_kata.py` — Resoluto builds + runs its own compose stack and
all six suites inside one Kata sandbox (~6 min).

## Security

No host-privileged pods (Kata gives guest-scoped privilege via
`privileged_without_host_devices`); no env/secret leak (telemetry redaction on egress);
fail-closed admission; prefix-scoped, write-only, expiring store credentials for the
sandbox. See `tasks/sandbox-design-2026-06-08.md` §12 in the umbrella repo.

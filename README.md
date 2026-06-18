# resoluto-sandbox

> **A store-mediated, Kata-isolated, cloud-agnostic substrate for running untrusted workloads — where the orchestrator and the sandbox _never hold a connection_.**

<p align="left">
  <img alt="python" src="https://img.shields.io/badge/python-3.12%2B-blue">
  <img alt="isolation" src="https://img.shields.io/badge/isolation-Kata%20microVM-8A2BE2">
  <img alt="rendezvous" src="https://img.shields.io/badge/rendezvous-object%20store-0aa">
  <img alt="status" src="https://img.shields.io/badge/status-alpha-orange">
</p>

`resoluto-sandbox` launches a **passive** sandbox — it opens no inbound port and keeps no long-lived
stream. The sandbox writes append-only, immutable JSONL chunk objects into a durable object store;
the orchestrator launches it, **tails the store**, and reaps it. Inputs (a worktree tar, the spec,
credentials) and outputs (telemetry, the result) flow only as store objects.

That single design decision — *no connection between the two halves* — is what makes the substrate
robust: **there is no stream to wedge, no socket to hang, no daemon to lose.** A crashed orchestrator
re-lists the store and resumes; a silently-dead sandbox is detected by the *absence* of new chunks.

---

## Why it exists

Connection-oriented sandboxes (an in-guest agent server, a held `exec` stream, a websocket) share one
failure mode: **the control channel can wedge.** Under load — a serialization spike, a memory stall, a
nested-virtualization hiccup — the stream goes quiet while neither side can prove the other is alive.
You then either kill healthy work (false-positive reap) or wait forever (hung lane). We lived this; it
sank our previous substrate.

`resoluto-sandbox` removes the channel entirely. Liveness is redefined as **monotonic chunk arrival**:
work is alive exactly as long as new chunks keep landing in the store. A lane that goes silent past a
bounded *death window* is declared dead and reconstructed from host-side forensics — never guessed at
over a frozen socket.

---

## Features

| | |
|---|---|
| 🛰️ **Passive, store-mediated comms** | No inbound port, no held stream. The sandbox self-reports JSONL to an object store; the host tails it. Crash-durable and replayable on **both** ends. |
| 🔒 **Hardware-VM isolation (Kata)** | Each sandbox is a throwaway microVM via the k8s `kata` `runtimeClass`. A kernel/runc escape lands in a disposable guest kernel — never the host. The host pod stays **unprivileged**. |
| ☁️ **Cloud-agnostic by construction** | The whole system hangs off three small interfaces. Porting to a new platform is **two adapters** — a `SandboxRuntime` and an `ObjectStore` — nothing else. |
| 💓 **Liveness = chunk arrival** | A silently-dead substrate (the guest can't report its own death) is **time-bounded** by a death window and captured with host-side forensics. No false-positive reaps of healthy work. |
| 🌲 **Span-tree telemetry** | Every run is a tree of `SpanEvent`s (run → node → setup/workload/cleanup), redacted on egress. The same wire drives live UI, replay, and post-mortem forensics. |
| 🧱 **dind that actually works on Kata** | A hard-won `overlay2`-on-tmpfs graph makes Docker-in-Docker reliable inside a Kata guest (where the obvious drivers all dead-end — see [Storage on Kata](#storage-on-kata-read-before-touching-the-dind-path)). |
| ⚖️ **Orchestrator-side verdicts** | The in-guest exit code is **work product, not a trust decision.** A fully-compromised guest cannot forge a result; the authoritative verdict is derived outside the guest. |
| 🪝 **Step lifecycle hooks** | Injectable, observable `setup` / `cleanup` hooks — the place to free temp/RAM between gates (`docker builder prune`, `compose down -v`). |

---

## Architecture

Three small contracts (`contracts.py`) are the entire surface area:

| Interface | Role | Implementations |
|-----------|------|-----------------|
| `SandboxRuntime` | the ONE platform-specific surface — `launch` / `status` / `destroy` / `sweep` / `logs` | `K8sSandboxRuntime` (Kata via k8s `runtimeClassName`) |
| `ObjectStore`    | the durable rendezvous — `put` / `get` / `list_prefix` | `LocalFsObjectStore`, `S3ObjectStore` (minio / any S3), `GcsObjectStore` |
| `SandboxPool`    | platform-independent admission — count cap + [resource-aware RAM budget](#resource-aware-admission) (queue/park, no starvation) | `SandboxPool` |

```mermaid
flowchart LR
    subgraph host["🧠 Orchestrator (host)"]
        pool["SandboxPool<br/>FIFO admission + cap"]
        drive["drive_node()<br/>tail · reap"]
    end
    subgraph store["🗄️ ObjectStore (durable rendezvous)"]
        chunks["events-000000.jsonl<br/>events-000001.jsonl<br/>…<br/>result.json"]
    end
    subgraph guest["📦 Sandbox (Kata microVM pod)"]
        runner["runner_main<br/>setup → workload → cleanup"]
    end

    pool -- "launch" --> runner
    runner -- "append-only chunks" --> chunks
    drive -- "list_prefix + get (poll)" --> chunks
    drive -- "status terminal? → read result.json, then destroy" --> runner

    classDef s fill:#1e1e2e,stroke:#8A2BE2,color:#cdd6f4;
    classDef t fill:#11111b,stroke:#0aa,color:#cdd6f4;
    class host,guest s; class store t;
```

**The isolation stack** — privilege is inverted relative to a privileged-DinD design: the host pod is
unprivileged; any escalation a `dind` lane needs is *guest-scoped* by Kata.

```mermaid
flowchart TD
    node["k8s node (kubelet)"] --> pod["Pod · runtimeClassName: kata · UNPRIVILEGED"]
    pod --> vm["Kata microVM — own guest kernel"]
    vm --> work["workload (plain)"]
    vm --> dind["inner dockerd (dind, guest-scoped privilege)<br/>overlay2 on a RAM-backed tmpfs graph"]

    classDef k fill:#11111b,stroke:#585b70,color:#cdd6f4;
    classDef v fill:#1e1e2e,stroke:#8A2BE2,color:#cdd6f4;
    class node,pod k; class vm,work,dind v;
```

---

## How a lane runs

```
host (orchestrator)                         object store            sandbox (Kata pod)
─────────────────────                       ────────────            ──────────────────
pool.acquire(spec) ───────── launch ───────────────────────────────▶ runner_main
drive_node():                                                          run_node_in_sandbox:
  ChunkReader.poll() ◀──── events-000000.jsonl ◀──── ChunkShipper ──── setup → workload → cleanup
  forward SpanEvents                                  (append-only)     (each step = a span)
  runtime.status() ─ terminal? ─▶ read result.json ◀── result.json ─── write result
  lease.release() ──────────── destroy ──────────────────────────────▶ (pod reaped)
```

- **Liveness** is monotonic chunk arrival. Silence past the death window ⇒ a `substrate dead` failure
  with host-side forensics attached.
- **Verdict** is derived orchestrator-side — the in-guest exit code in `result.json` is work product,
  not a trust decision.
- **Telemetry** is a span tree (`run → node → setup/workload/cleanup`) via `SpanEvent`, redacted on egress.

---

## Quickstart

### Install

```bash
uv pip install -e ".[k8s,s3]"
# extras: [k8s] kubernetes-asyncio · [s3] aioboto3 · [gcs] gcloud-aio-storage
```

The package's top-level surface is **platform-independent** (pydantic only). Concrete runtimes and
stores import their heavy deps lazily, so you pull in only what you use.

### Host side — launch, tail, reap

```python
from resoluto_sandbox import SandboxLaunchSpec, SandboxPool, drive_node
from resoluto_sandbox.runtime.k8s import K8sSandboxRuntime
from resoluto_sandbox.objectstore.s3 import S3ObjectStore

runtime = K8sSandboxRuntime(namespace="resoluto-sandboxes", context="default")
pool = SandboxPool(runtime, max_concurrent=4)
store = S3ObjectStore("lanes", endpoint_url="http://minio:9000",
                      aws_access_key_id="…", aws_secret_access_key="…")

spec = SandboxLaunchSpec(
    image="your-lane-image:dev",
    flavor="plain",                 # or "dind" for docker-compose workloads
    runtime_class="kata",
    cpu="2", memory="4Gi",
    store_prefix="run/<run_id>/nodes/<node_id>",   # where the sandbox self-reports
    args=["python", "-m", "resoluto_sandbox.runner_main"],
    env={
        "RESOLUTO_STORE_KIND": "s3",
        "RESOLUTO_STORE_BUCKET": "lanes",
        "RESOLUTO_STORE_ENDPOINT": "http://minio:9000",
        "RESOLUTO_STORE_PREFIX": "run/<run_id>/nodes/<node_id>",
        "RESOLUTO_RUN_ID": "<run_id>",
        "RESOLUTO_NODE_ID": "<node_id>",
        "RESOLUTO_WORKLOAD_ARGV": '["bash","-lc","pytest -q"]',
        # AWS_* / scoped store creds injected here too
    },
    labels={"resoluto.node_id": "<node_id>"},
)

result = await drive_node(pool, store, spec, on_event=print)
print(result.status, result.reason)     # "success" | "failure"
```

`drive_node(pool, store, spec, *, on_event=None, poll_interval_s=2.0, dead_after_s=120.0)` returns a
typed [`NodeResult`](#noderesult): `status`, `exit_code`, `output_archive`, `observed_phase`,
`reason`, and `substrate_logs` (populated from out-of-guest signals when the substrate dies silently).

### Sandbox side — the passive runner

The image's entrypoint is `python -m resoluto_sandbox.runner_main`, **configured entirely from env.**
It opens no port; it learns where to self-report from the env the runtime injected, runs the workload,
and ships chunks + a `result.json` to the store:

| Env var | Meaning |
|---|---|
| `RESOLUTO_STORE_KIND` | `localfs` \| `s3` \| `gcs` |
| `RESOLUTO_STORE_ROOT` / `RESOLUTO_STORE_BUCKET` (+ `…_ENDPOINT`, `…_REGION`) | store location |
| `RESOLUTO_STORE_PREFIX` | the per-node prefix to write under |
| `RESOLUTO_RUN_ID` / `RESOLUTO_NODE_ID` | identity stamped on every span |
| `RESOLUTO_WORKLOAD_ARGV` | JSON argv of the actual work |
| `RESOLUTO_WORKSPACE_DIR` | cwd for the workload (optional) |
| `RESOLUTO_OUTPUT_PATHS` | JSON list of paths to collect back as an output archive (optional) |
| `RESOLUTO_SETUP_ARGV` / `RESOLUTO_CLEANUP_ARGV` | JSON argv for the lifecycle hooks (optional) |

> **Bring your own image.** `resoluto-sandbox` is the *runtime contract*, not an image. Any container
> that has Python + this package on `PYTHONPATH` and runs `runner_main` as its entrypoint is a valid
> sandbox. For `dind` workloads the image must ship Docker + the storage setup below.

### Step lifecycle hooks

`run_node_in_sandbox` exposes injectable, observable hooks:

- `setup_argv` — runs **before** the workload; a non-zero exit fails the node.
- `cleanup_argv` — runs **after** the workload, **always** (success, failure, or staging error),
  best-effort. The place to free temp/RAM between gates, e.g. `docker builder prune -f`,
  `docker compose down -v`.

---

## Object store backends

| Backend | Class | Use |
|---|---|---|
| Local filesystem | `LocalFsObjectStore` | dev, single-box, tests — zero infra |
| S3 / MinIO | `S3ObjectStore` | production rendezvous on any S3-compatible store |
| Google Cloud Storage | `GcsObjectStore` | GCP deployments |

Chunks are **immutable** append-only objects, so the store needs no append semantics — the reader tails
via `list_prefix` + whole-object `get`. Any blob store with list + read-after-write can be a backend; a
new one is a single `ObjectStore` subclass.

---

## Storage on Kata (read before touching the `dind` path)

`dind` lanes need `/var/lib/docker` on a **real (non-virtiofs) filesystem** so the inner dockerd can
use `overlay2`. Two backends are supported, selected by `SandboxLaunchSpec.graph_backend`:

### `block` (production path — recommended)

`graph_backend="block"` emits `emptyDir: {sizeLimit: docker_graph_block_size}` (no `medium` key). Kata
maps this to a **virtio-blk block device** inside the guest. The `lane-entrypoint.sh` detects the raw
block device via `findmnt`/`blkid`, formats it `ext4` (idempotent — skipped if already formatted), and
remounts it before starting `dockerd`. Overlay2 on ext4/virtio-blk is **proven in Spike #1** (2026-06-08):
full compose build + `docker compose up --wait` healthy, Playwright e2e green. This path does **not**
count against pod memory — the block device is outside the guest's RAM budget, so compose stacks that
exceed available RAM no longer OOM silently.

**Required Kata host configuration** (`/opt/kata/share/defaults/kata-containers/configuration.toml`):
```toml
[hypervisor.qemu]
# Allow Kata to present non-Memory emptyDirs as virtio-blk block devices in the guest.
# Without this, emptyDir without medium falls back to virtiofs (FUSE), which fails overlay2.
block_device_driver = "virtio-blk"
enable_block_device_use = true
```
Verify the installed Kata version supports `enable_block_device_use` (Kata 3.x+). The k3s + Kata 3.31.0
setup on this host is confirmed working.

**Activation**: set `RESOLUTO_LANE_GRAPH_BACKEND=block` in the worker environment. Defaults to `tmpfs`.
Block size defaults to `50Gi`; override with `RESOLUTO_LANE_DIND_BLOCK_SIZE`.

**Why in-guest loop files don't work**: `mount -o loop` needs `/dev/loop*` device nodes, which
`privileged_without_host_devices` withholds — confirmed dead end in Spike #1. The block device must be
Kata-attached (virtio-blk), not created inside the guest.

### `tmpfs` (fallback / default)

`graph_backend="tmpfs"` (default) emits `emptyDir: {medium: Memory, sizeLimit: docker_graph_size}`.
This is RAM-backed — overlay2 works, but the graph counts against pod memory. The default `docker_graph_size`
is `16Gi` (overridden to `18Gi` via `RESOLUTO_LANE_DIND_GRAPH`); a 24Gi pod leaves only 6Gi for the
workload, and on boxes with ~11Gi available RAM this OOMs silently.

The virtiofs alternatives are **proven dead ends** on a real multi-image build:

| Driver | Failure on the Kata virtiofs guest |
|--------|------------------------------------|
| `vfs` | copies every layer's files → exhausts **virtiofsd's host-side** file handles → `too many open files` *while the guest uses <40 fds* (a misleading errno) |
| `overlay2` directly on virtiofs | `failed to mount overlay: invalid argument` — unsupported |
| `fuse-overlayfs` | initializes, then **deadlocks the guest** (D-state on FUSE) |

Only `dind` lanes pay the storage cost; pure-compute lanes use no Docker.

---

## Liveness & verdicts

- **Only substrate-silence kills.** A lane that is quiet but still shipping chunks (e.g. a long, output-
  silent compose build) is never reaped; absence of *any* new chunk past `dead_after_s` is the kill
  signal. Healthy-but-quiet work is safe.
- **Silent death is reconstructed, not guessed.** When a terminal pod has no/garbled `result.json`, the
  driver returns a `failure` `NodeResult` carrying `observed_phase` and the tail of `runtime.logs()` —
  enough to tell an OOM run-up from a scheduling failure.
- **The guest is not trusted on read.** `result.json` and pod logs are forensic; the authoritative
  verdict is derived from out-of-guest signals. A compromised guest cannot forge a pass.

#### `NodeResult`

```python
node_id: str
status: Literal["success", "failure"]
exit_code: int | None
output_archive: str | None     # key of the collected outputs tar, if any
observed_phase: str            # filled by the orchestrator from runtime.status()
reason: str                    # human-readable failure cause
substrate_logs: str            # host-side forensics on silent death
```

---

## Resource-aware admission

A finite cluster can't launch sandboxes freely: a plain lane pod is small (e.g. 4Gi)
but a `dind` gate pod is large (e.g. 12Gi). `SandboxPool` therefore admits by **real
memory cost against a budget**, not just a pod count — on by default, sized from the node.

```python
# Default: budget derived from the node's allocatable RAM (resolved async on first acquire).
pool = SandboxPool(runtime, max_concurrent=4, mem_budget_provider=node_ram_share)
# Or a fixed budget:
pool = SandboxPool(runtime, max_concurrent=4, mem_budget_bytes=parse_k8s_memory("16Gi"))
```

- **Budget, not count.** A spec is admitted only when its `spec.memory` fits within the
  pool's remaining byte budget. The count cap (`max_concurrent`) remains as a secondary
  ceiling.
- **Queued holds nothing.** When a spec doesn't fit it **parks on a fair
  `ResourceSemaphore`** — the pod is *not launched* until budget frees, so a pipeline
  waiting for resources consumes **no RAM** (just a parked future, no spin, no held
  thread). It is woken event-driven the instant a release frees enough budget.
- **No starvation, no race.** The semaphore grants the FIFO **head** first and never
  skips it for a smaller waiter behind it (a heavy step always eventually runs);
  allocation is atomic on `release` (two waiters can't both claim the same freed bytes).
- **Fail-loud on the impossible.** A spec larger than the whole budget raises rather
  than parking forever; a count-cap that can't clear within `acquire_timeout_s` raises
  *substrate starvation* (distinct from agent-work liveness — no wall-clock kill of work).
- **`on_wait` surfaces the state.** `acquire(spec, on_wait=...)` fires the callback once
  when the caller parks, so the orchestrator can mark the execution **"queued for
  resources"** (Resoluto emits a `resource_queued` event for both lanes and gate pods).

> **On by default — derived from the node, no config.** Resoluto turns this on
> automatically: it reads the node's **allocatable RAM** and **partitions it per-kind** —
> the gate pool (heavy `dind` pods) gets the larger share, the lane pool (cheap fleet)
> the rest. The two shares **sum to node RAM**, so the lane fleet + gate pods together
> never over-commit the node. There is no env var to set (the budget is resolved lazily
> on first acquire, since the node-RAM query is async); when node RAM is unknown
> (offline/tests) the memory gate is simply off.
>
> **Per-kind budgets, never shared.** The lane pool and the (independent) gate pool each
> carry their **own** budget. A *shared* budget would let a full lane fleet starve gate
> admission while each lane waits on a gate verdict — a deadlock. Separate per-kind
> budgets keep the two independent. In-process per worker; cross-replica coordination is
> the k8s `ResourceQuota` backstop.

---

## Security model

What the substrate gives you **today**, honestly scoped:

- ✅ **Per-lane hardware-VM isolation.** Kata `runtimeClass`; the host pod is unprivileged; `dind`
  privilege is guest-scoped (`privileged_without_host_devices`). A guest escape stays in a throwaway VM.
- ✅ **Zero inbound attack surface.** The sandbox listens on nothing; all comms are the sandbox writing
  to a store it was handed write access to. There is no host docker socket, no control daemon.
- ✅ **Untrusted-by-default verdicts.** In-guest output never decides the outcome (§ orchestrator-side
  verdict above).
- ✅ **Telemetry redaction on egress.** `redact.py` scrubs known secret shapes from spans before any
  chunk is written. (Treat it as belt-and-suspenders, not a primary control.)

> **Shipped egress controls** — `K8sSandboxRuntime` now pairs every lane pod with a declarative k8s
> NetworkPolicy when an `EgressConfig` is supplied. The policy enforces default-deny egress and allows
> only the configured CIDRs (object store, LLM provider, git hosts) on TCP/443 plus kube-dns on UDP/53.
> Every `ipBlock` rule includes `except: ["169.254.169.254/32"]` to block the cloud metadata endpoint
> (IMDS) regardless of the allowed CIDR range. The NetworkPolicy is owner-referenced to the pod so it
> is garbage-collected automatically on pod deletion.
>
> **Operational requirements for enforcement:**
> - **Enforcing CNI required** — the cluster must run Calico, Cilium, or another CNI that enforces
>   `NetworkPolicy`. Plain Flannel silently accepts the policy object but never enforces it; a Flannel
>   cluster provides no egress isolation even when `EgressConfig` is set.
> - **IMDSv2 hop-limit = 1 recommended** — set `HttpPutResponseHopLimit=1` on all nodes as a
>   belt-and-suspenders control. The `except` block in the NetworkPolicy is the primary guard; the
>   hop-limit prevents escalation if a CNI misconfiguration or upgrade gap temporarily opens IMDS.
> - **Minimal node service account recommended** — bind the node's SA to the minimum IAM policy
>   needed for kubelet operation. Even with IMDS blocked, defense-in-depth limits blast radius if the
>   node credential is reached through another path.
> - **EgressConfig fields must be CIDRs** — k8s NetworkPolicy `ipBlock` does not support FQDNs.
>   The caller resolves hostnames to IPs and passes CIDR strings (e.g. `"1.2.3.4/32"`) to
>   `EgressConfig`. Passing a bare hostname raises `ValueError` at construction time.
>
> **Shipped credential scoping** — for S3/MinIO backends the host mints a **per-lane STS credential**
> scoped to the run's own prefix (`PutObject`/`GetObject` on `<prefix>/*` plus prefix-scoped
> `ListBucket`), ~1h TTL, injected as `RESOLUTO_STORE_WRITE_TOKEN`; the in-guest `store_from_env`
> consumes it and the broad `AWS_*` credentials are **no longer forwarded** to the guest. One lane can
> neither read nor overwrite another lane's objects. Fail-closed: an S3 backend with no
> `RESOLUTO_STORE_STS_ROLE_ARN` configured refuses to launch rather than fall back to broad creds.
> (The `localfs` dev backend has no credentials and is unaffected.)
>
> **Shipped fail-closed controls** —
> - **Acquire-time egress canary:** before the workload runs, the guest verifies a known
>   non-allowlisted host and IMDS are unreachable while the store is reachable; on failure the lane
>   refuses to run (reported over the store channel — no long-lived stream).
> - **Isolation-floor admission guard:** `SandboxPool`/`K8sSandboxRuntime` refuse to launch a
>   non-Kata `runtime_class` (`''`/`runc`) unless `RESOLUTO_TRUSTED_LOCAL` is explicitly set, so a
>   silent microVM→shared-kernel downgrade can't happen against adversarial code.
> - **k8s-native orphan GC:** lane pods carry `ownerReferences` (+ a namespace `ResourceQuota` /
>   `LimitRange`), so the cluster garbage-collects leaked pods even if the orchestrator dies.

---

## Testing

```bash
uv run pytest                    # unit tests (integration deselected by default)
uv run pytest -m integration     # live: needs k3s + Kata runtimeClass + a MinIO
```

Integration tests assume a dev cluster: k3s, the `kata` runtimeClass, and a MinIO. The canonical
end-to-end proof is the consuming project's selftest — building and running a full compose stack and a
real test suite **inside one Kata sandbox**, self-reported through MinIO.

---

## Caveats & limitations

- **`dind` lanes are RAM-bound when using the default `tmpfs` backend.** The image bytes must fit in
  pod RAM, and an over-budget graph can OOM. Use `RESOLUTO_LANE_GRAPH_BACKEND=block` (the
  block-backed virtio-blk path) to lift the RAM ceiling — the block device is outside the guest's
  RAM budget. See [Storage on Kata](#storage-on-kata-read-before-touching-the-dind-path).
- **Single backend, real today.** `K8sSandboxRuntime` (Kata) is the only shipped `SandboxRuntime`; the
  three-interface design makes ECS/Fly/Docker adapters straightforward, but they aren't written yet.
  "Cloud-agnostic" describes the *seam*, not multiple shipped backends.
- **Egress enforcement depends on the CNI.** Default-deny egress + IMDS block ship as a declarative
  NetworkPolicy, but a non-enforcing CNI (plain Flannel) silently no-ops it — the acquire-time egress
  canary is what turns "policy applied" into "enforcement verified". See [Security model](#security-model).
- **Scoped store creds require STS on the backend.** The per-lane prefix-scoped token needs an
  S3/MinIO that supports `AssumeRole` + a configured `RESOLUTO_STORE_STS_ROLE_ARN`; without it an s3
  backend fails closed (the `localfs` dev path is unaffected).
- **Latency, not interactivity.** Store-mediated comms trade a few seconds of polling latency for
  wedge-resistance. This substrate is built for batch/lane workloads that run for minutes, not for
  sub-second interactive sessions.
- **A missing chunk stalls the tail.** The reader consumes chunks in contiguous index order; a gap
  (e.g. a shipper crash mid-sequence) blocks the tail until the death window fires. Robust, but the
  diagnostic for a gap vs. true silence is still coarse.

---

## Image layers

The runtime image is split into three tiers so `resoluto-sandbox` is usable as a standalone OSS project
without any Anthropic/Claude tooling baked in:

```
resoluto-sandbox-base:dev          (resoluto-sandbox/Dockerfile.base)
  └── substrate only
      python 3.12, docker-ce + compose, node 20, tini, gh
      overlay2 daemon.json, lane user (uid 1000), pam_limits
      resoluto-sandbox wheel (passive store runner)
      lane-entrypoint.sh (dind startup + runuser drop)

resoluto-lane:dev                  (resoluto-worker/Dockerfile.lane)
  └── FROM resoluto-sandbox-base:dev
      factory wheels (core/pipeline/agent-claude/sandbox-cube/worker)
      provider npm globals (from PROVIDER_NPM_GLOBALS build-arg)
      playwright + chromium (behavioral_gate)
      gate-runner (baked Playwright harness)
      ~/.claude credential wiring
      factory git identity (user.email / user.name)
```

### Standalone OSS usage

The base image alone is a valid `resoluto-sandbox` runtime. Any container built `FROM
resoluto-sandbox-base:dev` that runs `runner_main` as its entrypoint is a fully functional sandbox:

```bash
docker build -f resoluto-sandbox/Dockerfile.base -t resoluto-sandbox-base:dev .
docker run --rm resoluto-sandbox-base:dev python -m resoluto_sandbox.runner_main --help
```

### Adding a new agent vendor

No Dockerfile edit required. Implement `system_deps` on your `AgentProviderInterface` subclass to
declare the npm globals your provider needs, then pass the package names as a `PROVIDER_NPM_GLOBALS`
build-arg when building the extension layer:

```python
# resoluto-agent-myprovider/src/resoluto_agent_myprovider/provider.py
@property
def system_deps(self) -> SystemDeps:
    return SystemDeps(npm_globals=[
        NpmGlobal(package="my-provider-cli", expected_binary="my-cli"),
    ])
```

```bash
docker build -f resoluto-worker/Dockerfile.lane \
    --build-arg BASE_IMAGE=resoluto-sandbox-base:dev \
    --build-arg PROVIDER_NPM_GLOBALS="my-provider-cli" \
    -t resoluto-lane-myprovider:dev .
```

---

## Roadmap

- [x] Default-deny egress NetworkPolicy + IMDS drop (shipped — `EgressConfig` in `runtime/k8s.py`)
- [x] Prefix-scoped / write-only / expiring store-credential minting + in-guest consumption (shipped — per-lane STS token via `mint_scoped_credential`)
- [x] k8s-native orphan GC (`ownerReferences` + TTL) and a deployment-wide cap (shipped — owner refs + `ResourceQuota`/`LimitRange`; kind-scoped per-pool admission)
- [x] Fail-closed admission guard (reject non-Kata `runtime_class` outside trusted-local) (shipped — `check_runtime_class_guard`)
- [x] Block-backed (virtio-blk) Docker graph to lift the `dind` RAM ceiling (shipped — `graph_backend="block"` in `SandboxLaunchSpec`; opt-in via `RESOLUTO_LANE_GRAPH_BACKEND=block`)
- [x] Per-gate flavor — the lane runs plain; only a compose gate spawns an ephemeral `dind` gate pod (shipped — gate offload via the store protocol)
- [x] Resource-aware admission — per-kind RAM budget via a fair `ResourceSemaphore`; a queued lane/gate holds no RAM (shipped — see [Resource-aware admission](#resource-aware-admission))
- [ ] Per-step pausable pipeline — release the **lane** pod between gates and resume from the store checkpoint (the semaphore foundation is shipped; lane-release + resume is the remaining deep step)
- [ ] Additional `SandboxRuntime` adapters (ECS / Fly / plain Docker)
- [x] OSS-clean base image: substrate + this runner only, with the consuming stack layered on top (shipped — `resoluto-sandbox/Dockerfile.base`)

---

## How it differs from connection-oriented sandboxes

| | Connection-oriented (agent server / held `exec`) | **resoluto-sandbox** |
|---|---|---|
| Control channel | live socket / stream | **none** — durable object store |
| Liveness | heartbeat / healthcheck over the channel | **chunk arrival** in the store |
| Orchestrator crash | loses the session | **re-list + resume** |
| Sandbox death | hang or false-positive reap | bounded death window + forensics |
| Inbound surface | a port / daemon to attack | **zero** |
| Trust on output | often implicit | **never** — verdict is orchestrator-side |

---

## License & author

Part of the **Resoluto** project — `DeepBlueCoding/resoluto-sandbox`. See `LICENSE` in the repository.

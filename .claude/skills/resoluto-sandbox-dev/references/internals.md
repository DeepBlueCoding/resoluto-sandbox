# Substrate internals + wire protocol

Action-first reference for an agent that USES or EXTENDS this sandbox. The wire is a
language-neutral JSON-on-KV protocol; the full spec is `../../../../spec/PROTOCOL.md` (relative to
this repo: `spec/PROTOCOL.md`). This doc covers the Python reference impl. Source of truth is the
code in `src/resoluto_sandbox/` — every signature below is verified against it.

There is NO long-lived connection between host and pod. They rendezvous **only** through a durable
key/value store (`Conduit`). The pod is passive: it self-reports JSONL chunks + a `result.json`; the
host tails + reaps. Liveness = monotonic chunk-arrival + heartbeats. **No wall-clock timeouts.**

---

## 1. Public API (the one entrypoint)

```python
from resoluto_sandbox import Sandbox, RunResult

sb = Sandbox(backend="docker")            # default: Docker container on this host
res = sb.run(["python", "agent.py", "--x"],
            workspace="/abs/dir",         # program cwd, staged into the sandbox
            env={"FOO": "1"},             # overlays the sandbox environment
            output_paths=["out/*.json"],  # globs collected into res.artifacts
            stream=sys.stdout)            # live output sink; None = sys.stdout
res.ok          # bool == (exit_code == 0)
res.exit_code   # int
res.output      # str   stdout+stderr, merged by the in-sandbox runner
res.errors      # str   ALWAYS "" — merged into output, by design
res.artifacts   # list[str]  collected output_paths (absolute paths)
res.result      # dict | None  parsed result.json if the program wrote one
res.reason      # str  substrate forensics (evicted/OOMKilled/observed_phase)
```

`Sandbox(backend=...)` accepts `"local"`, `"k8s"`, or a `Backend` instance.
`RunResult` is a pydantic `BaseModel` (`backends/base.py`). The program you run is **plain** — it
never imports `resoluto_sandbox`; it reads argv, writes stdout/files. `stdin` is NOT supported on
either backend (the substrate runner has no interactive stdin) — pass inputs via argv, env, or
workspace files.

### One backend, two runtimes

There is a single `SubstrateBackend` (`backends/substrate.py`). It is runtime-agnostic: it stages the
workspace in, builds a `SandboxLaunchSpec`, calls `drive_node(runtime, conduit, spec, ...)`, and maps
the resulting `NodeResult` to a `RunResult`. Isolation/placement is the injected `SandboxRuntime`:

| backend | runtime | conduit | isolation | stdin | errors |
|---|---|---|---|---|---|
| `docker` | `DockerSandboxRuntime` (`docker run`) | `LocalConduit` (bind mount) | OS-level (namespaces/cgroups) | ❌ | `""` |
| `k8s` | `K8sSandboxRuntime` (Kata pod) | `S3Conduit` | hardware (Kata microVM) + optional egress | ❌ | `""` |

Both runtimes run the SAME image entrypoint `args=["python","-m","resoluto_sandbox.runner_main"]`; the
container/pod stages inputs, runs the workload, ships span events, writes result.json. The backend
fails loud:

```python
if stdin is not None: raise NotImplementedError("stdin is not supported on the substrate backend")
if self._image is None: raise ValueError("SubstrateBackend requires image=")
```

### Configuring a backend (inject the runtime)

```python
import os
from resoluto_sandbox import Sandbox
from resoluto_sandbox.backends.substrate import SubstrateBackend, store_env_for_pod
from resoluto_sandbox.conduit.factory import store_from_env
from resoluto_sandbox.runtime.k8s import K8sSandboxRuntime, EgressConfig   # EgressConfig lives in runtime.k8s

runtime = K8sSandboxRuntime(
    namespace=os.environ.get("RESOLUTO_SANDBOX_NAMESPACE", "resoluto-sandboxes"),
    context=os.environ.get("RESOLUTO_SANDBOX_KUBECONTEXT"),
    egress=EgressConfig(                         # None → unrestricted egress (Kata kernel isolation only)
        store_cidr="10.0.0.5/32",                # ALL fields must be CIDR; FQDNs rejected in __post_init__
        llm_cidr="160.79.104.0/23",
        git_cidrs=[],                            # default empty == no git egress
    ),
)
sb = Sandbox(backend=SubstrateBackend(
    runtime=runtime,
    conduit=store_from_env(),                    # needs RESOLUTO_STORE_KIND
    image="registry.local/resoluto-lane:dev",   # REQUIRED — no default
    store_env=store_env_for_pod(os.environ),
))
```

The `"local"` / `"k8s"` string presets (`client.py`) build the same `SubstrateBackend` with the right
runtime, conduit, and store_env. The k8s preset reads `RESOLUTO_SANDBOX_NAMESPACE`,
`RESOLUTO_SANDBOX_KUBECONTEXT`, `RESOLUTO_LANE_IMAGE_PULL_POLICY` from env; the substrate hard-codes
`dead_after_s=600.0` on its `drive_node` call. (`DockerSandboxRuntime` is stdlib-only — it shells the
`docker` CLI; `K8sSandboxRuntime` is imported lazily so the core import stays pydantic-only.)

**Store env propagated to the sandbox** (`store_env_for_pod`): only `RESOLUTO_STORE_*` and
`RESOLUTO_TRUSTED_LOCAL` are forwarded. Host `AWS_*` creds are **NOT** forwarded unless a scoped
`RESOLUTO_STORE_WRITE_TOKEN` is absent AND `RESOLUTO_TRUSTED_LOCAL=1` (dev only) — otherwise it raises.
Footgun: prefer minting a prefix-scoped `RESOLUTO_STORE_WRITE_TOKEN` over forwarding ambient AWS creds.

---

## 2. The host↔pod loop: `drive_node`

`driver.py`. The ONE launch → tail → reap loop. Two layers:

```python
from resoluto_sandbox import drive_node, drive_node_raw, NodeOutcome
# NodeResult-returning wrapper:
async def drive_node(runtime, store, spec, *, admit=None, on_event=None,
                     poll_interval_s=2.0, dead_after_s=120.0, clock=time.monotonic) -> NodeResult
# raw outcome (caller reads its own work product):
async def drive_node_raw(runtime, store, spec, *, admit=None, on_event=None,
                         result_ready=None, poll_interval_s=2.0, dead_after_s=120.0,
                         unstartable_polls=15, external_gone_polls=15,
                         clock=time.monotonic) -> NodeOutcome
```

- `runtime: SandboxRuntime` — placement (e.g. `K8sSandboxRuntime`).
- `store: Conduit` — the rendezvous KV.
- `spec: SandboxLaunchSpec` — what/where to launch.
- `admit: Admission | None` — **OPTIONAL** admission gate. Pass a `SandboxPool` to FIFO-gate WHEN the
  pod launches; pass `None` and it launches immediately via `_direct_lease` (right shape when an
  external admitter — Kueue, plain kube-scheduler — already gates via the spec's pod metadata).
- `on_event: Callable[[SpanEvent], None] | ...Awaitable...` — fired per tailed span event (sync or async).
- `result_ready` (raw only) — `Callable[[], Awaitable[bool]]`; lets a caller whose work product lands
  BEFORE the pod reports terminal finish as soon as it appears. Omit → completion keys on pod-terminal.

### `NodeOutcome` dispositions (`drive_node_raw`)

```python
@dataclass(frozen=True)
class NodeOutcome:
    disposition: str        # 'completed' | 'unstartable' | 'external' | 'silent'
    observed_phase: str
    reason: str = ""
    substrate_logs: str = ""
```

- `completed` — pod ran to terminal, OR `result_ready()` returned True.
- `unstartable` — a fatal `waiting.reason` sustained `unstartable_polls` (15). Fail fast.
- `external` — sustained `'unknown'` phase (`external_gone_polls`) AND dead telemetry tail.
- `silent` — no telemetry within the death window; captures `runtime.logs(handle)` tail (last 4000 chars).

`_FATAL_WAITING` (fail-fast reasons, never reach running): `ImagePullBackOff`, `ErrImagePull`,
`ErrImageNeverPull`, `InvalidImageName`, `CreateContainerConfigError`, `CreateContainerError`,
`RunContainerError`, `CrashLoopBackOff`.

`drive_node` maps a non-`completed` outcome to `NodeResult(status="failure", ...)`; on `completed` it
reads `<prefix>/result.json` via `result_key(spec.store_prefix)` and parses it as `NodeResult`. A
missing/garbled `result.json` is itself a failure verdict — never trusted as the authoritative gate
verdict (that's derived orchestrator-side).

### Liveness contract (read this — it's the whole point)

- The silence window **arms only at RUNNING** (`reader.arm()`, idempotent). Pending / SchedulingGated /
  image-pull / external-admission time is **not** silence.
- `is_dead()` → WORK-silence (progress window). `substrate_silent` → SUBSTRATE-silence (any chunk
  arrival). Heartbeats keep `seconds_since_arrival` fresh while alive, so a heartbeating pod is NEVER
  reaped by `substrate_silent`; only a hung WORKLOAD trips `is_dead()` when a `progress_filter` is set.
- A non-contiguous gap (have chunk N+1, never saw N) stalls contiguous progress → surfaces through the
  same `is_dead()` window. No separate raise path.
- **No `wait_for(timeout=)`, no max-wall-seconds anywhere.** If work is alive, it runs.

---

## 3. In-pod runner: `runner_main` → `run_node_in_sandbox`

`runner_main.py` is the image ENTRYPOINT, configured ENTIRELY from env (the pod carries no
orchestrator connection). It reads:

| env | meaning |
|---|---|
| `RESOLUTO_STORE_KIND` (+ `RESOLUTO_STORE_*`) | builds the `Conduit` via `store_from_env()` |
| `RESOLUTO_STORE_PREFIX` | where to self-report (`run/<run_id>/nodes/<node_id>/...`) |
| `RESOLUTO_RUN_ID`, `RESOLUTO_NODE_ID` | identity |
| `RESOLUTO_WORKLOAD_ARGV` | **JSON list** — the program to exec |
| `RESOLUTO_WORKSPACE_DIR` | if set, stage `inbox/` here and run the workload here |
| `RESOLUTO_OUTPUT_PATHS` | JSON list — tarred to `outbox/` on success |
| `RESOLUTO_SETUP_ARGV` / `RESOLUTO_CLEANUP_ARGV` | JSON list — lifecycle hooks |
| `RESOLUTO_TRUSTED_LOCAL` | presence → skip egress canary |
| `RESOLUTO_CANARY_PROBE_HOST` / `_PORT` | canary target (default `1.1.1.1:80`) |
| `RESOLUTO_IMAGE_VERSION` | if set, asserts image↔wheel version match (fail loud on drift) |

Entrypoint exit code: `0 if result.status == "success" else 1`.

```python
async def run_node_in_sandbox(*, store, prefix, run_id, node_id, workload_argv,
    workspace_dir=None, output_paths=None, setup_argv=None, cleanup_argv=None,
    heartbeat_interval_s=5.0, clock=time.time,
    skip_egress_canary=False, canary_probe_host="1.1.1.1", canary_probe_port=80) -> NodeResult
```

Flow inside the root `node` span:
1. **Egress canary** (unless `skip_egress_canary`) — platform invariant, runs before setup/workload.
2. **Stage** — `Path(workspace_dir).mkdir(...)` then `stage_inputs(store, prefix, workspace_dir)`
   extracts every `inbox/*.tar.gz` (the repo arrives as a store object — never a runtime git clone).
3. **setup hook** (`setup_argv`) — non-zero exit ABORTS the node (failed setup = failed node, skip workload).
4. **workload** (`workload_argv`) — `result.exit_code = rc`; `status = "success" if rc==0 else "failure"`.
5. On `rc==0` + `output_paths`: `collect_outputs(...)` → `result.output_archive`.
6. **cleanup hook** (`cleanup_argv`) — ALWAYS runs (success/failure/staging error), best-effort, NEVER
   changes the verdict. This is the "free temp/resources after a gate" hook (`docker builder prune -f`,
   `docker compose down -v`, `rm -rf scratch`) so a reused sandbox's tmpfs graph doesn't accrue.
7. `finally`: write `<prefix>/result.json` (`result.model_dump_json()`), cancel heartbeat, close shipper.

Each command runs through `_exec_logged` under its own span, merging stdout+stderr as redacted `log`
events. The verdict here is the OBSERVED exit code — work product, not a trust decision.

---

## 4. Telemetry: `ChunkShipper` / `ChunkReader` (`telemetry.py`)

Append-FREE. Shipper writes immutable sequence-numbered chunks `events-000001.jsonl`, …; reader
list+concatenates in contiguous index order. Reconnect = "re-list, resume at index".

```python
ChunkShipper(store, prefix, *, flush_bytes=64*1024, flush_interval_s=5.0,
             heartbeat_s=30.0, heartbeat_factory=_default_heartbeat, clock=time.time)
  await shipper.emit_line(str)      # ship any opaque JSONL record (payload-agnostic)
  await shipper.emit(SpanEvent)     # typed convenience
  await shipper.tick()              # interval flush + heartbeat when quiet
  await shipper.close()             # final flush + writes _manifest.json {"total_chunks": N}

ChunkReader(store, prefix, *, dead_after_s=120.0, clock=time.monotonic, progress_filter=None)
  await reader.poll_lines() -> list[str]    # contiguous-index tail (payload-agnostic)
  await reader.poll()       -> list[SpanEvent]
  reader.arm()                               # start silence window (call at RUNNING; idempotent)
  reader.is_dead()          -> bool          # WORK-silence; False until armed/finished
  reader.substrate_silent   -> bool          # SUBSTRATE-silence; the only kill the worker acts on
  reader.finished           -> bool          # total_chunks reached
  reader.seconds_since_progress / .seconds_since_arrival
```

- The `_heartbeat` coroutine in the runner ticks the shipper every `heartbeat_interval_s` (5s) so a
  chunk lands even when quiet — keeps the reader's liveness signal monotonic AND drives timely flush of
  buffered output (per-line flush was removed).
- `progress_filter` (host side): with NONE, every line is progress (window == substrate-silence). With a
  filter, only accepted lines reset the window (window == work-silence) — unconditional heartbeats keep
  arrival fresh but CANNOT mask a hung workload. Feed EVERY line to the (stateful) filter; don't
  short-circuit with `any()`.
- `clock=time.monotonic` on the reader is deliberate: a host suspend must not count as silence.

---

## 5. Staging: tar-in-the-store (`staging.py`)

Inputs reach the passive sandbox ONLY as `<prefix>/inbox/<name>.tar.gz`; outputs return under
`<prefix>/outbox/`. Default-deny egress forbids a runtime `git clone`, so the repo MUST arrive as a
store object. `.git` rides INSIDE the tar (history preserved, zero git egress).

```python
INBOX = "inbox"; OUTBOX = "outbox"
await put_dir(store, prefix, local_dir, *, name="workspace", exclude=_DEFAULT_EXCLUDES) -> key   # HOST
await stage_inputs(store, prefix, workspace_dir) -> list[str]                                     # POD
await collect_outputs(store, prefix, workspace_dir, paths, *, name="output") -> key               # POD
await fetch_outputs(store, prefix, dest_dir) -> list[str]                                          # HOST
```

- `_DEFAULT_EXCLUDES` drops dep/build/cache trees (`.venv`, `venv`, `node_modules`, `__pycache__`,
  `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `.tox`, `.hypothesis`, `dist`, `build`, `htmlcov`,
  `.coverage`, `.next`, `.turbo`, `.cache`, `resoluto.old`, `.claude`). They bloat the tar by orders of
  magnitude AND hold absolute symlinks (e.g. `.venv/bin/python` → `/usr/bin/python`) that the safe-extract
  `data` filter rejects with `AbsoluteLinkError`, failing the whole stage. `.git` is KEPT on purpose.
- Extraction is ALWAYS `filter="data"` (rejects traversal / absolute / device entries) — the output tar
  is produced by the ADVERSARIAL guest. Absolute symlinks are dropped at archive time (only non-crashing
  option). Footgun: a declared `output_paths` entry that doesn't exist → loud `OSError`.

---

## 6. Wire protocol (language-neutral) — see `spec/PROTOCOL.md`

Transport is a `Conduit`: `put(key, bytes)` / `get(key) -> bytes` / `list_prefix(prefix) -> [ObjectInfo]`.
Optional `copy_prefix(src, dst) -> int` (default round-trips bytes; backends override for server-side
copy — this is how resume copies a lane forward). Encoding is ALWAYS UTF-8 JSON for structured objects
and gzip-tar for archives. No pickle/msgpack anywhere on the wire — any language that reads/writes JSON
and gzip-tar can implement a client.

Key namespace under `run/<run_id>/nodes/<node_id>/`:

| key (relative) | direction | what |
|---|---|---|
| `inbox/<name>.tar.gz` | host → pod | workspace, gzip-tarred |
| `task.json` | host → pod | **RESERVED — not read by the reference runner** (env-driven, not file-driven) |
| `events-<NNNNNN>.jsonl` | pod → host | progress, one JSON object per line, 6-digit zero-padded seq |
| `result.json` | pod → host | final verdict + output metadata (`NodeResult`) |
| `outbox/<name>.tar.gz` | pod → host | output artifacts |
| `_manifest.json` | pod → host | EOF: `{"total_chunks": N}` |

`SpanEvent` (each JSONL line) required fields: `run_id`, `span_id`, `parent_span_id` (`""` for root),
`kind` (advisory free string; common: `run`/`node`/`lane`/`attempt`/`gate`/`agent`/`tool`/`log`),
`name`, `event` (`open`/`close`/`log`), `ts` (epoch-seconds float), `status` (on `close`), `data`
(redacted). `NodeResult` (`result.json`): `node_id`, `status` (`success`|`failure`), `exit_code`,
`output_archive`, plus orchestrator-filled `observed_phase`/`reason`/`substrate_logs` (out-of-guest,
untrusted). Don't duplicate the schemas — `spec/*.schema.json` (JSON Schema draft 2020-12) are authoritative.

### Conduits (`store_from_env()`, `RESOLUTO_STORE_KIND`)

| kind | class | env | status |
|---|---|---|---|
| `localfs` | `LocalConduit(RESOLUTO_STORE_ROOT)` | — | **proven** (local backend) |
| `stdout` | `StdoutConduit()` | — | **proven** (local/debug) |
| `s3` | `S3Conduit(...)` | `RESOLUTO_STORE_BUCKET`/`_ENDPOINT`/`_REGION`, or a JSON `RESOLUTO_STORE_WRITE_TOKEN` (`bucket`/`endpoint_url`/`region`/`access_key_id`/`secret_access_key`/`session_token`) | **proven** against minio (k8s) |
| `gcs` | `GcsConduit(bucket, service_file=RESOLUTO_GCS_SERVICE_FILE)` | `RESOLUTO_STORE_BUCKET`, `RESOLUTO_GCS_SERVICE_FILE` | **EXPERIMENTAL / unverified — do not rely on it** |

---

## 7. k8s runtime specifics (`runtime/k8s.py`)

`K8sSandboxRuntime` maps launch/status/destroy/sweep onto Pods. `kubernetes_asyncio` imports lazily
(core stays dep-light). `SandboxRuntime` surface: `launch(spec) -> SandboxHandle`, `status(handle) ->
SandboxStatus`, `destroy(handle)`, `sweep(labels) -> int`, `logs(handle, tail=200)`, plus
`node_allocatable_memory()`, `ensure_run_owner(run_id)`, `delete_run_owner`, `reap_stale_run_owners`,
`count_active_pods(kind=None)`, `close()`.

```python
K8sSandboxRuntime(*, namespace="resoluto-sandboxes", kubeconfig=None, context=None,
                  image_pull_policy="IfNotPresent", egress=None, node_allocatable_memory=None)
```

**Kata `runtimeClass`.** Pods set `runtimeClassName: kata` (each is a QEMU microVM). The
`check_runtime_class_guard` invariant refuses any non-`kata` runtime_class unless `RESOLUTO_TRUSTED_LOCAL`
is set — an isolation downgrade is fail-loud, not silent.

**Pinned kube-context, fail-closed.** `_client()` calls `load_kube_config(context=self._context)`. If
no context is pinned (`context=None`) and not in-cluster, it **raises** unless
`RESOLUTO_SANDBOX_ALLOW_AMBIENT_CONTEXT=1`:
```
refusing to launch lane pods on the ambient kube-context — set RESOLUTO_SANDBOX_KUBECONTEXT, or
RESOLUTO_SANDBOX_ALLOW_AMBIENT_CONTEXT=1 to override
```
Rationale: an unpinned current-context can wander to an unrelated (even production) cluster and launch
adversarial lane pods there. Missing/empty local kube-config → in-cluster fallback (that path is allowed).

**dind storage driver note.** `flavor="dind"` runs privileged (GUEST-scoped under Kata — host stays
unprivileged) with an emptyDir docker graph at `/var/lib/docker`. `graph_backend`:
- `tmpfs` (default) — `emptyDir{medium: Memory, sizeLimit=docker_graph_size}` (default `16Gi`). RAM-backed;
  **counted WITHIN the pod's memory cgroup** (not additive). overlay2 on tmpfs is proven. On Kata the
  virtiofs rootfs does NOT work for the graph — vfs exhausts host-side fd handles and overlay2/
  fuse-overlayfs fail; **tmpfs is the only non-virtiofs fallback**. `_preflight_memory` refuses a launch
  where `graph_size >= pod_memory` or `pod_memory > node_allocatable` (distinct actionable messages).
- `block` — Kata maps a no-medium emptyDir to a virtio-blk device; the lane entrypoint formats it ext4
  and remounts before dockerd starts. overlay2 on ext4/virtio-blk, NO RAM tax (`docker_graph_block_size`
  default `50Gi`, off-pod-memory). Use this to avoid the tmpfs RAM tax.

`plain` flavor gets the restricted profile: `runAsNonRoot`, drop ALL caps, no privilege escalation,
`seccompProfile: RuntimeDefault`.

**Other invariants.** `automountServiceAccountToken: False`; `restartPolicy: Never`; honest
requests==limits (CPU/memory/ephemeral-storage). `activeDeadlineSeconds` only when `spec.deadline_seconds`
is set (None = no per-pod self-destruct — orphan protection is the label sweep + ownerRef GC, not a
wall-clock). `spec.scheduling_gates` are relayed VERBATIM as pod `schedulingGates` (the Kueue seam; the
substrate never constructs/names/removes a gate). GC anchor = per-run owner ConfigMap
(`run-owner-<dns_safe(run_id)>`); pods + NetworkPolicies carry an ownerReference to it so they
cascade-delete even if the dispatcher is long dead. `reap_stale_run_owners(keep_run_id, max_age_s=7200)`
backstops kill-9'd runs.

**`EgressConfig`** (`from resoluto_sandbox.runtime.k8s import EgressConfig`): `store_cidr`, `llm_cidr`,
`git_cidrs=[]`. ALL must be CIDR (`__post_init__` rejects a missing `/` — k8s ipBlock has no FQDNs;
resolve hostnames first). Builds a default-deny egress NetworkPolicy: store + LLM + each git host on
TCP/443, kube-dns on UDP/53, and `except=[169.254.169.254/32]` on every rule (blocks cloud IMDS). `None`
→ unrestricted egress (Kata kernel isolation only).

---

## 8. Admission (optional): `SandboxPool` (`pool.py`)

Platform-independent FIFO admission + global cap over a runtime. Pass it as `drive_node(..., admit=pool)`
or call directly:

```python
pool = SandboxPool(runtime, *, max_concurrent, acquire_timeout_s=600.0,
                   admission_gate=None, mem_budget_bytes=None, mem_budget_provider=None)
async with await pool.acquire(spec, on_wait=None) as lease:
    handle = lease.handle
```
- `max_concurrent` < 1 → `ValueError`. `acquire_timeout_s` is a SUBSTRATE cap on WAITING for a slot
  (distinct from the no-timeout-on-work principle); timeout → `RuntimeError` (substrate starvation).
- `admission_gate` (async `-> int` active-pod count, e.g. `runtime.count_active_pods`) replaces the
  in-process semaphore so the cap spans worker replicas (k8s API is the coordination point).
- RAM-budget gate (`mem_budget_bytes` or lazy `mem_budget_provider`): a parked caller holds NO RAM (pod
  not launched until granted), wakes event-driven on release (FIFO, no starvation). `on_wait(amount,
  available)` fires once when a caller parks. A held lane consumes zero RAM → competitors **serialize**.

---

## 9. Extending — add a backend / runtime / conduit

- New substrate (Ecs/Docker/Temporal pod placement): subclass `SandboxRuntime` (`launch`/`status`/
  `destroy`/`sweep` + optional `logs`). Reuse `drive_node` + `ChunkReader`/`ChunkShipper` unchanged.
- New `run()`-level backend: subclass `Backend` (`backends/base.py`) — same signature across backends;
  return a `RunResult`. Inject via `Sandbox(backend=YourBackend(...))`.
- New conduit: subclass `Conduit` (`put`/`get`/`list_prefix`; override `copy_prefix` for server-side
  copy) and wire it into `store_from_env()` (a new `RESOLUTO_STORE_KIND`).
- The in-pod half (`run_node_in_sandbox` / `runner_main`) is substrate-agnostic — it only needs a
  `Conduit` + the `RESOLUTO_*` env. Keep the wire JSON-on-KV so non-Python guests interoperate.

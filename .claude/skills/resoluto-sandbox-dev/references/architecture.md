# ARCHITECTURE: the seams, composition & DI

Reference for an agent that will USE or EXTEND this sandbox in its own system. Terse, API-exact, copy-pasteable. Verify against source if in doubt — every signature below is from the live code.

## The one composition rule

`Sandbox` is a thin facade. It HOLDS one injected `Backend` and DELEGATES every call to it. No substrate logic lives in the facade — it only selects/holds a backend and forwards `run(...)`.

ONE `SubstrateBackend` drives both presets. The only thing that varies is the injected `SandboxRuntime`.

```python
from resoluto_sandbox.client import Sandbox

# select by name (presets)
sb = Sandbox(backend="local")          # SubstrateBackend(KataNerdctlSandboxRuntime + LocalConduit)
sb = Sandbox(backend="k8s")            # SubstrateBackend(K8sSandboxRuntime + store_from_env()) — needs RESOLUTO_LANE_IMAGE

# or inject a configured SubstrateBackend (the real k8s path with egress/conduit config)
import os
from resoluto_sandbox.backends.substrate import SubstrateBackend, store_env_for_pod
from resoluto_sandbox.conduit.factory import store_from_env
from resoluto_sandbox.runtime.k8s import K8sSandboxRuntime, EgressConfig

runtime = K8sSandboxRuntime(
    namespace="resoluto-sandboxes",
    context=os.environ.get("RESOLUTO_SANDBOX_KUBECONTEXT"),
    egress=EgressConfig(store_cidr="10.0.0.5/32", llm_cidr="1.2.3.4/32"),
)
sb = Sandbox(backend=SubstrateBackend(
    runtime=runtime,
    conduit=store_from_env(),
    image="ghcr.io/you/lane:tag",
    store_env=store_env_for_pod(os.environ),
))
```

`Sandbox.__init__(*, backend: Backend | str = "local", image: str | None = None)`:
- a `Backend` instance → held as-is
- `"local"` (default) → builds `SubstrateBackend(KataNerdctlSandboxRuntime + LocalConduit)`, image `resoluto-sandbox-base:dev`
- `"k8s"` → builds `SubstrateBackend(K8sSandboxRuntime + store_from_env())` (needs `RESOLUTO_LANE_IMAGE`)
- anything else (including `"docker"`) → `ValueError`

## The run API (identical across backends)

```python
RunResult = sb.run(
    argv,                              # Sequence[str], the program + args
    *,
    workspace: str | None = None,      # program cwd (a directory); staged into sandbox
    stdin: str | bytes | None = None,  # NOT SUPPORTED — NotImplementedError on both backends
    env: dict[str, str] | None = None, # overlays sandbox env
    output_paths: Sequence[str] | None = None,  # globs → RunResult.artifacts
    stream: IO[str] | None = None,     # live output sink (default sys.stdout)
)
```

`RunResult` (pydantic `BaseModel`):

| field | type | meaning |
|-------|------|---------|
| `exit_code` | `int` | process exit code |
| `output` | `str` | program's answer. **Both backends:** MERGED stdout+stderr |
| `errors` | `str` | **always empty by design** (merged into output) |
| `artifacts` | `list[str]` | collected `output_paths` |
| `result` | `dict \| None` | parsed `result.json` if the program wrote one, else `None` |
| `reason` | `str` | substrate forensics (evicted/OOMKilled pod, …); empty for local |
| `ok` | `bool` (property) | `exit_code == 0` |

The program you run is plain: it reads argv, writes stdout/files, and never imports `resoluto_sandbox`. A program that runs as `uv run agent.py` on your machine runs unchanged under `run()`.

Dependencies are your program's concern — put `uv run`/`pip install` in your argv, or use a prebuilt image.

## The three seams (ABCs)

### 1. `Backend` — `resoluto_sandbox.backends.base`

The substrate seam. One abstract method; inputs/outputs identical across implementations.

```python
class Backend(ABC):
    @abstractmethod
    def run(self, argv, *, workspace=None, stdin=None, env=None,
            output_paths=None, stream=None) -> RunResult: ...
```

One implementation drives both presets:
- **`SubstrateBackend(*, runtime, conduit, image, store_env)`** — fully implemented: launches a sandbox via the injected `SandboxRuntime`, stages workspace, tails Conduit for output, fetches artifacts. `stdin is not None` → `NotImplementedError`. `image` is required. `run()` calls `asyncio.run(...)` internally (sync surface, async core).
  - With `KataNerdctlSandboxRuntime` → local preset (Kata microVM via nerdctl + a dedicated containerd, VM-grade isolation)
  - With `K8sSandboxRuntime` → k8s preset (Kata microVM, hardware isolation + optional egress)

**Footguns:**
- `stdin is not None` → `NotImplementedError` on BOTH presets
- `image` missing → `ValueError`

Everything else works. It is NOT a roadmap stub.

### 2. `Conduit` — `resoluto_sandbox.contracts`

The host↔sandbox exchange: a durable key/value rendezvous. The sandbox self-reports append-only immutable JSONL chunk objects under its prefix; the host tails via `list_prefix` + whole-object `get`. No in-sandbox server, no long-lived stream.

```python
class Conduit(ABC):
    @abstractmethod
    async def put(self, key: str, data: bytes) -> None: ...
    @abstractmethod
    async def get(self, key: str) -> bytes: ...
    @abstractmethod
    async def list_prefix(self, prefix: str) -> list[ObjectInfo]: ...

    async def copy_prefix(self, src_prefix: str, dst_prefix: str) -> int:
        # suffix-preserving copy of every object; returns count.
        # default round-trips bytes through get/put; override for server-side copy.
```

`ObjectInfo(key: str, size: int)`. Transport/I/O failures raise `ConduitError`.

Implementations (`resoluto_sandbox.conduit`):
- **`LocalConduit(root)`** — localfs. Proven (local backend bind-mount).
- **`StdoutConduit()`** — writes chunks to stdout. Proven (local backend path).
- **`S3Conduit(bucket, *, endpoint_url=None, region_name=None, aws_access_key_id=None, aws_secret_access_key=None, aws_session_token=None)`** — proven against minio (the k8s path). `[s3]` extra also pulls `aioboto3`; factory defaults `region_name` to `"us-east-1"` when absent.
- **`GcsConduit(bucket, *, service_file=)`** — EXPERIMENTAL / unverified. Do not rely on it.

Build one from env with `store_from_env(env=None) -> Conduit` (`resoluto_sandbox.conduit.factory`), keyed on `RESOLUTO_STORE_KIND` ∈ `stdout | localfs | s3 | gcs`. For `s3`, a JSON `RESOLUTO_STORE_WRITE_TOKEN` (prefix-scoped, write-only, expiring) overrides the static `AWS_*` / `RESOLUTO_STORE_*` vars.

### 3. `SandboxRuntime` — `resoluto_sandbox.contracts`

The isolation/placement seam. The runtime owns launch, status, destroy, sweep for a specific substrate (local Kata via nerdctl, k8s Kata, ECS, Fly, …).

```python
class SandboxRuntime(ABC):
    @abstractmethod
    async def launch(self, spec: SandboxLaunchSpec) -> SandboxHandle: ...
    @abstractmethod
    async def status(self, handle: SandboxHandle) -> SandboxStatus: ...
    @abstractmethod
    async def destroy(self, handle: SandboxHandle) -> None: ...
    @abstractmethod
    async def sweep(self, labels: dict[str, str]) -> int: ...   # leak backstop
    async def logs(self, handle, *, tail=200) -> str: ...        # forensic only; untrusted
```

Implementations:
- **`KataNerdctlSandboxRuntime`** (`runtime/kata_nerdctl.py`) — local preset; each sandbox is a Kata microVM launched via `nerdctl` against a dedicated, standalone containerd (own socket/root `/run/resoluto-local/containerd/`). Build it with `KataNerdctlSandboxRuntime.from_env(...)`; default image `DEFAULT_LOCAL_IMAGE = "resoluto-sandbox-base:dev"`.
- **`K8sSandboxRuntime(*, namespace="resoluto-sandboxes", kubeconfig=None, context=None, image_pull_policy="IfNotPresent", egress=None, node_allocatable_memory=None)`** — k8s preset; Kata pod. **Footgun:** `context` PINS the kube context — leave it `None` only knowingly (None follows the ambient current-context, which can wander to another cluster).

`SubstrateBackend` drives the runtime for you. Reach for `SandboxRuntime` directly only when building a new placement substrate.

## pydantic-only core (the import-light litmus)

`contracts.py` and the backend models are pydantic `BaseModel` + ABCs with NO platform deps (no kubernetes, no boto3 at module top). Heavy clients are imported lazily INSIDE methods (`store_from_env` imports `S3Conduit` only on the `s3` branch; `K8sSandboxRuntime._client` imports `kubernetes_asyncio` lazily). Litmus: importing `resoluto_sandbox.contracts` must not pull a cloud SDK. Keep new contracts dep-light; push platform imports down into the concrete impl.

Key pydantic contracts: `RunResult`, `SandboxLaunchSpec`, `SandboxHandle`, `SandboxStatus`, `NodeResult`, `ObjectInfo`, `SpanEvent`. `EgressConfig` is a frozen `@dataclass` (not pydantic) in `runtime.k8s`.

## Where each concern lives

| concern | home |
|---------|------|
| public entrypoint / facade | `client.py` (`Sandbox`) |
| substrate seam + `RunResult` | `backends/base.py` |
| ONE backend impl (local Kata + k8s) | `backends/substrate.py` (`SubstrateBackend`) |
| host↔sandbox exchange seam | `contracts.py` (`Conduit`) + `conduit/*` |
| conduit-from-env | `conduit/factory.py` |
| placement/isolation seam | `contracts.py` (`SandboxRuntime`) |
| local Kata runtime (local preset) | `runtime/kata_nerdctl.py` (`KataNerdctlSandboxRuntime`) |
| k8s Kata runtime | `runtime/k8s.py` (`K8sSandboxRuntime`) |
| admission (WHEN) — separate from substrate (HOW) | `contracts.py` (`Admission`/`Lease`), `pool.py` |
| egress policy | `runtime/k8s.py` (`EgressConfig`) |
| wire schema | `contracts.py` (`SpanEvent`) + `spec/PROTOCOL.md` |

## Configuring the k8s backend

```python
import os
from resoluto_sandbox.backends.substrate import SubstrateBackend, store_env_for_pod
from resoluto_sandbox.conduit.s3 import S3Conduit
from resoluto_sandbox.runtime.k8s import K8sSandboxRuntime, EgressConfig

runtime = K8sSandboxRuntime(
    namespace="resoluto-sandboxes",
    context=os.environ.get("RESOLUTO_SANDBOX_KUBECONTEXT"),
    egress=EgressConfig(                         # None (default) = unrestricted egress (Kata isolation only)
        store_cidr="10.0.0.5/32",                # object store endpoint
        llm_cidr="1.2.3.4/32",                   # LLM provider API
        git_cidrs=["140.82.112.0/20"],           # default [] = no git egress
    ),
)
backend = SubstrateBackend(
    runtime=runtime,
    conduit=S3Conduit("my-bucket", endpoint_url="http://minio:9000",
                      aws_access_key_id="...", aws_secret_access_key="..."),
    image="ghcr.io/you/lane:tag",
    store_env=store_env_for_pod(os.environ),
)
from resoluto_sandbox.client import Sandbox
sb = Sandbox(backend=backend)
res = sb.run(["agent.py"], workspace="/work", output_paths=["out/*.json"])
```

`EgressConfig` applies a default-deny NetworkPolicy: allows only the declared CIDRs on TCP/443 plus kube-dns on UDP/53. All fields MUST be CIDR (`x.x.x.x/32`) — k8s ipBlock has no FQDN support, so resolve hostnames yourself; a missing `/` raises `ValueError`.

## Adding a new substrate (ECS / Temporal / Fly / …)

**Primary path:** implement `SandboxRuntime` (the isolation/placement seam) and wire it into `SubstrateBackend`. This reuses the entire store-mediated wire (runner_main, ChunkShipper/ChunkReader, staging, telemetry) and only adds the placement mechanism:

```python
from resoluto_sandbox.contracts import SandboxRuntime, SandboxLaunchSpec, SandboxHandle, SandboxStatus

class EcsRuntime(SandboxRuntime):
    async def launch(self, spec: SandboxLaunchSpec) -> SandboxHandle: ...
    async def status(self, handle: SandboxHandle) -> SandboxStatus: ...
    async def destroy(self, handle: SandboxHandle) -> None: ...
    async def sweep(self, labels: dict[str, str]) -> int: ...

# Wire it in:
SubstrateBackend(runtime=EcsRuntime(...), conduit=store_from_env(), image="...", store_env=...)
```

**Alternative:** implement the `Backend` ABC directly for a completely different run approach (no store-mediated wire):

```python
from resoluto_sandbox.backends.base import Backend, RunResult

class MyBackend(Backend):
    def run(self, argv, *, workspace=None, stdin=None, env=None,
            output_paths=None, stream=None) -> RunResult:
        ...

Sandbox(backend=MyBackend(...))   # no facade change needed
```

Reuse `Conduit` for the host↔sandbox exchange — don't invent a new transport. Keep heavy SDK imports lazy/inside methods to preserve the import-light litmus.

## Layer diagrams

### Layering (the full stack)

```
your program  (plain: reads argv -> writes stdout/files/exit; never imports resoluto_sandbox)
      |  argv / workspace                         ^  output / errors / artifacts
      v                                           |
┌─────────────────────────────────────────────────────────────┐
│ Sandbox(backend=...)            thin facade: composes + delegates
│   .run(argv, ...) -> RunResult(exit_code, output, errors, …)  │
├─────────────────────────────────────────────────────────────┤
│ SubstrateBackend (the ONE impl) ← drive_node + Conduit + runner_main
├──────────────────────────────┬──────────────────────────────┤
│ SandboxRuntime (ABC)          │  Conduit (ABC)  host<->sandbox exchange
│   KataNerdctlSandboxRuntime   │    LocalConduit (local)
│   (Kata microVM, nerdctl)     │    StdoutConduit | S3Conduit | GcsConduit(exp.)
│   K8sSandboxRuntime           │
│   (Kata microVM pod on k8s)   │
└──────────────────────────────┴──────────────────────────────┘
```

### Run flow (both backends; runtime + conduit differ)

```
   host (your process)            Conduit  (LocalConduit / S3 / …)  Sandbox (Kata microVM / Kata pod)
   ───────────────────           ───────────────────────         ──────────────────────────
   put_dir(workspace) ─────────────▶  inbox/ *.tar.gz ───────────▶  stage inputs -> /workspace
   SandboxRuntime.launch ──────────────────────────────────────────▶  runner_main starts
   tail ChunkReader  ◀───────────── events-000001.jsonl ◀──────────  ship spans + heartbeat
        (silence-watchdog; NO wall-clock timeout)
   read result.json  ◀───────────── result.json ◀─────────────────  write verdict
   fetch_outputs     ◀───────────── outbox/ *.tar.gz ◀────────────  collect output_paths
   destroy sandbox
   → RunResult(output reconstructed from chunks, exit_code, artifacts)
```

`pydantic-only contracts.py` underlies all seams (import-light: no cloud SDK at import).
`RunResult.output` carries merged stdout+stderr; `RunResult.errors` is always `""` by design.

## Cross-links

- Wire protocol, chunk/JSONL framing, `SpanEvent` semantics → `spec/PROTOCOL.md`
- Substrate internals (storage driver, dind, stepped loop, resume-via-copy_prefix) → `internals.md`
- Worker/pipeline layering and the lane seam → `../` sibling reference docs in this skill

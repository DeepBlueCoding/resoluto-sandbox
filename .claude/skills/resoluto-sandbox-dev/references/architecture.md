# ARCHITECTURE: the seams, composition & DI

Reference for an agent that will USE or EXTEND this sandbox in its own system. Terse, API-exact, copy-pasteable. Verify against source if in doubt — every signature below is from the live code.

## The one composition rule

`Sandbox` is a thin facade. It HOLDS one injected `Backend` and DELEGATES every call to it. No substrate logic lives in the facade — it only selects/holds a backend and forwards `run(...)`.

```python
from resoluto_sandbox.client import Sandbox

# select by name
sb = Sandbox(backend="local")          # LocalBackend()
sb = Sandbox(backend="k8s")            # K8sBackend() — but run() needs an image; inject instead
# or inject a configured instance (the real k8s path)
from resoluto_sandbox.backends.k8s import K8sBackend
sb = Sandbox(backend=K8sBackend(image="ghcr.io/you/lane:tag"))
```

`Sandbox.__init__(*, backend: Backend | str = "local")`:
- a `Backend` instance → held as-is
- `"local"` → `LocalBackend()`
- `"k8s"` → `K8sBackend()` (no image; you must inject `K8sBackend(image=...)` to actually run)
- anything else → `ValueError`

## The run API (identical across backends)

```python
RunResult = sb.run(
    argv,                              # Sequence[str], the program + args
    *,
    workspace: str | None = None,      # program cwd (a directory)
    stdin: str | bytes | None = None,  # fed on stdin       — LOCAL ONLY (k8s raises)
    env: dict[str, str] | None = None, # overlays host env
    output_paths: Sequence[str] | None = None,  # globs → RunResult.artifacts
    stream: IO[str] | None = None,     # live output sink (default sys.stdout)
)
```

`RunResult` (pydantic `BaseModel`):

| field | type | meaning |
|-------|------|---------|
| `exit_code` | `int` | process exit code |
| `output` | `str` | program's answer. **k8s:** MERGED stdout+stderr |
| `errors` | `str` | local only; **empty on k8s by design** (merged into output) |
| `artifacts` | `list[str]` | collected `output_paths` |
| `result` | `dict \| None` | parsed `result.json` if the program wrote one, else `None` |
| `reason` | `str` | substrate forensics (evicted/OOMKilled pod, …); empty for local |
| `ok` | `bool` (property) | `exit_code == 0` |

The program you run is plain: it reads argv/stdin, writes stdout/files, and never imports `resoluto_sandbox`. Guarantee: a program that runs as `uv run agent.py` on your machine runs byte-identically under `run()`.

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

Implementations:
- **`LocalBackend`** — subprocess on this host, inheriting host env (an already-logged-in agent CLI authenticates with no extra wiring). Streams live to `stream`. Honors `stdin`.
- **`K8sBackend(*, image=None, conduit=None, egress=None)`** — fully implemented: launches a real Kata pod via the internal `drive_node` primitive; in-pod runner self-reports JSONL chunks to a `Conduit`; the host tails the store and reaps. Requires `RESOLUTO_STORE_KIND` in the env. `run()` calls `asyncio.run(...)` internally (sync surface, async core).

**k8s footguns:**
- `stdin is not None` → `NotImplementedError("stdin is not supported on backend='k8s'")`
- `image is None` → `ValueError("backend='k8s' requires K8sBackend(image=...)")`

Everything else on k8s works. It is NOT a roadmap stub.

### 2. `Conduit` — `resoluto_sandbox.contracts`

The host↔sandbox exchange: a durable key/value rendezvous. The pod self-reports append-only immutable JSONL chunk objects under its prefix; the host tails via `list_prefix` + whole-object `get`. No in-sandbox server, no long-lived stream.

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
- **`LocalConduit(root)`** — localfs. Proven (local backend / localfs path).
- **`StdoutConduit()`** — writes chunks to stdout. Proven (local backend path).
- **`S3Conduit(bucket, *, endpoint_url=None, region_name=None, aws_access_key_id=None, aws_secret_access_key=None, aws_session_token=None)`** — proven against minio (the k8s path). `[s3]` extra also pulls `aioboto3`; factory defaults `region_name` to `"us-east-1"` when absent.
- **`GcsConduit(bucket, *, service_file=)`** — EXPERIMENTAL / unverified. Do not rely on it.

Build one from env with `store_from_env(env=None) -> Conduit` (`resoluto_sandbox.conduit.factory`), keyed on `RESOLUTO_STORE_KIND` ∈ `stdout | localfs | s3 | gcs`. For `s3`, a JSON `RESOLUTO_STORE_WRITE_TOKEN` (prefix-scoped, write-only, expiring) overrides the static `AWS_*` / `RESOLUTO_STORE_*` vars.

### 3. `SandboxRuntime` — `resoluto_sandbox.contracts`

The ONE platform-specific placement surface (k8s / ECS / Fly / docker). The pool owns admission/ordering; the runtime owns placement.

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

`K8sSandboxRuntime(*, namespace="resoluto-sandboxes", kubeconfig=None, context=None, image_pull_policy="IfNotPresent", egress=None, node_allocatable_memory=None)`. **Footgun:** `context` PINS the kube context — leave it `None` only knowingly (None follows the ambient current-context, which can wander to another cluster).

You normally don't touch `SandboxRuntime` directly — `K8sBackend` drives it for you. Reach for it only when building a new placement substrate.

## pydantic-only core (the import-light litmus)

`contracts.py` and the backend models are pydantic `BaseModel` + ABCs with NO platform deps (no kubernetes, no boto3 at module top). Heavy clients are imported lazily INSIDE methods (`store_from_env` imports `S3Conduit` only on the `s3` branch; `K8sSandboxRuntime._client` imports `kubernetes_asyncio` lazily). Litmus: importing `resoluto_sandbox.contracts` must not pull a cloud SDK. Keep new contracts dep-light; push platform imports down into the concrete impl.

Key pydantic contracts: `RunResult`, `SandboxLaunchSpec`, `SandboxHandle`, `SandboxStatus`, `NodeResult`, `ObjectInfo`, `SpanEvent`. `EgressConfig` is a frozen `@dataclass` (not pydantic) in `runtime.k8s`.

## Where each concern lives

| concern | home |
|---------|------|
| public entrypoint / facade | `client.py` (`Sandbox`) |
| substrate seam + `RunResult` | `backends/base.py` |
| local subprocess | `backends/local.py` |
| Kata pod via `drive_node` | `backends/k8s.py` |
| host↔pod exchange seam | `contracts.py` (`Conduit`) + `conduit/*` |
| conduit-from-env | `conduit/factory.py` |
| placement seam | `contracts.py` (`SandboxRuntime`) + `runtime/k8s.py` |
| admission (WHEN) — separate from substrate (HOW) | `contracts.py` (`Admission`/`Lease`), `pool.py` |
| egress policy | `runtime/k8s.py` (`EgressConfig`) |
| wire schema | `contracts.py` (`SpanEvent`) + `spec/PROTOCOL.md` |

## Configuring the k8s backend

```python
from resoluto_sandbox.backends.k8s import K8sBackend
from resoluto_sandbox.runtime.k8s import EgressConfig
from resoluto_sandbox.conduit.s3 import S3Conduit

backend = K8sBackend(
    image="ghcr.io/you/lane:tag",
    conduit=S3Conduit("my-bucket", endpoint_url="http://minio:9000",
                      aws_access_key_id="...", aws_secret_access_key="..."),
    egress=EgressConfig(                         # None (default) = unrestricted egress (Kata isolation only)
        store_cidr="10.0.0.5/32",                # object store endpoint
        llm_cidr="1.2.3.4/32",                   # LLM provider API
        git_cidrs=["140.82.112.0/20"],           # default [] = no git egress
    ),
)
sb = Sandbox(backend=backend)
res = sb.run(["agent.py"], workspace="/work", output_paths=["out/*.json"])
```

`EgressConfig` applies a default-deny NetworkPolicy: allows only the declared CIDRs on TCP/443 plus kube-dns on UDP/53. All fields MUST be CIDR (`x.x.x.x/32`) — k8s ipBlock has no FQDN support, so resolve hostnames yourself; a missing `/` raises `ValueError`.

## Adding a new substrate (Ecs / Docker / Temporal / …)

Implement **`Backend`** — that's the whole job. One method:

```python
from resoluto_sandbox.backends.base import Backend, RunResult

class EcsBackend(Backend):
    def __init__(self, *, image: str, conduit=None, ...): ...
    def run(self, argv, *, workspace=None, stdin=None, env=None,
            output_paths=None, stream=None) -> RunResult:
        # run argv; collect output_paths into artifacts; parse result.json into .result
        # raise NotImplementedError for genuinely unsupported kwargs (don't silently drop)
        ...

Sandbox(backend=EcsBackend(image="..."))   # no facade change needed
```

If your substrate also needs novel placement, implement `SandboxRuntime` too and drive it from your `Backend`. Reuse `Conduit` for the host↔sandbox exchange — don't invent a new transport. Keep heavy SDK imports lazy/inside methods to preserve the import-light litmus.

## Layer diagram

```
            your system  (calls sb.run(argv, ...))
                  │
                  ▼
        ┌───────────────────┐
        │      Sandbox      │  facade — HOLDS one Backend, DELEGATES run()
        │     (client.py)   │  no substrate logic here
        └─────────┬─────────┘
                  │ self._backend.run(...)
        ┌─────────▼─────────┐
        │   Backend (ABC)   │  run(argv, ...) -> RunResult        [SEAM 1]
        └─────────┬─────────┘
       ┌──────────┴───────────┐
       ▼                      ▼
 LocalBackend            K8sBackend(image=, conduit=, egress=)
 subprocess              Kata pod via drive_node
 stdin OK                (no stdin — bake deps into image)
       │                      │ tail / reap
       │                      ▼
       │            ┌───────────────────┐
       │            │  Conduit (ABC)    │ put/get/list_prefix/copy_prefix  [SEAM 2]
       │            │  host↔sandbox     │ Local·Stdout·S3(minio)·GCS(exp)
       │            └─────────┬─────────┘
       │                      ▼
       │            ┌───────────────────┐
       │            │ SandboxRuntime ABC│ launch/status/destroy/sweep      [SEAM 3]
       │            │  (placement)      │ K8sSandboxRuntime
       │            └───────────────────┘
       ▼
 host process (localfs / stdout conduit)

 pydantic-only contracts.py underlies all three seams (import-light: no cloud SDK at import)
```

## Cross-links

- Wire protocol, chunk/JSONL framing, `SpanEvent` semantics → `spec/PROTOCOL.md`
- Substrate internals (storage driver, dind, stepped loop, resume-via-copy_prefix) → `internals.md`
- Worker/pipeline layering and the lane seam → `../` sibling reference docs in this skill

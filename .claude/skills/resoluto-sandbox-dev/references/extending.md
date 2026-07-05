# EXTENDING: add a SandboxRuntime / Backend / Conduit

Three extension seams. A **SandboxRuntime** owns the isolation/placement for a new substrate
(ECS, Fly, Temporal, a custom Docker wrapper, …) and wires into `SubstrateBackend` — this is
the primary path for adding a new place-to-run. A **Backend** runs a program and returns a
`RunResult` — use this for a completely new run approach that bypasses the store-mediated wire.
A **Conduit** is the durable key/value rendezvous (localfs, S3-on-minio, GCS…) the substrate
backend uses to ship inputs/outputs/telemetry. They are orthogonal.

Cross-links: store-mediated wire protocol → `../../../../spec/PROTOCOL.md`. Layering/usage →
`../SKILL.md` (SKILL body) and sibling reference docs in this dir.

---

## The public surface (verbatim)

```python
from resoluto_sandbox.client import Sandbox

Sandbox(backend="local" | "k8s" | <Backend instance>).run(   # default "local"
    argv,                       # Sequence[str] — the program + args
    *,
    workspace=None,             # str | None — a directory staged at /workspace; None = nothing staged (not cwd)
    stdin=None,                 # NOT SUPPORTED — NotImplementedError on both backends
    env=None,                   # dict[str, str] | None — overlays sandbox env
    output_paths=None,          # Sequence[str] | None — globs, collected into artifacts
    stream=None,                # IO[str] | None — live output sink (default sys.stdout)
    egress=None,                # Sequence[str] | None — per-run domain allowlist (local); None/[] = deny all but DNS+store
) -> RunResult
```

`RunResult` (pydantic, `backends/base.py`):

```python
class RunResult(BaseModel):
    exit_code: int
    output: str
    errors: str
    artifacts: list[str] = []     # resolved output_paths
    result: dict | None = None    # parsed result.json if the program wrote one
    reason: str = ""              # substrate forensics (e.g. OOMKilled pod); "" for local
    @property
    def ok(self) -> bool: return self.exit_code == 0
```

`Sandbox.__init__` accepts a `Backend` instance OR the strings `"local"` / `"k8s"` (default `"local"`). A
configured k8s backend (image, conduit, egress) MUST be injected as an instance.

### Backend status (honest)
- **`local`** — `SubstrateBackend(KataNerdctlSandboxRuntime + LocalConduit)`: each sandbox is a Kata
  microVM (hardware-virtualized) via `nerdctl` against a dedicated, standalone containerd
  (own socket/root `/run/resoluto-local/containerd/`) on this host — VM-grade isolation at parity with
  k8s, single host, no cluster. The egress canary RUNS (fail-closed); local egress is enforced HOST-SIDE
  on the lane CNI bridge (default-deny). Needs `/dev/kvm`, `nerdctl`, the dedicated containerd up
  (`scripts/local-backend-up.sh`) + an image (default `resoluto-sandbox-base:dev`).
  `stdin` raises `NotImplementedError`. `RunResult.errors` is always `""`.
- **`k8s`** — `SubstrateBackend(K8sSandboxRuntime + store_from_env())`: **fully implemented**:
  launches a real Kata pod via `drive_node`, stages the workspace in, fetches `output_paths`
  back out, reaps. `stdin` raises `NotImplementedError`. `RunResult.errors` is always `""`.

Dependencies are your program's concern — put `uv run`/`pip install` in your argv, or use a prebuilt image.

---

## Add a new SandboxRuntime (primary extension path)

This reuses the entire store-mediated wire (runner_main, ChunkShipper/ChunkReader, staging,
telemetry) and only adds the new placement mechanism. Implement `SandboxRuntime` and wire it
into `SubstrateBackend`.

### Contract (`contracts.py`)

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
    async def logs(self, handle, *, tail=200) -> str: ...        # forensic only; optional override
```

`SandboxLaunchSpec` (pydantic): `image`, `flavor`, `env`, `args`, `store_prefix`, `labels`, plus
optional k8s fields (`scheduling_gates`, `deadline_seconds`, etc.). `SandboxHandle`: opaque
identifier returned by `launch`, passed to `status`/`destroy`/`logs`. `SandboxStatus`: `phase`
string (`"pending"` / `"running"` / `"succeeded"` / `"failed"` / `"unknown"`), optional `reason`.

### Steps

1. Implement the four abstract methods.
2. Wire it into `SubstrateBackend`:
   ```python
   from resoluto_sandbox.backends.substrate import SubstrateBackend, store_env_for_pod
   from resoluto_sandbox.conduit.factory import store_from_env

   backend = SubstrateBackend(
       runtime=MyRuntime(...),
       conduit=store_from_env(),   # or inject a Conduit directly
       image="my-image:tag",
       store_env=store_env_for_pod(os.environ),  # or {"RESOLUTO_STORE_KIND": "localfs", ...}
   )
   Sandbox(backend=backend)
   ```
3. Keep heavy SDK imports LAZY — inside methods, never at module top.

### The DockerBackend example (illustrative — not a new Backend)

The REAL local backend is `SubstrateBackend(KataNerdctlSandboxRuntime + LocalConduit)` which uses the
full store-mediated wire (runner_main, telemetry, staging). The simple Docker wrapper below is
illustrative for understanding the seam, but lacks the telemetry wire — it won't produce
`result.json` or span events:

```python
# resoluto_sandbox/backends/docker_simple.py  — ILLUSTRATIVE ONLY
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import IO, Sequence

from resoluto_sandbox.backends.artifacts import _collect, read_result_json
from resoluto_sandbox.backends.base import Backend, RunResult


class SimpleDockerBackend(Backend):
    """Runs argv in a container via direct docker run (no telemetry wire).
    For production use, prefer SubstrateBackend(KataNerdctlSandboxRuntime + LocalConduit)."""

    def __init__(self, *, image: str) -> None:
        self._image = image

    def run(
        self,
        argv: Sequence[str],
        *,
        workspace: str | None = None,
        stdin: str | bytes | None = None,
        env: dict[str, str] | None = None,
        output_paths: Sequence[str] | None = None,
        stream: IO[str] | None = None,
        egress: Sequence[str] | None = None,   # per-run domain allowlist (None/[] = deny)
    ) -> RunResult:
        cwd = Path(workspace).resolve() if workspace else Path.cwd()
        if not cwd.is_dir():
            raise NotADirectoryError(f"workspace is not a directory: {cwd}")

        docker_argv = ["docker", "run", "--rm", "-w", "/workspace",
                       "-v", f"{cwd}:/workspace"]
        for k, v in (env or {}).items():
            docker_argv += ["-e", f"{k}={v}"]
        docker_argv += [self._image, *argv]

        sink = stream if stream is not None else sys.stdout
        proc = subprocess.run(docker_argv, capture_output=True, text=True)
        sink.write(proc.stdout); sink.flush()

        return RunResult(
            exit_code=proc.returncode,
            output=proc.stdout,
            errors=proc.stderr,
            artifacts=_collect(cwd, output_paths),
            result=read_result_json(cwd),
        )
```

For ECS/Fly/Temporal that go through the store-mediated path, mirror `SubstrateBackend._run_async`
→ `drive_node`: stage the workspace into a conduit prefix with `staging.put_dir`, launch via your
`SandboxRuntime`, then `staging.fetch_outputs` + `_collect` + `read_result_json`. See
`spec/PROTOCOL.md` for the JSONL self-report wire.

---

## Add a new Backend

For a completely new run approach that does NOT use the store-mediated wire, subclass `Backend` directly.

### Contract
Subclass `resoluto_sandbox.backends.base.Backend` and implement the single
`run(...)` method with the EXACT signature above, returning a `RunResult`.

### Steps
1. Implement `run`: resolve `workspace` as the cwd, execute `argv` in your
   substrate, capture exit code + output/errors, tee to `stream` (default
   `sys.stdout`) if you want live output.
2. Reuse the shared artifact helpers from `backends/artifacts.py`:
   - `_collect(cwd: Path, output_paths) -> list[str]` — glob-resolves
     `output_paths` (recursive) under `cwd` into a sorted path list.
   - `read_result_json(cwd: Path) -> dict | None` — parses `result.json` under
     `cwd` if present (filename is `telemetry.RESULT_FILENAME`).
3. Return `RunResult(exit_code=, output=, errors=, artifacts=, result=, reason=)`.
   Put substrate forensics (eviction/OOM/terminated reason) in `reason`.
4. Inject it: `Sandbox(backend=YourBackend(...))`. No registry, no string name —
   instances are passed directly. (Only `"local"`/`"k8s"` are name-resolvable in
   `client.py`; do not add new string names — inject the instance.)

### Footguns
- Honor `stream`: default to `sys.stdout` when `None`, never silently drop output.
- `child_env = {**os.environ, **env}` — `env` OVERLAYS the host env, it does not
  replace it.
- Validate `workspace` is a directory and fail loud (`NotADirectoryError`) — no
  silent fallback to cwd if the caller passed a bad path.
- Keep heavy/substrate deps LAZY — import the cloud SDK / k8s driver INSIDE
  `run`/a helper, never at module top. Keep the local path import-light.

---

## Add a new Conduit

A Conduit is the durable rendezvous the store-mediated path reads and writes
(telemetry chunks, staged inputs, fetched outputs, resume copies). Conduit is in `contracts.py`.

### Status of shipped conduits
- **`LocalConduit`** (`conduit/local.py`) — localfs, atomic writes. **Proven**
  with the local/store-mediated path.
- **`StdoutConduit`** (`conduit/stdout.py`) — write-only; `get`/`list_prefix`/
  `copy_prefix` all raise `NotImplementedError`. Surfaces events live on stdout.
- **`S3Conduit`** (`conduit/s3.py`, `[s3]` extra, lazy `aioboto3`) — **proven**
  against minio for the k8s backend.
- **`GcsConduit`** (`conduit/gcs.py`) — **experimental / unverified**. Wired into
  the factory but not exercised in the live loop. Treat as a starting point.

### Contract (`contracts.py`)

```python
class Conduit(ABC):
    @abstractmethod
    async def put(self, key: str, data: bytes) -> None: ...
    @abstractmethod
    async def get(self, key: str) -> bytes: ...
    @abstractmethod
    async def list_prefix(self, prefix: str) -> list[ObjectInfo]: ...

    async def copy_prefix(self, src_prefix: str, dst_prefix: str) -> int:
        """Default: round-trips bytes via get/put (suffix-preserving), returns count.
        No-ops cleanly on an empty src. Override for server-side copy."""
```

`ObjectInfo(key: str, size: int)`. `ConduitError` is the substrate-failure type:
raise it for real transport/I/O failures (disk full, connection refused,
timeout) so the upstream layer can attribute infrastructure errors. The bucket /
fs root is the store root; keys are full keys (no per-call root prefix).

### Steps
1. Implement the three abstract methods (`put`, `get`, `list_prefix`).
2. Either inherit the default `copy_prefix` (correct but round-trips bytes
   through the host) or override it for server-side copy — `LocalConduit`
   overrides with path-level `shutil.copy2` to avoid buffering large objects into
   RAM; `S3Conduit` overrides with `CopyObject`.
3. Keep the cloud SDK import LAZY — inside `__init__`/methods, never at module
   top — so importing `resoluto_sandbox` never pulls boto3/google-cloud.
4. Register a `kind` in `conduit/factory.py::store_from_env`, reading your config
   from `RESOLUTO_STORE_*` env vars. The factory is BOTH the host-side and in-sandbox
   entry point — the sandbox builds the same conduit from env, so config must travel
   as env. The k8s backend forwards `RESOLUTO_STORE_*` (and the prefix-scoped
   `RESOLUTO_STORE_WRITE_TOKEN`) into the pod.
5. Inject it directly when you want to bypass env:
   `Sandbox(backend=SubstrateBackend(..., conduit=YourConduit(...)))`.

### Footguns
- Atomicity: a listed object MUST be fully durable. `LocalConduit` writes to a
  `.tmp-partial` then `os.replace` (atomic) so `list_prefix` never returns a
  partial chunk. Replicate this guarantee in your backend.
- `list_prefix` MUST exclude in-flight temp objects and return keys relative to
  the store root (parity with `LocalConduit`/`S3Conduit`).
- Reject path traversal in key→path mapping (`LocalConduit._path` rejects keys
  that escape the root).
- Wrap genuine I/O failures as `ConduitError`, not bare `OSError`/SDK errors, so
  attribution works upstream. Do NOT swallow them.

### Worked minimal Conduit (Redis)

```python
# resoluto_sandbox/conduit/redis.py
from __future__ import annotations

from resoluto_sandbox.contracts import Conduit, ConduitError, ObjectInfo


class RedisConduit(Conduit):
    """Keys live under a Redis hash-per-prefix. Lazy redis import."""

    def __init__(self, url: str) -> None:
        import redis.asyncio as redis  # lazy — keep the import off module top
        self._r = redis.from_url(url)

    async def put(self, key: str, data: bytes) -> None:
        try:
            await self._r.set(key, data)
        except Exception as exc:
            raise ConduitError(f"redis put failed (key={key}): {exc}") from exc

    async def get(self, key: str) -> bytes:
        data = await self._r.get(key)
        if data is None:
            raise KeyError(key)
        return data

    async def list_prefix(self, prefix: str) -> list[ObjectInfo]:
        out: list[ObjectInfo] = []
        async for k in self._r.scan_iter(match=f"{prefix.rstrip('/')}*"):
            v = await self._r.get(k)
            out.append(ObjectInfo(key=k.decode(), size=len(v or b"")))
        return sorted(out, key=lambda o: o.key)

    # copy_prefix inherited from Conduit (get/put round-trip) — fine for Redis.
```

Register the kind (`conduit/factory.py::store_from_env`):

```python
    if kind == "redis":
        from resoluto_sandbox.conduit.redis import RedisConduit
        return RedisConduit(env["RESOLUTO_STORE_URL"])
```

Then `RESOLUTO_STORE_KIND=redis RESOLUTO_STORE_URL=redis://...` selects it for
both host and sandbox, or inject directly:
`Sandbox(backend=SubstrateBackend(..., conduit=RedisConduit("redis://...")))`.

---

## k8s backend config knobs (reference)

```python
import os
from resoluto_sandbox.backends.substrate import SubstrateBackend, store_env_for_pod
from resoluto_sandbox.conduit.factory import store_from_env
from resoluto_sandbox.runtime.k8s import K8sSandboxRuntime, EgressConfig

runtime = K8sSandboxRuntime(
    namespace="resoluto-sandboxes",
    context=os.environ.get("RESOLUTO_SANDBOX_KUBECONTEXT"),
    egress=None,                 # None → unrestricted egress (Kata kernel isolation only)
                                 # EgressConfig(...) → default-deny, allow declared CIDRs on TCP/443 + kube-dns
)
backend = SubstrateBackend(
    runtime=runtime,
    conduit=store_from_env(),    # needs RESOLUTO_STORE_KIND
    image="registry/lane:tag",   # REQUIRED
    store_env=store_env_for_pod(os.environ),
)
```

Pod placement reads env: `RESOLUTO_SANDBOX_NAMESPACE` (default
`resoluto-sandboxes`), `RESOLUTO_SANDBOX_KUBECONTEXT`,
`RESOLUTO_LANE_IMAGE_PULL_POLICY` (default `IfNotPresent`). Pods run
`runtime_class="kata"`; `check_runtime_class_guard` refuses ANY non-Kata `runtime_class`
UNCONDITIONALLY — VM-grade isolation is required, there is no trusted-local bypass. Host
`AWS_*` creds are NOT forwarded to the untrusted pod — production uses the prefix-scoped
`RESOLUTO_STORE_WRITE_TOKEN`.
Wire/staging details → `spec/PROTOCOL.md`.

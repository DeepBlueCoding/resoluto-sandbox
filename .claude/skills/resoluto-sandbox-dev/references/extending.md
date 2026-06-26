# EXTENDING: add a Backend / add a Conduit

Two extension seams. A **Backend** runs a program and returns a `RunResult` (the
substrate: subprocess, Kata pod, Docker, ECS…). A **Conduit** is the durable
key/value rendezvous (localfs, S3-on-minio, GCS…) the k8s backend uses to ship
inputs/outputs/telemetry. They are orthogonal: pick a backend, then (for `k8s`)
pick a conduit.

Cross-links: store-mediated wire protocol → `../../../../spec/PROTOCOL.md`. Layering/usage →
`../SKILL.md` (SKILL body) and sibling reference docs in this dir.

---

## The public surface (verbatim)

```python
from resoluto_sandbox.client import Sandbox

Sandbox(backend="local" | "k8s" | <Backend instance>).run(
    argv,                       # Sequence[str] — the program + args
    *,
    workspace=None,             # str | None — program cwd (a directory)
    stdin=None,                 # str | bytes | None
    env=None,                   # dict[str, str] | None — overlays host env
    output_paths=None,          # Sequence[str] | None — globs, collected into artifacts
    stream=None,                # IO[str] | None — live stdout sink (default sys.stdout)
    deps=None,                  # Deps | None — dependency strategy (local only)
) -> RunResult
```

`RunResult` (pydantic, `backends/base.py`):

```python
class RunResult(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    artifacts: list[str] = []     # resolved output_paths
    result: dict | None = None    # parsed result.json if the program wrote one
    reason: str = ""              # substrate forensics (e.g. OOMKilled pod); "" for local
    @property
    def ok(self) -> bool: return self.exit_code == 0
```

`Sandbox.__init__` accepts a `Backend` instance OR the strings `"local"` /
`"k8s"`. A configured k8s backend (image, conduit, egress) MUST be injected as an
instance — `Sandbox(backend=K8sBackend(image=...))`. The bare string `"k8s"`
constructs `K8sBackend()` with no image and fails at `run()`.

### Backend status (honest)
- **`local`** (`LocalBackend`) — runs `argv` as a host subprocess. NO isolation.
  Trusted code only. Supports `stdin` and `deps`. Tees stdout/stderr live.
- **`k8s`** (`K8sBackend`) — **fully implemented**: launches a real Kata pod via
  `drive_node`, stages the workspace in, fetches `output_paths` back out, reaps.
  Two real limits, both raise `NotImplementedError`:
  - `stdin is not None` → unsupported.
  - `deps is not None` → unsupported (bake deps into the image instead).
  Also: `RunResult.stderr` is always `""` on k8s — the in-pod runner emits
  stdout+stderr merged as `log` span events, so everything lands in `stdout` by
  design (not a dropped field).

---

## Add a new Backend

### Contract
Subclass `resoluto_sandbox.backends.base.Backend` and implement the single
`run(...)` method with the EXACT signature above, returning a `RunResult`.

### Steps
1. Implement `run`: resolve `workspace` as the cwd, execute `argv` in your
   substrate, capture exit code + stdout/stderr, tee to `stream` (default
   `sys.stdout`) if you want live output.
2. Reuse the shared artifact helpers from `backends/artifacts.py`:
   - `_collect(cwd: Path, output_paths) -> list[str]` — glob-resolves
     `output_paths` (recursive) under `cwd` into a sorted path list.
   - `read_result_json(cwd: Path) -> dict | None` — parses `result.json` under
     `cwd` if present (filename is `telemetry.RESULT_FILENAME`).
3. Return `RunResult(exit_code=, stdout=, stderr=, artifacts=, result=, reason=)`.
   Put substrate forensics (eviction/OOM/terminated reason) in `reason`.
4. Inject it: `Sandbox(backend=YourBackend(...))`. No registry, no string name —
   instances are passed directly. (Only `"local"`/`"k8s"` are name-resolvable in
   `client.py`; do not add new string names — inject the instance.)

### Footguns
- Honor `stream`: default to `sys.stdout` when `None`, never silently drop output.
- `child_env = {**os.environ, **env}` — `env` OVERLAYS the host env, it does not
  replace it (match `LocalBackend`).
- Validate `workspace` is a directory and fail loud (`NotADirectoryError`) — no
  silent fallback to cwd if the caller passed a bad path.
- Keep heavy/substrate deps LAZY — import the cloud SDK / k8s driver INSIDE
  `run`/a helper, never at module top. `K8sBackend` imports `drive_node`,
  `K8sSandboxRuntime`, `store_from_env`, staging helpers all inside
  `_run_async` for exactly this reason. The local path must import nothing heavy.

### Worked minimal Backend (Docker)

```python
# resoluto_sandbox/backends/docker.py
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import IO, Sequence

from resoluto_sandbox.backends.artifacts import _collect, read_result_json
from resoluto_sandbox.backends.base import Backend, RunResult
from resoluto_sandbox.deps import Deps


class DockerBackend(Backend):
    """Runs argv in a container. Mounts `workspace` at /workspace (rw) so collected
    outputs land back on the host, matching the local/k8s backends."""

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
        deps: Deps | None = None,
    ) -> RunResult:
        if deps is not None:
            raise NotImplementedError("deps unsupported — bake them into the image")
        cwd = Path(workspace).resolve() if workspace else Path.cwd()
        if not cwd.is_dir():
            raise NotADirectoryError(f"workspace is not a directory: {cwd}")

        docker_argv = ["docker", "run", "--rm", "-w", "/workspace",
                       "-v", f"{cwd}:/workspace"]
        for k, v in (env or {}).items():
            docker_argv += ["-e", f"{k}={v}"]
        if stdin is not None:
            docker_argv.append("-i")
        docker_argv += [self._image, *argv]

        sink = stream if stream is not None else sys.stdout
        proc = subprocess.run(
            docker_argv,
            input=stdin.decode() if isinstance(stdin, bytes) else stdin,
            capture_output=True, text=True,
        )
        sink.write(proc.stdout); sink.flush()

        return RunResult(
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            artifacts=_collect(cwd, output_paths),
            result=read_result_json(cwd),
        )
```

Use it:

```python
res = Sandbox(backend=DockerBackend(image="python:3.12-slim")).run(
    ["python", "solve.py"], workspace="/abs/workspace", output_paths=["out/*.json"],
)
assert res.ok
```

For an ECS/Fly/Temporal backend that goes through the store-mediated path,
mirror `K8sBackend.run` → `drive_node`: stage the workspace into a conduit
prefix with `staging.put_dir`, launch via your `SandboxRuntime`, then
`staging.fetch_outputs` + `_collect` + `read_result_json`. See `spec/PROTOCOL.md`
for the JSONL self-report wire.

---

## Add a new Conduit

A Conduit is the durable rendezvous the k8s/store-mediated path reads and writes
(telemetry chunks, staged inputs, fetched outputs, resume copies). The local
backend does NOT use one. Conduit is in `contracts.py`.

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
   from `RESOLUTO_STORE_*` env vars. The factory is BOTH the host-side and in-pod
   entry point — the pod builds the same conduit from env, so config must travel
   as env. The k8s backend forwards `RESOLUTO_STORE_*` (and the prefix-scoped
   `RESOLUTO_STORE_WRITE_TOKEN`) into the pod.
5. Inject it directly when you want to bypass env:
   `Sandbox(backend=K8sBackend(image=..., conduit=YourConduit(...)))`. When
   `conduit` is `None`, `K8sBackend` calls `store_from_env()` (requires
   `RESOLUTO_STORE_KIND` set).

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
both host and pod, or inject directly:
`Sandbox(backend=K8sBackend(image=..., conduit=RedisConduit("redis://...")))`.

---

## k8s backend config knobs (reference)

```python
from resoluto_sandbox.backends.k8s import K8sBackend
from resoluto_sandbox.runtime.k8s import EgressConfig   # default-deny egress policy

K8sBackend(
    image="registry/lane:tag",   # REQUIRED — bare "k8s" string has none
    conduit=None,                # None → store_from_env() (needs RESOLUTO_STORE_KIND)
    egress=None,                 # None → unrestricted egress (Kata kernel isolation only)
                                 # EgressConfig(...) → default-deny, allow declared CIDRs on TCP/443 + kube-dns
)
```

Pod placement reads env: `RESOLUTO_SANDBOX_NAMESPACE` (default
`resoluto-sandboxes`), `RESOLUTO_SANDBOX_KUBECONTEXT`,
`RESOLUTO_LANE_IMAGE_PULL_POLICY` (default `IfNotPresent`). Pods run
`runtime_class="kata"`; downgrading requires `RESOLUTO_TRUSTED_LOCAL`. Host
`AWS_*` creds are NOT forwarded to the untrusted pod unless `RESOLUTO_TRUSTED_LOCAL`
is set — production uses the prefix-scoped `RESOLUTO_STORE_WRITE_TOKEN`.
Wire/staging details → `spec/PROTOCOL.md`.

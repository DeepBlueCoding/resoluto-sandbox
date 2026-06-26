# TESTING + CONTRIBUTING conventions

Agent reference for using/extending `resoluto-sandbox`. Action-first. Verify every signature against the cited source before relying on it.

Cross-links (do not duplicate): wire protocol → `../../../../spec/PROTOCOL.md`; auth → `../../../../docs/auth.md`; concepts → `../../../../docs/concepts.md`; networking → `../../../../docs/networking.md`.

---

## Public API (verbatim — `src/resoluto_sandbox/client.py`, `backends/base.py`)

```python
from resoluto_sandbox import Sandbox, RunResult
from resoluto_sandbox.backends.k8s import K8sBackend
from resoluto_sandbox.runtime.k8s import EgressConfig
from resoluto_sandbox.deps import Deps

Sandbox(*, backend: "Backend | str" = "local")          # "local" | "k8s" | Backend instance
  .run(
      argv: Sequence[str],
      *,
      workspace: str | None = None,        # program cwd; k8s mounts it at /workspace
      stdin: str | bytes | None = None,    # local only — k8s raises NotImplementedError
      env: dict[str, str] | None = None,   # overlays host env
      output_paths: Sequence[str] | None = None,  # globs collected into RunResult.artifacts
      stream: IO[str] | None = None,       # live stdout sink; default sys.stdout
      deps: Deps | None = None,            # local only — k8s raises NotImplementedError
  ) -> RunResult
```

```python
class RunResult(BaseModel):
    exit_code: int
    stdout: str
    stderr: str                 # k8s: ALWAYS "" — runner merges stdout+stderr into stdout (by design)
    artifacts: list[str] = []   # collected output_paths, abs paths under workspace
    result: dict | None = None  # parsed result.json if the program wrote one, else None
    reason: str = ""            # substrate forensics (evicted/OOMKilled/observed_phase); "" for local
    @property
    def ok(self) -> bool        # exit_code == 0
```

Backend selection: `Sandbox(backend="local")` → `LocalBackend()`; `Sandbox(backend="k8s")` → `K8sBackend()` (no image → `.run` raises `ValueError`); any unknown string → `ValueError`.

### k8s config — inject a configured backend
```python
from resoluto_sandbox.backends.k8s import K8sBackend
from resoluto_sandbox.runtime.k8s import EgressConfig
from resoluto_sandbox.conduit.s3 import S3Conduit

sb = Sandbox(backend=K8sBackend(
    image="<lane-image>",          # REQUIRED for k8s; .run() raises ValueError if None
    conduit=None,                  # None → store_from_env() (RESOLUTO_STORE_KIND)
    egress=None,                   # None → unrestricted egress (Kata kernel isolation only)
))
```

`EgressConfig` (a dataclass, `runtime/k8s.py`) — default-deny NetworkPolicy, allows only declared CIDRs on TCP/443 + kube-dns UDP/53. **CIDR notation only; no FQDNs** (resolve hostnames to IPs yourself):
```python
EgressConfig(store_cidr="10.0.0.5/32", llm_cidr="160.79.104.0/23", git_cidrs=["140.82.112.0/20"])
# non-CIDR (missing "/") → ValueError at construction
```

### k8s real limits (NOT roadmap — the backend IS implemented via `drive_node` → real Kata pod)
- `stdin is not None` → `NotImplementedError("stdin is not supported on backend='k8s'")`
- `deps is not None` → `NotImplementedError("deps is not supported on backend='k8s' (bake them into the image)")`
- Everything else (workspace stage-in via `put_dir`, live `log` span streaming, `output_paths` fetch-out via `fetch_outputs`, `result.json` parse) works against a live pod. Requires `RESOLUTO_STORE_KIND` in env (the conduit) and a reachable k3s+Kata.

`deps` (`deps.py`, local backend only) — `Deps(kind="auto"|"inline"|"requirements"|"image"|"vendored", requirements=None)`. `auto` detects PEP-723 / `requirements.txt` / `pyproject.toml` and launches via `uv run`.

### Conduits (host↔pod rendezvous; selected by `RESOLUTO_STORE_KIND` in `conduit/factory.py`)
| kind | class | status |
|------|-------|--------|
| `stdout` | `StdoutConduit` | proven (local backend live streaming) |
| `localfs` | `LocalConduit(RESOLUTO_STORE_ROOT)` | proven (local backend) |
| `s3` | `S3Conduit` | proven against minio (k8s backend) |
| `gcs` | `GcsConduit` | **experimental / unverified** — no real-bucket integration; validated only by S3 contract parity. Do not rely on in production until run against a real bucket. |

---

## How to run tests

Config lives in `pyproject.toml` `[tool.pytest.ini_options]`: `asyncio_mode = "auto"`, `pythonpath = ["src"]`, `markers = ["integration: ..."]`, and `addopts = "-m 'not integration'"`.

```bash
# UNIT — integration is deselected by default (addopts), so this is the everyday command:
uv run pytest

# INTEGRATION — opt in explicitly; needs a LIVE k3s + Kata + minio box:
RESOLUTO_LANE_IMAGE=<lane-image> uv run pytest -m integration
```

`-m integration` tests (e.g. `tests/test_client_k8s.py::test_k8s_run_roundtrips`) round-trip `Sandbox(backend=K8sBackend(image=...)).run()` through a real Kata pod. They read `RESOLUTO_LANE_IMAGE` from env. Without the live box they will fail — that is correct; do not stub them green.

### Green-canary preflight (run BEFORE any `-m integration`)
Integration tests require a live k3s + Kata + minio cluster. Export `RESOLUTO_LANE_IMAGE` and the `RESOLUTO_STORE_*` variables before running. Unit tests need none of this — they run against stubs and the default `addopts` deselects `@integration` automatically.

RED canary (store unreachable / image missing) → fix infra first, do not run integration tests.

### Piping pytest — ALWAYS `set -o pipefail`
A test runner piped through `tail`/`head`/`grep` without `pipefail` reports the pipe's exit, masking failures as a false PASS:
```bash
set -o pipefail; uv run pytest 2>&1 | tail -40
```

---

## Invariants you MUST preserve when extending

### 1. core import stays light (pydantic-only) — litmus test
`tests/test_core_import_is_light.py` spawns a fresh interpreter, imports `resoluto_sandbox`, and **fails if any of `kubernetes_asyncio`, `aioboto3`, `botocore`, `gcloud` got pulled into `sys.modules`**. The top-level surface (`__init__.py`, `contracts.py`, `client.py`) carries no platform deps. Heavy backends import **lazily, inside functions** (see `K8sBackend._run_async` importing `K8sSandboxRuntime`/`store_from_env`/`staging` at call time, and `conduit/factory.py` importing each concrete conduit inside its branch).

Footgun: a module-top `import aioboto3` / `from kubernetes_asyncio import ...` anywhere reachable from `import resoluto_sandbox` breaks this test. Keep platform imports function-local. Optional deps gate behind extras (`[k8s]`, `[s3]`, `[gcs]` in `pyproject.toml`).

### 2. unit tests NEVER launch a pod
A non-`@integration` test must not call `K8sSandboxRuntime.launch()` / `drive_node` against a reachable cluster — it leaks unlabeled `ImagePullBackOff` pods that eat quota and starve real runs. To exercise k8s code in a unit test, stub the client/runtime; otherwise mark `@pytest.mark.integration`.

### 3. fail-fast — no fallbacks
No try/except-swallow, no default-on-missing-input, no placeholders. Mirror the codebase: `store_from_env` raises `RuntimeError` on an unknown `RESOLUTO_STORE_KIND`; `K8sBackend` raises `ValueError`/`NotImplementedError` rather than degrading; `parse_k8s_memory` rejects garbage with an anchored regex; `check_runtime_class_guard` refuses a non-Kata `runtime_class` unless `RESOLUTO_TRUSTED_LOCAL` is set. Let it crash loud at the source.

### 4. pydantic end-to-end
All wire/contract types are `pydantic.BaseModel` (`RunResult`, `SandboxLaunchSpec`, `NodeResult`, `SandboxStatus`, `SpanEvent`, `ObjectInfo`, `Deps`). No manual dict construction for these, no `.model_dump()` plumbing in the middle. Return models; let serialization happen at the edge. `RunResult.result` is an intentionally generic `dict` (parsed foreign `result.json`) — not a vocabulary the substrate owns.

### 5. no comments except minimal docstrings
Match existing style: a one-line (or short) function/class docstring stating inputs/outputs; no inline narration. Don't add explanatory comments to code.

### 6. no wall-clock timeouts on liveness
Liveness is substrate-silence + heartbeat, not a clock. `drive_node(..., dead_after_s=600.0)` is the silence watchdog (no chunk for N seconds), not a wall-clock cap. `SandboxLaunchSpec.deadline_seconds` defaults to `None` = no pod deadline. Don't introduce `wait_for(timeout=)` / `max_wall_seconds` style caps.

---

## Branch / commit / release notes

- Branch off `main`; never commit directly to `main`. One concern per branch.
- Conventional commits, e.g. `feat(backend): ...`, `fix(conduit): ...`, `test(k8s): ...`.
- A change to in-pod code (anything the lane image bakes) needs an **image rebuild + republish** — the host runs live source while pods run a baked image; they drift silently otherwise. Bump `version` in `pyproject.toml` when you cut a new wheel/image.
- Optional-dep changes: keep them in the right extra (`[k8s]`/`[s3]`/`[gcs]`) and confirm `uv run pytest tests/test_core_import_is_light.py` still passes (no leak into the light surface).
- Before opening a PR: `set -o pipefail; uv run pytest` green (units), and if you touched k8s/conduit paths, run the green-canary preflight + `uv run pytest -m integration` on the live box.

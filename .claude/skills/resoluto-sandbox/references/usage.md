# USAGE: the `Sandbox.run()` contract end-to-end

How to USE or EXTEND this sandbox from your own system. One entrypoint, one
result type, two backend presets. Plain programs in, structured results out.

Cross-links (don't duplicate these):
- Wire protocol (span events, manifest, result/task schemas): [`spec/PROTOCOL.md`](../../../../spec/PROTOCOL.md)
- Concepts and layering deep-dive: [`docs/concepts.md`](../../../../docs/concepts.md)
- Substrate internals (Kata pod, storage driver, stepped loop): the `resoluto-sandbox` SKILL.md

---

## The single entrypoint

```python
from resoluto_sandbox import Sandbox   # re-exported; canonical: resoluto_sandbox.client

result = Sandbox(backend="local").run(["agent.py", "--task", "fix the bug"], workspace="/abs/repo")
print(result.output)      # the program's answer
assert result.ok          # exit_code == 0
```

There is exactly ONE public call shape. `Sandbox(...)` holds a `Backend`; `.run(...)`
delegates to it. Everything else is a backend implementation detail.

### `Sandbox(*, backend="local"|"k8s"|<Backend>, image=None)`

```python
def __init__(self, *, backend: Backend | str = "local", image: str | None = None) -> None
```

- `backend="local"` → builds `SubstrateBackend(runtime=KataNerdctlSandboxRuntime, conduit=LocalConduit, image="resoluto-sandbox-base:dev", ...)` (default).
- `backend="k8s"`   → builds `SubstrateBackend(runtime=K8sSandboxRuntime, conduit=store_from_env(), ...)` (needs `RESOLUTO_LANE_IMAGE` and `RESOLUTO_STORE_KIND`).
- `backend=<Backend instance>` → injected as-is (the supported way to configure k8s with egress, custom conduit, etc.).
- anything else → `ValueError("unknown backend ...")`.

### `.run(argv, *, ...) -> RunResult`

```python
def run(
    self,
    argv: Sequence[str],
    *,
    workspace: str | None = None,
    stdin: str | bytes | None = None,
    env: dict[str, str] | None = None,
    output_paths: Sequence[str] | None = None,
    stream: IO[str] | None = None,
    egress: Sequence[str] | None = None,
) -> RunResult
```

| kwarg | meaning | local | k8s |
|---|---|---|---|
| `argv` | program + args | yes | yes |
| `workspace` | program cwd (must be an existing dir; staged into sandbox). `None` → `Path.cwd()` | yes | yes (staged in + mutated in place by `output_paths`) |
| `stdin` | `str`/`bytes` fed on stdin | **NotImplementedError** | **NotImplementedError** |
| `env` | overlay on top of sandbox env (`{**store_env, **env}`) | yes | yes (overlaid on the curated sandbox env) |
| `output_paths` | globs collected into `RunResult.artifacts` after the run | yes | yes |
| `stream` | live output sink; `None` → `sys.stdout` | yes | yes |
| `egress` | domains allowed for THIS run's outbound TLS (`None`/`[]` → deny all but DNS + store); set on the fly per step via the SNI proxy, cleared after | yes | (use `EgressConfig` per-runtime) |

Dependencies are your program's concern — put `uv run`/`pip install` in your argv, or use a prebuilt image.

Footgun: `workspace` that isn't a directory → `NotADirectoryError` (local). On k8s,
artifacts/`result.json` are only fetched back when BOTH `output_paths` AND `workspace`
are set — no `workspace` means nothing comes back out.

---

## `RunResult` — every field

From `resoluto_sandbox.backends.base`:

```python
class RunResult(BaseModel):
    exit_code: int
    output: str
    errors: str
    artifacts: list[str] = []
    result: dict | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:        # exit_code == 0
        return self.exit_code == 0
```

- `exit_code` — process exit code. When the runner reports no explicit code it
  is derived: `0` if status was `success`, else `1`.
- `output` — the program's output (live-teed to `stream` as it runs). **Both backends** merge
  stdout and stderr into `output` (the in-sandbox runner emits both as `log` span events).
- `errors` — **always `""` on both backends** by design (see merged-stream note above).
- `artifacts` — absolute/glob-collected paths from `output_paths` (empty if none requested).
- `result` — parsed `result.json` if the program wrote one in `workspace`, else `None`.
  (Schema: `spec/result.schema.json`.) This is how a program returns structured data
  without polluting output.
- `reason` — substrate forensics: evicted/OOMKilled/observed pod phase. k8s only;
  always `""` for local.
- `.ok` — convenience for `exit_code == 0`.

---

## Backend selection: string vs injected Backend (DI)

Two ways to pick a backend. Strings are for the trivial cases; **inject a configured
`Backend` for anything real** (this is the only way to set the k8s image/conduit/egress).

```python
# By string (no config knobs)
Sandbox(backend="local")                     # Kata microVM via nerdctl, default image
Sandbox(backend="local", image="my:img")     # Kata microVM via nerdctl, custom image
Sandbox(backend="k8s", image="<tag>")        # k8s preset — reads RESOLUTO_STORE_KIND from env

# By injection (DI) — the supported path for k8s config with egress/custom conduit
import os
from resoluto_sandbox.backends.substrate import SubstrateBackend, store_env_for_pod
from resoluto_sandbox.conduit.factory import store_from_env
from resoluto_sandbox.runtime.k8s import K8sSandboxRuntime
from resoluto_sandbox.egress import EgressConfig   # backend-neutral allowlist (k8s + local); re-exported from runtime.k8s

Sandbox(backend=SubstrateBackend(
    runtime=K8sSandboxRuntime(
        namespace="resoluto-sandboxes",
        context=os.environ.get("RESOLUTO_SANDBOX_KUBECONTEXT"),
        egress=EgressConfig(                # None → opt OUT (no NetworkPolicy, unrestricted egress)
            store_cidr="10.0.0.5/32",       # object store (k8s only); store + DNS auto-allowed; IMDS denied. SECURE BY DEFAULT
            store_port=443,                 # default 443
            allow=["anthropic", "npm", "pypi"],    # open only what's needed (least privilege)
            # public_https=True,                    # escape hatch: allow ALL :443 (trusted code)
        ),
    ),
    conduit=store_from_env(),               # or inject a Conduit instance
    image="registry.example/lane:dev",
    store_env=store_env_for_pod(os.environ),
))
```

You can also inject your own `Backend` subclass (implement `run(...) -> RunResult`); the
facade does `isinstance(backend, Backend)` and uses it directly.

---

## SubstrateBackend (local Kata) vs SubstrateBackend (k8s Kata) — behavior differences

| concern | `local` (Kata microVM via nerdctl) | `k8s` (Kata pod) |
|---|---|---|
| isolation | Kata microVM (hardware-virtualized) via nerdctl + a dedicated containerd; VM-grade, parity with k8s. Egress canary RUNS; egress enforced HOST-SIDE on the CNI bridge (default-deny: store+DNS only until you opt in via `RESOLUTO_EGRESS_ALLOW`/`_PUBLIC_HTTPS`; REJECT IMDS+RFC1918). Suitable for untrusted code. | Kata microVM (kernel isolation), curated env, optional egress NetworkPolicy. Use for untrusted/adversarial code. |
| output | captured (from `log` span events) + live-teed to `stream` | captured (from `log` span events) + live-teed to `stream` |
| errors | **always `""` by design** (runner merges both streams as `log` events) | **always `""` by design** (runner merges both streams as `log` events) |
| `result` | `result.json` read from `workspace` only if `output_paths` AND `workspace` set | `result.json` fetched back only if `output_paths` AND `workspace` set |
| `reason` | always `""` | pod forensics (OOMKilled/evicted/phase) |
| `stdin` | **NotImplementedError** | **NotImplementedError** |
| env requirement | needs `/dev/kvm` + nerdctl + the dedicated containerd + an image | `RESOLUTO_STORE_KIND` must be set (conduit from env) unless you inject `conduit=` |

k8s pod env is curated by `store_env_for_pod`: forwards `RESOLUTO_STORE_*`. Host `AWS_*`
creds are NOT forwarded — the pod authenticates to the store via the prefix-scoped
`RESOLUTO_STORE_WRITE_TOKEN`.

k8s liveness: substrate-silence watchdog (`dead_after_s=600.0` — no chunk for 600s kills);
NO wall-clock timeout on the work itself.

---

## The decoupling guarantee: same program, any backend

The program you run is PLAIN. It reads `argv`, writes `stdout`/files, optionally
drops a `result.json`. It imports NOTHING from `resoluto_sandbox`. The contract:

> A program that works as `uv run agent.py` on your machine works unchanged inside the sandbox.
> On `local` it runs in a Kata microVM via nerdctl; on `k8s` it runs in a Kata microVM pod. Same program,
> same inputs, same outputs.

Don't reach into `resoluto_sandbox` from the workload — if you find
yourself importing the package inside the program you run, you've broken the seam.

---

## Conduits (object store backends)

Built from env by `store_from_env()` (`RESOLUTO_STORE_KIND`), or injected via
`SubstrateBackend(conduit=...)`:

- `stdout` (`StdoutConduit`) — stdout-streaming wiring. **proven.**
- `localfs` (`LocalConduit`, `RESOLUTO_STORE_ROOT`) — local filesystem store (the local-backend default). **proven.**
- `s3` (`S3Conduit`) — S3/minio; uses `RESOLUTO_STORE_WRITE_TOKEN` (scoped) or
  `RESOLUTO_STORE_BUCKET`/`ENDPOINT`/`REGION` + `AWS_*`. **proven against minio (the k8s path).**
- `gcs` (`GcsConduit`, `RESOLUTO_STORE_BUCKET`, `RESOLUTO_GCS_SERVICE_FILE`) —
  **EXPERIMENTAL / unverified. Do not rely on it without testing.**

---

## Copy-paste recipes

Local run (Kata microVM via nerdctl), capture answer + an artifact:
```python
r = Sandbox(backend="local").run(
    ["analyze.py", "--input", "data.csv"],
    workspace="/abs/job",
    output_paths=["report.md", "out/*.json"],
)
print(r.output); print(r.artifacts); print(r.result)   # result.json parsed if written
```

Untrusted run in a Kata pod, with egress lockdown:
```python
import os
from resoluto_sandbox import Sandbox
from resoluto_sandbox.backends.substrate import SubstrateBackend, store_env_for_pod
from resoluto_sandbox.conduit.factory import store_from_env
from resoluto_sandbox.runtime.k8s import K8sSandboxRuntime
from resoluto_sandbox.egress import EgressConfig

runtime = K8sSandboxRuntime(
    namespace="resoluto-sandboxes",
    context=os.environ.get("RESOLUTO_SANDBOX_KUBECONTEXT"),
    egress=EgressConfig(store_cidr="10.0.0.5/32", store_port=443),   # SECURE BY DEFAULT: store + DNS only; add allow=[...] or public_https=True to open egress
)
sb = Sandbox(backend=SubstrateBackend(
    runtime=runtime,
    conduit=store_from_env(),
    image="registry.example/lane:dev",
    store_env=store_env_for_pod(os.environ),
))
r = sb.run(["agent.py", "--task", "..."], workspace="/abs/repo", output_paths=["**/*.patch"])
if not r.ok:
    print("FAILED", r.exit_code, r.reason)   # reason = pod forensics
```

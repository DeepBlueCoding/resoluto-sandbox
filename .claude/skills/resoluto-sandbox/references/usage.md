# USAGE: the `Sandbox.run()` contract end-to-end

How to USE or EXTEND this sandbox from your own system. One entrypoint, one
result type, two backends. Plain programs in, structured results out.

Cross-links (don't duplicate these):
- Wire protocol (span events, manifest, result/task schemas): [`spec/PROTOCOL.md`](../../../../spec/PROTOCOL.md)
- Concepts and layering deep-dive: [`docs/concepts.md`](../../../../docs/concepts.md)
- Substrate internals (Kata pod, storage driver, stepped loop): the `resoluto-sandbox` SKILL.md

---

## The single entrypoint

```python
from resoluto_sandbox import Sandbox   # re-exported; canonical: resoluto_sandbox.client

result = Sandbox(backend="local").run(["agent.py", "--task", "fix the bug"], workspace="/abs/repo")
print(result.stdout)      # the program's answer
assert result.ok          # exit_code == 0
```

There is exactly ONE public call shape. `Sandbox(...)` holds a `Backend`; `.run(...)`
delegates to it. Everything else is a backend implementation detail.

### `Sandbox(*, backend="local"|"k8s"|<Backend>)`

```python
def __init__(self, *, backend: Backend | str = "local") -> None
```

- `backend="local"` → constructs `LocalBackend()` (default).
- `backend="k8s"`   → constructs `K8sBackend()` (NO image → `.run()` raises `ValueError`; see DI below).
- `backend=<Backend instance>` → injected as-is (the supported way to configure k8s).
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
    deps: Deps | None = None,
) -> RunResult
```

| kwarg | meaning | local | k8s |
|---|---|---|---|
| `argv` | program + args; `argv[0]` is resolved relative to `workspace` for dep detection | yes | yes |
| `workspace` | program cwd (must be an existing dir; staged into pod for k8s). `None` → `Path.cwd()` (local) | yes | yes (staged in + mutated in place by `output_paths`) |
| `stdin` | `str`/`bytes` fed on stdin | yes | **NotImplementedError** |
| `env` | overlay on top of host env (`{**os.environ, **env}`) | yes | yes (overlaid on the curated pod env) |
| `output_paths` | globs collected into `RunResult.artifacts` after the run | yes | yes |
| `stream` | live stdout sink; `None` → `sys.stdout` | yes | yes |
| `deps` | `Deps` strategy for launching the program (see below) | yes | **NotImplementedError** (bake into image) |

Footgun: `workspace` that isn't a directory → `NotADirectoryError` (local). On k8s,
artifacts/`result.json` are only fetched back when BOTH `output_paths` AND `workspace`
are set — no `workspace` means nothing comes back out.

---

## `RunResult` — every field

From `resoluto_sandbox.backends.base`:

```python
class RunResult(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    artifacts: list[str] = []
    result: dict | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:        # exit_code == 0
        return self.exit_code == 0
```

- `exit_code` — process exit code. On k8s, when the runner reports no explicit code it
  is derived: `0` if pod status was `success`, else `1`.
- `stdout` — the program's answer (live-teed to `stream` as it runs).
- `stderr` — **local only.** On k8s this is `""` BY DESIGN (see merged-stream note below).
- `artifacts` — absolute/glob-collected paths from `output_paths` (empty if none requested).
- `result` — parsed `result.json` if the program wrote one in `workspace`, else `None`.
  (Schema: `spec/result.schema.json`.) This is how a program returns structured data
  without polluting stdout.
- `reason` — substrate forensics: evicted/OOMKilled/observed pod phase. **k8s only**;
  always `""` for local.
- `.ok` — convenience for `exit_code == 0`.

---

## Backend selection: string vs injected Backend (DI)

Two ways to pick a backend. Strings are for the trivial cases; **inject a configured
`Backend` for anything real** (this is the only way to set the k8s image/conduit/egress).

```python
# By string (no config knobs)
Sandbox(backend="local")
Sandbox(backend="k8s")        # ValueError at .run() time — no image

# By injection (DI) — the supported path for k8s config
from resoluto_sandbox.backends.k8s import K8sBackend
from resoluto_sandbox.runtime.k8s import EgressConfig

Sandbox(backend=K8sBackend(
    image="registry.example/lane:dev",
    conduit=None,                       # None → store_from_env() (RESOLUTO_STORE_KIND)
    egress=EgressConfig(                # None → unrestricted egress (Kata isolation only)
        store_cidr="10.0.0.5/32",
        llm_cidr="1.2.3.4/32",
        git_cidrs=["140.82.112.0/24"],  # optional; default [] = no git egress
    ),
))
```

`K8sBackend.__init__(*, image=None, conduit=None, egress=None)`:
- `image` REQUIRED before `.run()` — `None` → `ValueError("backend='k8s' requires K8sBackend(image=...)")`.
- `conduit` — a `Conduit` (object store). `None` → built from env via `store_from_env()`.
- `egress` — `EgressConfig` (from `resoluto_sandbox.runtime.k8s`). All fields MUST be
  CIDR notation (`x.x.x.x/32`); NetworkPolicy `ipBlock` rejects FQDNs — resolve hostnames
  to IPs yourself first or `__post_init__` raises `ValueError`. `None` → unrestricted egress.

You can also inject your own `Backend` subclass (implement `run(...) -> RunResult`); the
facade does `isinstance(backend, Backend)` and uses it directly.

---

## local vs k8s — behavior differences (read this)

| concern | `LocalBackend` | `K8sBackend` |
|---|---|---|
| isolation | NONE — host subprocess, host env inherited. Trusted code ONLY. | Kata microVM (kernel isolation), curated env, optional egress NetworkPolicy. Use for untrusted/adversarial code. |
| stdout | captured + live-teed to `stream` | captured (from `log` span events) + live-teed to `stream` |
| stderr | captured separately into `RunResult.stderr` | **MERGED into `stdout`; `RunResult.stderr == ""`** (in-pod runner emits both as `log` events) — intentional, not a dropped field |
| `result` | `result.json` read from `workspace` | `result.json` fetched back only if `output_paths` AND `workspace` set |
| `reason` | always `""` | pod forensics (OOMKilled/evicted/phase) |
| `stdin` | supported | **NotImplementedError** |
| `deps` | supported (`uv run`, `--with-requirements`, image, vendored) | **NotImplementedError** — bake deps into the image |
| env requirement | none | `RESOLUTO_STORE_KIND` must be set (conduit from env) unless you inject `conduit=` |

k8s pod env is curated by `_store_env_for_pod`: forwards `RESOLUTO_STORE_*` and
`RESOLUTO_TRUSTED_LOCAL`. Host `AWS_*` creds are NOT forwarded — the pod authenticates
to the store via the prefix-scoped `RESOLUTO_STORE_WRITE_TOKEN`. If you have no scoped
token and want to forward host AWS creds for dev, set `RESOLUTO_TRUSTED_LOCAL=1`,
otherwise `.run()` raises `RuntimeError`.

k8s liveness: substrate-silence watchdog (`dead_after_s=600.0` — no chunk for 600s kills);
NO wall-clock timeout on the work itself.

---

## The decoupling guarantee: `uv run` == sandbox

The program you run is PLAIN. It reads `argv`/`stdin`, writes `stdout`/files, optionally
drops a `result.json`. It imports NOTHING from `resoluto_sandbox`. The contract:

> a program that runs as `uv run agent.py` on your machine runs byte-identically under
> `Sandbox(...).run(["agent.py", ...])`.

This is what makes the sandbox swappable: same program, same inputs, same outputs across
`local` and `k8s`. Don't reach into `resoluto_sandbox` from the workload — if you find
yourself importing the package inside the program you run, you've broken the seam.

### `deps` (local only) — how the program is launched

`Deps(kind="auto"|"inline"|"requirements"|"image"|"vendored", requirements=None)` from
`resoluto_sandbox.deps`. `resolve_invocation` maps `(argv, deps)` to the actual launch argv:

- `auto` (default) — detect: PEP 723 inline script → `uv run`; `requirements.txt` present
  → `uv run --with-requirements`; `pyproject.toml` present → `uv run`; else run argv as-is.
- `inline` → `["uv", "run", *argv]`
- `requirements` → `["uv", "run", "--with-requirements", <workspace/requirements.txt>, *argv]`
- `image` / `vendored` → argv unchanged (deps already present)

On k8s, deps are pre-baked into the image — passing `deps` raises `NotImplementedError`.

---

## Conduits (object store backends)

Built from env by `store_from_env()` (`RESOLUTO_STORE_KIND`), or injected via
`K8sBackend(conduit=...)`:

- `stdout` (`StdoutConduit`) — local-backend wiring. **proven.**
- `localfs` (`LocalConduit`, `RESOLUTO_STORE_ROOT`) — local filesystem store. **proven.**
- `s3` (`S3Conduit`) — S3/minio; uses `RESOLUTO_STORE_WRITE_TOKEN` (scoped) or
  `RESOLUTO_STORE_BUCKET`/`ENDPOINT`/`REGION` + `AWS_*`. **proven against minio (the k8s path).**
- `gcs` (`GcsConduit`, `RESOLUTO_STORE_BUCKET`, `RESOLUTO_GCS_SERVICE_FILE`) —
  **EXPERIMENTAL / unverified. Do not rely on it without testing.**

---

## Copy-paste recipes

Trusted local run, capture answer + an artifact:
```python
r = Sandbox(backend="local").run(
    ["analyze.py", "--input", "data.csv"],
    workspace="/abs/job",
    output_paths=["report.md", "out/*.json"],
)
print(r.stdout); print(r.artifacts); print(r.result)   # result.json parsed if written
```

Untrusted run in a Kata pod, with egress lockdown:
```python
import os
os.environ["RESOLUTO_STORE_KIND"] = "s3"   # or inject conduit=
sb = Sandbox(backend=K8sBackend(
    image="registry.example/lane:dev",
    egress=EgressConfig(store_cidr="10.0.0.5/32", llm_cidr="1.2.3.4/32"),
))
r = sb.run(["agent.py", "--task", "..."], workspace="/abs/repo", output_paths=["**/*.patch"])
if not r.ok:
    print("FAILED", r.exit_code, r.reason)   # reason = pod forensics
```

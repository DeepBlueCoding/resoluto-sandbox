# OPERATIONS: CLI, images, storage, version-lock

Action-first reference for running/extending this sandbox. Verified against source:
`src/resoluto_sandbox/{cli,images,version_guard}.py`, `conduit/factory.py`,
`backends/{base,k8s}.py`, `runtime/k8s.py`, `client.py`.

Cross-links (don't duplicate): protocol/event/chunk semantics → `../../../../spec/PROTOCOL.md`.
Substrate images are `Dockerfile.base` + `images/{claude,langchain,openai}.Dockerfile`; substrate internals (storage driver, stepped loop) → [`../../resoluto-sandbox-dev/references/internals.md`](../../resoluto-sandbox-dev/references/internals.md).

---

## Backends

```
your program  (plain: reads argv/stdin -> writes stdout/files/exit; never imports resoluto_sandbox)
      |  argv / workspace                         ^  output / errors / artifacts
      v                                           |
┌─────────────────────────────────────────────────────────────┐
│ Sandbox(backend=...)            thin facade: composes + delegates
│   .run(argv, ...) -> RunResult(exit_code, output, errors, …)  │
├─────────────────────────────────────────────────────────────┤
│ Backend  (ABC)      ← the extension seam: implement to add a substrate
│    ├── LocalBackend  → subprocess on this host
│    └── K8sBackend    → composes a SandboxRuntime + a Conduit
├──────────────────────────────┬──────────────────────────────┤
│ SandboxRuntime (ABC)         │  Conduit (ABC)  host<->sandbox exchange
│   K8sSandboxRuntime          │    StdoutConduit | LocalConduit
│   (Kata microVM pod on k8s)  │    S3Conduit | GcsConduit(exp.)
└──────────────────────────────┴──────────────────────────────┘
```

| backend | isolation | where it runs | needs | use for |
|---------|-----------|---------------|-------|---------|
| `local` | none (host subprocess) | your host | nothing | dev, trusted code, fast iteration |
| `k8s` | hardware (Kata microVM) per run | a Kubernetes cluster | k8s + Kata + S3 store + image | untrusted/adversarial code, production |

`RunResult.output` and `RunResult.errors` are populated in the same fields by both backends — the
Conduit is transport; the result shape is uniform regardless of which store carries the bytes.

For the full backends guide (local detail, k8s detail, vendor-neutral k8s install guide for any
Kubernetes distribution): [`../../../../docs/backends.md`](../../../../docs/backends.md).

---

---

## 1. Public API (use this, not the CLI internals)

```python
from resoluto_sandbox.client import Sandbox  # the ONLY entrypoint

sb = Sandbox(backend="local")            # or "k8s", or a Backend instance (see §5)
result = sb.run(
    argv,                                 # Sequence[str], the program + args
    workspace=None,                       # str dir = program cwd; outputs land here in place
    stdin=None,                           # str|bytes piped to stdin   (LOCAL ONLY)
    env=None,                             # dict[str,str] overlaid on host env
    output_paths=None,                    # Sequence[str] globs collected into artifacts
    stream=None,                          # IO[str], live output (default sys.stdout)
) -> RunResult
```

`RunResult` (pydantic `BaseModel`, `backends/base.py`):

```python
exit_code: int
output: str            # program output (k8s: MERGED stdout+stderr, see §5)
errors: str            # local: program stderr; k8s: "" by design
artifacts: list[str]   # collected output_paths (absolute paths)
result: dict | None    # parsed result.json if the program wrote one, else None
reason: str            # substrate forensics (evicted/OOMKilled/observed phase); "" for local
ok -> bool             # property: exit_code == 0
```

The program you run is plain — reads argv/stdin, writes stdout/files, NEVER imports
`resoluto_sandbox`. Guarantee: a program that runs as `uv run agent.py` locally runs
byte-identically under `run()`.

Dependencies are your program's concern — put `uv run`/`pip install` in your argv, or use a prebuilt image.

---

## 2. CLI — `resoluto-sandbox` (`cli.py`)

Three subcommands: `run`, `doctor`, `image build`. Unknown/no subcommand → prints help, exit 2.

### `run`
```
resoluto-sandbox run [opts] -- <program> [args...]
```
Program argv comes AFTER `--`. Any stray arg before `--` → error, exit 2. No program → exit 2.
Exit code = the program's exit code. Streams output live to `sys.stdout`.

Flags:
| flag | default | values |
|---|---|---|
| `--backend` | `local` | `local`, `k8s` |
| `--workspace` | `None` | dir (program cwd) |
| `--image` | `None` | k8s image tag (REQUIRED for `--backend k8s`) |

```bash
# local run
resoluto-sandbox run --workspace . -- python agent.py --task build

# k8s (image REQUIRED)
resoluto-sandbox run --backend k8s --image resoluto-sandbox:0.2.3-claude -- python agent.py
```

### `doctor`
Readiness report, always exit 0. Checks: `docker` (k8s/images), `uv` (useful for Python programs),
`RESOLUTO_SANDBOX_KUBECONTEXT` (k8s). Prints `[OK]`/`[MISSING]` per check.

### `image build` (`images.py`)
```
resoluto-sandbox image build [--provider P] [--version VER] [--context PATH]
```
| flag | default | values |
|---|---|---|
| `--provider` | `claude` | `claude`, `langchain`, `openai`, `all` |
| `--version` | `None` → installed wheel version | VER string |
| `--context` | `.` | docker build context PATH |

`--provider all` builds the base ONCE, then each overlay reusing that base. Single provider
builds the base first if needed. Prints each built tag to stdout.

> FOOTGUN: the base image needs the workspace ROOT as context until this package is a
> standalone repo. Build from one level up: `--context ..`.

---

## 3. Image matrix + version-lock (`images.py`, `version_guard.py`)

```
PROVIDERS = ("claude", "langchain", "openai")
```

`image_tags(ver)` → tag map:
```
base       -> resoluto-sandbox-base:<ver>
claude     -> resoluto-sandbox:<ver>-claude
langchain  -> resoluto-sandbox:<ver>-langchain
openai     -> resoluto-sandbox:<ver>-openai
```

Build wiring:
- `build_base(*, ver=None, context=".", runner=subprocess.run) -> str`
  `docker build -f Dockerfile.base -t resoluto-sandbox-base:<ver> <context>`
- `build(provider, *, ver=None, context=".", base_tag=None, runner=subprocess.run) -> str`
  builds base first if `base_tag is None`, then
  `docker build -f images/<provider>.Dockerfile --build-arg BASE_IMAGE=<base_tag> --build-arg IMAGE_VERSION=<ver> -t resoluto-sandbox:<ver>-<provider> <context>`.
  Unknown provider → `ValueError`.
- `wheel_version() -> str` = `importlib.metadata.version("resoluto-sandbox")`. Default `ver`.
- `runner` is injectable (tests pass a fake; default `subprocess.run(..., check=True)`).

Overlay Dockerfiles live in `images/{claude,langchain,openai}.Dockerfile`; base is `Dockerfile.base`.

### Version-lock guard (`version_guard.py`)
```python
assert_image_matches_wheel(image_version: str, wheel_version: str) -> None
```
Raises `RuntimeError` if `MAJOR.MINOR` differ (patch is ignored). `_major_minor` splits on `.`,
missing minor defaults to `"0"`. The image is tagged with `IMAGE_VERSION=<ver>` at build; running
a baked image whose tag's major.minor drifts from the installed wheel is rejected. Keep wheel and
image tag in lockstep — rebuild the image after any wheel bump that changes major.minor.

---

## 4. Storage / conduit selection (`conduit/factory.py`)

```python
from resoluto_sandbox.conduit.factory import store_from_env
conduit = store_from_env(env=None)   # defaults to os.environ
```
Switches on `RESOLUTO_STORE_KIND` (KeyError if unset; unknown value → RuntimeError):

| kind | class | needs | status |
|---|---|---|---|
| `stdout` | `StdoutConduit()` | nothing | proven (local default) |
| `localfs` | `LocalConduit(RESOLUTO_STORE_ROOT)` | `RESOLUTO_STORE_ROOT` | proven (local) |
| `s3` | `S3Conduit(...)` | token OR bucket+creds (below) | proven (minio + cloud S3) |
| `gcs` | `GcsConduit(RESOLUTO_STORE_BUCKET, service_file=...)` | bucket; `RESOLUTO_GCS_SERVICE_FILE` opt | EXPERIMENTAL / unverified |

> GcsConduit is NOT integration-tested (no GCP creds in the env) — validated only by
> contract parity with the minio-tested S3Conduit. Run a real-bucket conformance pass
> before relying on it.

`s3` resolution (factory):
- If `RESOLUTO_STORE_WRITE_TOKEN` is set, it is `json.loads`'d and wins:
  `{bucket, endpoint_url?, region?, access_key_id, secret_access_key, session_token?}`
  → `S3Conduit(bucket, endpoint_url=, region_name=region|None, aws_access_key_id=, aws_secret_access_key=, aws_session_token=)` (factory defaults region to `"us-east-1"` when absent).
  This is the prefix-scoped, write-only, expiring credential the pod should use.
- Else falls back to ambient: `RESOLUTO_STORE_BUCKET` (required) +
  `RESOLUTO_STORE_ENDPOINT` (None if empty) + `RESOLUTO_STORE_REGION` (default `us-east-1`) +
  `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`.

Conduit constructors (for direct injection):
```python
StdoutConduit(*, sink: IO[str] | None = None)      # write-only; get/list/copy unsupported
LocalConduit(root: str | Path)                      # atomic tmp+rename+fsync writes
S3Conduit(bucket, *, endpoint_url=None, region_name=None,
          aws_access_key_id=None, aws_secret_access_key=None, aws_session_token=None)  # extra [s3]; also pulls aioboto3 via [s3] extra; factory defaults region to us-east-1
GcsConduit(bucket, *, service_file=None)            # extra [gcs]; service_file None → Workload Identity/ADC
```

Backend ↔ conduit pairing:
- **local** → `stdout` (default, telemetry to stdout, inputs read from workspace in place) or
  `localfs` (durable local store). No external infra.
- **k8s** → `s3` against minio (local) or real S3 (cloud). The pod self-reports chunks to the
  store; the orchestrator tails it. `K8sBackend` requires `RESOLUTO_STORE_KIND` in the env (it
  calls `store_from_env()` unless you inject `conduit=`).

---

## 5. k8s backend config (`backends/k8s.py`, `runtime/k8s.py`)

The k8s backend is FULLY IMPLEMENTED — a real Kata pod via `drive_node`, not a stub. Inject a
configured backend rather than passing `backend="k8s"` when you need image/conduit/egress:

```python
from resoluto_sandbox.client import Sandbox
from resoluto_sandbox.backends.k8s import K8sBackend
from resoluto_sandbox.runtime.k8s import EgressConfig

sb = Sandbox(backend=K8sBackend(
    image="resoluto-sandbox:0.2.3-claude",   # REQUIRED — ValueError if None at run()
    conduit=None,                            # None → store_from_env() (needs RESOLUTO_STORE_KIND)
    egress=None,                             # None → unrestricted egress (Kata kernel isolation only)
))
result = sb.run(["python", "agent.py"], workspace="/path/to/ws", output_paths=["out/*.json"])
```

`K8sBackend(*, image=None, conduit=None, egress=None)`. `run()` launches a Kata pod
(`flavor="plain"`, `runtime_class="kata"`, non-privileged), stages `workspace` into the store
prefix, runs `python -m resoluto_sandbox.runner_main`, fetches `output_paths` back into
`workspace` in place, reads `result.json`. Liveness = substrate silence watchdog
(`dead_after_s=600`), NO wall-clock timeout.

### k8s hard limit:
- **`stdin` is unsupported** → `NotImplementedError`. Don't pass `stdin=` with `backend="k8s"`.

### k8s output divergence (intentional)
The in-pod runner emits stdout AND stderr as `log` span events, so `RunResult.output` carries the
MERGED stream and `RunResult.errors == ""`. This is by design, not a dropped field.

### Egress allowlist — `EgressConfig` (frozen dataclass, `runtime/k8s.py`)
```python
EgressConfig(store_cidr: str, llm_cidr: str, git_cidrs: list[str] = [])
```
When set, applies a default-deny egress NetworkPolicy: allows ONLY the declared CIDRs on TCP/443
plus kube-dns on UDP/53; every rule excepts the IMDS CIDR `169.254.169.254/32`.
> FOOTGUN: all fields MUST be CIDR notation (`1.2.3.4/32`) — k8s `ipBlock` rejects FQDNs.
> Resolve hostnames to IPs yourself first; a bare hostname raises `ValueError` in `__post_init__`.
> `egress=None` → no NetworkPolicy → unrestricted egress (kernel isolation only).

### Kube-context safety
`K8sSandboxRuntime` PINS the context from `RESOLUTO_SANDBOX_KUBECONTEXT`. With NO context and no
in-cluster config it REFUSES to launch (could target the wrong/production cluster) unless
`RESOLUTO_SANDBOX_ALLOW_AMBIENT_CONTEXT=1`.

---

## 6. Env knobs the k8s path reads

Store wiring (consumed by `store_from_env`, forwarded to the pod via `_store_env_for_pod`,
which forwards `RESOLUTO_STORE_*` and `RESOLUTO_TRUSTED_LOCAL`):

| var | used by | meaning |
|---|---|---|
| `RESOLUTO_STORE_KIND` | factory | `stdout`/`localfs`/`s3`/`gcs` — selects the conduit |
| `RESOLUTO_STORE_ROOT` | localfs | local store root dir |
| `RESOLUTO_STORE_BUCKET` | s3/gcs | bucket name (ambient s3 fallback / gcs) |
| `RESOLUTO_STORE_ENDPOINT` | s3 | endpoint URL (minio); empty → None |
| `RESOLUTO_STORE_REGION` | s3 | region (default `us-east-1`) |
| `RESOLUTO_STORE_WRITE_TOKEN` | s3 | JSON prefix-scoped write-only creds; wins over ambient |
| `RESOLUTO_STORE_PREFIX` | pod | per-run/per-node store prefix (set by the backend) |
| `RESOLUTO_GCS_SERVICE_FILE` | gcs | service-account file; None → Workload Identity/ADC |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | s3 ambient | only used when no write token |
| `RESOLUTO_TRUSTED_LOCAL` | k8s pod env | dev-only: forward host AWS creds to the pod |

Runtime/placement:

| var | default | meaning |
|---|---|---|
| `RESOLUTO_SANDBOX_KUBECONTEXT` | None | PINNED kube-context (required to launch safely) |
| `RESOLUTO_SANDBOX_NAMESPACE` | `resoluto-sandboxes` | pod namespace |
| `RESOLUTO_LANE_IMAGE_PULL_POLICY` | `IfNotPresent` | pod imagePullPolicy |
| `RESOLUTO_SANDBOX_ALLOW_AMBIENT_CONTEXT` | unset | `1` to allow unpinned context (unsafe) |
| `RESOLUTO_SANDBOX_MAX_PODS` | `20` | namespace ResourceQuota pods |
| `RESOLUTO_SANDBOX_MAX_MEMORY` | `96Gi` | namespace ResourceQuota limits.memory |
| `RESOLUTO_SANDBOX_POD_MAX_MEMORY` | `24Gi` | per-pod LimitRange max memory |
| `RESOLUTO_SANDBOX_POD_MAX_CPU` | `4` | per-pod LimitRange max cpu |
| `RESOLUTO_NODE_ALLOCATABLE_MEMORY` | k8s API | dind tmpfs preflight node-RAM override |

> FOOTGUN: the standalone k8s backend forwards host `AWS_*` creds to the (untrusted) pod ONLY if
> `RESOLUTO_TRUSTED_LOCAL` is set; otherwise it raises and demands a scoped
> `RESOLUTO_STORE_WRITE_TOKEN`. Prefer the scoped write token; reserve `RESOLUTO_TRUSTED_LOCAL=1`
> for dev.

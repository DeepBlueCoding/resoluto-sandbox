# OPERATIONS: CLI, images, storage, version-lock

Action-first reference for running/extending this sandbox. Verified against source:
`src/resoluto/sandbox/{cli,images,version_guard}.py`, `conduit/factory.py`,
`backends/{base,substrate}.py`, `runtime/k8s.py`, `client.py`.

Cross-links (don't duplicate): protocol/event/chunk semantics → `../../../../spec/PROTOCOL.md`.
Substrate images are `Dockerfile.base` + `images/{claude,langchain,openai}.Dockerfile`; substrate internals (storage driver, stepped loop) → [`../../resoluto-sandbox-dev/references/internals.md`](../../resoluto-sandbox-dev/references/internals.md).

---

## Backends

```
your program  (plain: reads argv -> writes stdout/files/exit; never imports resoluto.sandbox)
      |  argv / workspace                         ^  output / errors / artifacts
      v                                           |
┌─────────────────────────────────────────────────────────────┐
│ Sandbox(backend=...)            thin facade: composes + delegates
│   .run(argv, ...) -> RunResult(exit_code, output, errors, …)  │
├─────────────────────────────────────────────────────────────┤
│ SubstrateBackend (the ONE impl)  ← drive_node + Conduit + runner_main
├──────────────────────────────┬──────────────────────────────┤
│ SandboxRuntime (ABC)         │  Conduit (ABC)  host<->sandbox exchange
│   KataNerdctlSandboxRuntime  │    LocalConduit (bind-mount, local backend)
│   (Kata microVM via nerdctl, │    StdoutConduit | S3Conduit | GcsConduit(exp.)
│    local)                    │
│   K8sSandboxRuntime          │
│   (Kata microVM pod on k8s)  │
└──────────────────────────────┴──────────────────────────────┘
```

| backend | isolation | where it runs | needs | use for |
|---------|-----------|---------------|-------|---------|
| `local` | hardware (Kata microVM via nerdctl + dedicated containerd) + host-side CNI egress | your machine | `/dev/kvm` + nerdctl + the dedicated containerd + an image | dev and most workloads, no cluster; untrusted code at VM-grade isolation |
| `k8s` | hardware (Kata microVM) + egress policy | a Kubernetes cluster | k8s + Kata + S3 store + image | untrusted code at scale, locked-down egress, production |

`RunResult.output` carries merged stdout+stderr (both backends). `RunResult.errors` is always
`""` by design — the in-sandbox runner emits both streams as `log` span events.

For the full backends guide (local detail, k8s detail, vendor-neutral k8s install guide for any
Kubernetes distribution): [`../../../../docs/backends.md`](../../../../docs/backends.md).

---

## 1. Public API (use this, not the CLI internals)

```python
from resoluto.sandbox.client import Sandbox  # the ONLY entrypoint

sb = Sandbox(backend="local")             # or "k8s", or a Backend instance (see §5)
result = sb.run(
    argv,                                 # Sequence[str], the program + args
    workspace=None,                       # str dir staged at /workspace, outputs land here in place; None = nothing staged
    stdin=None,                           # NOT SUPPORTED — NotImplementedError on both backends
    env=None,                             # dict[str,str] overlaid on sandbox env
    output_paths=None,                    # Sequence[str] globs collected into artifacts
    stream=None,                          # IO[str], live output (default sys.stdout)
) -> RunResult
```

`RunResult` (pydantic `BaseModel`, `backends/base.py`):

```python
exit_code: int
output: str            # program output (MERGED stdout+stderr on both backends)
errors: str            # "" by design — runner merges both streams
artifacts: list[str]   # collected output_paths (absolute paths)
result: dict | None    # parsed result.json if the program wrote one, else None
reason: str            # substrate forensics (evicted/OOMKilled/observed phase); "" for local
ok -> bool             # property: exit_code == 0
```

The program you run is plain — reads argv, writes stdout/files, NEVER imports
`resoluto.sandbox`. A program that works as `uv run agent.py` locally works
unchanged under `run()` — in a Kata microVM via nerdctl (local) or a Kata pod (k8s).

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
| `--workspace` | `None` | dir staged at `/workspace`; `None` = **nothing staged** (not cwd) |
| `--image` | `None` | image tag (REQUIRED for `--backend k8s`) |

```bash
# local run (Kata microVM via nerdctl)
resoluto-sandbox run --workspace . -- python agent.py --task build

# k8s (image REQUIRED)
resoluto-sandbox run --backend k8s --image resoluto-sandbox:claude-agent-sdk-0.2.110 -- python agent.py
```

### `doctor`
Readiness report. Exit 0 if all critical checks pass, else 1. Checks: `/dev/kvm`, the `nerdctl`
client, and the dedicated containerd socket (all REQUIRED for the local Kata-microVM backend,
critical); `docker` (only needed to build images, non-critical); `RESOLUTO_SANDBOX_KUBECONTEXT`
(k8s, non-critical). Prints `[OK]` per passing check, `[MISSING]` per failing critical check, and
`[absent]` per failing non-critical check.

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
builds the base first if needed. Prints each built tag to stdout. The default `--context .`
is this repo's own root — standalone, no parent/sibling directory needed.

---

## 3. Image matrix + version-lock (`images.py`, `version_guard.py`)

```
PROVIDERS = ("claude", "langchain", "openai")
SDK_PACKAGE = {"claude": "claude-agent-sdk", "langchain": "langchain", "openai": "openai-agents"}
SDK_VERSION = {"claude": "0.2.110", "langchain": "1.3.11", "openai": "0.17.7"}  # bump to move to a newer SDK release
# companion packages/binaries pinned alongside the anchor (NOT part of the tag, but still pinned —
# an unpinned companion is the same reproducibility break, one line over)
COMPANION_VERSIONS = {"claude": {"CLAUDE_CLI_VERSION": "2.1.201"}, "langchain": {"LANGGRAPH_VERSION": "1.2.7"}, "openai": {}}
```

`image_tags(ver)` → tag map. The base tag is the wheel version; each provider tag is its pinned
SDK package + version instead (so the tag itself says what's inside — never a floating install):
```
base       -> resoluto-sandbox-base:<ver>
claude     -> resoluto-sandbox:claude-agent-sdk-<sdk-version>
langchain  -> resoluto-sandbox:langchain-<sdk-version>
openai     -> resoluto-sandbox:openai-agents-<sdk-version>
```

Build wiring:
- `build_base(*, ver=None, context=".", runner=subprocess.run) -> str`
  `docker build -f Dockerfile.base -t resoluto-sandbox-base:<ver> <context>`
- `build(provider, *, ver=None, context=".", base_tag=None, runner=subprocess.run) -> str`
  builds base first if `base_tag is None`, then
  `docker build -f images/<provider>.Dockerfile --build-arg BASE_IMAGE=<base_tag> --build-arg IMAGE_VERSION=<ver> --build-arg SDK_VERSION=<sdk-version> [--build-arg <COMPANION_KEY>=<val> ...] -t resoluto-sandbox:<sdk-package>-<sdk-version> <context>`.
  Unknown provider → `ValueError`.
- `wheel_version() -> str` = `importlib.metadata.version("resoluto-sandbox")`. Default `ver`.
- `runner` is injectable (tests pass a fake; default `subprocess.run(..., check=True)`).

Each overlay Dockerfile pins its anchor package to `${SDK_VERSION}` (e.g.
`pip install claude-agent-sdk==${SDK_VERSION}`) and carries the wheel version as both
`LABEL resoluto.wheel_version=${IMAGE_VERSION}` (introspectable via `docker inspect`, no run needed)
and `ENV RESOLUTO_IMAGE_VERSION=${IMAGE_VERSION}` (asserted against the installed wheel at container
start by `version_guard.py` — fail loud on drift). Companion packages (e.g. `langgraph`) are left
to pip's resolver to pick versions compatible with the pinned anchor. **`langchain` bakes NO LLM
integration** — LangChain itself is provider-agnostic; extend the image with `langchain-anthropic`,
`langchain-openai`, etc. before it can call anything (see `references/agents.md`).

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
from resoluto.sandbox.conduit.factory import store_from_env
conduit = store_from_env(env=None)   # defaults to os.environ
```
Switches on `RESOLUTO_STORE_KIND` (KeyError if unset; unknown value → RuntimeError):

| kind | class | needs | status |
|---|---|---|---|
| `stdout` | `StdoutConduit()` | nothing | proven (local default) |
| `localfs` | `LocalConduit(RESOLUTO_STORE_ROOT)` | `RESOLUTO_STORE_ROOT` | proven (local bind-mount) |
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
- **local** → `localfs` (`LocalConduit`, bind-mounted at `/conduit` inside the Kata microVM guest). The
  local backend wires this automatically.
- **k8s** → `s3` against minio (local) or real S3 (cloud). The pod self-reports chunks to the
  store; the orchestrator tails it. `SubstrateBackend` requires a conduit (inject or `store_from_env()`).

---

## 5. k8s backend config (`backends/substrate.py`, `runtime/k8s.py`)

The k8s backend is FULLY IMPLEMENTED — a real Kata pod via `drive_node`, not a stub. Inject a
configured `SubstrateBackend` rather than passing `backend="k8s"` when you need egress/conduit config:

```python
import os
from resoluto.sandbox.client import Sandbox
from resoluto.sandbox.backends.substrate import SubstrateBackend, store_env_for_pod
from resoluto.sandbox.conduit.factory import store_from_env
from resoluto.sandbox.runtime.k8s import K8sSandboxRuntime
from resoluto.sandbox.egress import EgressConfig   # backend-neutral; re-exported from runtime.k8s

runtime = K8sSandboxRuntime(
    namespace="resoluto-sandboxes",
    context=os.environ.get("RESOLUTO_SANDBOX_KUBECONTEXT"),
    egress=None,                    # None = opt OUT (unrestricted); EgressConfig(...) = secure by default
)
sb = Sandbox(backend=SubstrateBackend(
    runtime=runtime,
    conduit=store_from_env(),       # needs RESOLUTO_STORE_KIND
    image="<registry>/resoluto-lane:2026-07",
    store_env=store_env_for_pod(os.environ),
))
result = sb.run(["python", "agent.py"], workspace="/path/to/ws", output_paths=["out/*.json"])
```

`SubstrateBackend(*, runtime, conduit, image, store_env)`. `run()` launches a Kata pod
(`flavor="plain"`, `runtime_class="kata"`, non-privileged), stages `workspace` into the store
prefix, runs `python -m resoluto.sandbox.runner_main`, fetches `output_paths` back into
`workspace` in place, reads `result.json`. Liveness = substrate silence watchdog
(`dead_after_s=600`), NO wall-clock timeout.

### Hard limit:
- **`stdin` is unsupported** → `NotImplementedError` on BOTH backends. Don't pass `stdin=`.

### Output divergence (intentional)
The in-sandbox runner emits stdout AND stderr as `log` span events, so `RunResult.output` carries the
MERGED stream and `RunResult.errors == ""`. This is by design, not a dropped field.

### Egress allowlist — `EgressConfig` (backend-neutral, frozen dataclass, `egress.py`)
```python
EgressConfig(allow=(), allow_port=443, public_https=False, store_cidr=None, store_port=443)
```
Canonical home `resoluto.sandbox.egress` (re-exported from `runtime.k8s` for back-compat). It is
**backend-neutral**: two pure renderers — `k8s_egress_rules()` (NetworkPolicy) and
`local_egress_iptables()` (host iptables) — drive the SAME config on BOTH backends. SECURE BY DEFAULT:
`EgressConfig()` ALWAYS allows only the object store at `store_cidr:store_port` (k8s only — local store
is a file mount) and DNS UDP+TCP/53; opt-in adds each `allow` entry on `allow_port`, and ALL public 443
ONLY when `public_https=True`. IMDS `169.254.169.254` is always denied (the local renderer also denies
RFC1918). **github / api.anthropic.com / any HTTPS do NOT work until you open them** — use `allow=[...]`
(least privilege, e.g. `["anthropic","npm","pypi"]`, or `allow_port=22` for git-over-SSH) or
`public_https=True` (escape hatch: ALL :443, trusted code). `EgressConfig.from_store_env()`
derives `store_cidr`/`store_port` from `RESOLUTO_STORE_ENDPOINT` (honoring `RESOLUTO_STORE_EGRESS_CIDR`/
`RESOLUTO_STORE_EGRESS_PORT`) AND the `RESOLUTO_EGRESS_ALLOW` / `_ALLOW_PORT` / `_PUBLIC_HTTPS` (default
0/deny) knobs (both backends honor those — local via `scripts/local-backend-up.sh`). Canonical
per-knob table: [`docs/networking.md`](../../../../docs/networking.md#modifying-the-egress-allowlist-one-config-both-backends).
To add a NEW backend, write a renderer in `egress.py`.
> FOOTGUN: `store_cidr` (and CIDR `allow` entries) MUST be CIDR notation (`1.2.3.4/32`) — k8s `ipBlock`
> rejects FQDNs (`__post_init__` raises `ValueError`); hostname `allow` entries resolve at render time.
> `egress=None` → opt OUT of isolation (no NetworkPolicy, unrestricted egress) — DIFFERENT from
> `EgressConfig()`, which denies by default.

### Kube-context safety
`K8sSandboxRuntime` PINS the context from `RESOLUTO_SANDBOX_KUBECONTEXT`. With NO context and no
in-cluster config it REFUSES to launch (could target the wrong/production cluster) unless
`RESOLUTO_SANDBOX_ALLOW_AMBIENT_CONTEXT=1`.

---

## 6. Env knobs the k8s path reads

Store wiring (consumed by `store_from_env`, forwarded to the pod via `store_env_for_pod`,
which forwards `RESOLUTO_STORE_*`):

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

> FOOTGUN: the k8s backend does NOT forward host `AWS_*` creds to the (untrusted) pod — it
> raises and demands a scoped `RESOLUTO_STORE_WRITE_TOKEN`. The pod authenticates to the store
> via the prefix-scoped, write-only, expiring token.

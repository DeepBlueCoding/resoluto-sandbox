# COOKBOOK + FOOTGUNS

Action-first recipes for running programs in this sandbox + the footguns that bite.
For wire protocol see [spec/PROTOCOL.md](../../../../spec/PROTOCOL.md). For deeper
prose see [docs/auth.md](../../../../docs/auth.md),
[docs/networking.md](../../../../docs/networking.md),
[docs/concepts.md](../../../../docs/concepts.md).

## The one API

```python
from resoluto_sandbox import Sandbox

Sandbox(backend="docker" | "k8s" | <Backend instance>)   # default "docker"
sb.run(
    argv,                       # Sequence[str] — the program, e.g. ["uv","run","agent.py","prompt"]
    *,
    workspace=None,             # str dir → program cwd; staged in (k8s) / bind-mounted (local Docker)
    stdin=None,                 # NOT SUPPORTED — NotImplementedError on both backends
    env=None,                   # dict[str,str] — overlays sandbox env
    output_paths=None,          # Sequence[str] globs → collected into RunResult.artifacts
    stream=None,                # IO[str] — live output sink; None → echoes to sys.stdout
) -> RunResult
```

```python
class RunResult(BaseModel):
    exit_code: int
    output: str                 # MERGED stdout+stderr; errors stays ""
    errors: str                 # "" by design on both backends
    artifacts: list[str] = []   # collected output_paths (absolute paths)
    result: dict | None = None  # parsed result.json if the program wrote one, else None
    reason: str = ""            # substrate forensics (evicted/OOMKilled/canary fail); "" for local
    @property
    def ok(self) -> bool: ...   # exit_code == 0
```

The program is PLAIN: reads argv, writes stdout/files. It never imports
`resoluto_sandbox`. Same program runs in a Docker container (local) or Kata pod (k8s).

Dependencies are your program's concern — put `uv run`/`pip install` in your argv, or use a prebuilt image.

---

## RECIPES

### 1. Claude agent on a Max subscription — local

Runs in a Docker container (OS-level isolation, no egress NetworkPolicy). The container
inherits env you pass via `env=`. You need Docker + an image with `claude` CLI baked in.

```python
from resoluto_sandbox import Sandbox

r = Sandbox(backend="docker").run(
    ["uv", "run", "examples/claude_agent.py", "Say hello in five words"],
    workspace="examples",
    env={"CLAUDE_CODE_OAUTH_TOKEN": "..."},  # or mount credentials via the image
)
print(r.output)
```

Prereq: Docker running, an image with `uv` + your deps. Do NOT set `ANTHROPIC_API_KEY`
(see footguns). Auth detail: [docs/auth.md](../../../../docs/auth.md).

### 2. Claude agent on a Max subscription — k8s / image

Inject a configured `SubstrateBackend`. The image bakes the CLI+SDK but no creds; the
pod authenticates to the store via a scoped token. Requires `RESOLUTO_STORE_KIND`
+ `RESOLUTO_SANDBOX_KUBECONTEXT` in the host env.

```python
import os
from resoluto_sandbox import Sandbox
from resoluto_sandbox.backends.substrate import SubstrateBackend, store_env_for_pod
from resoluto_sandbox.conduit.factory import store_from_env
from resoluto_sandbox.runtime.k8s import K8sSandboxRuntime

runtime = K8sSandboxRuntime(
    namespace="resoluto-sandboxes",
    context=os.environ.get("RESOLUTO_SANDBOX_KUBECONTEXT"),
)
sb = Sandbox(backend=SubstrateBackend(
    runtime=runtime,
    conduit=store_from_env(),
    image="<registry>/resoluto-lane:dev",
    store_env=store_env_for_pod(os.environ),
))
r = sb.run(
    ["python", "claude_agent.py", "Say hello in five words"],
    workspace="/abs/path/to/examples",     # staged into the pod at /workspace
)
print(r.output, r.reason)
```

Subscription auth inside the pod: bake `CLAUDE_CODE_OAUTH_TOKEN` (from
`claude setup-token`) into the image, or pass it via `env=`. Never `ANTHROPIC_API_KEY`.

### 3. Capture artifacts (output_paths)

`output_paths` are globs collected after the run into `RunResult.artifacts`
(absolute paths). On k8s the files are fetched out of the pod into the
SAME `workspace` dir in place (workspace is mutated), matching local.

```python
r = sb.run(
    ["uv", "run", "build.py"],
    workspace="/abs/work",
    output_paths=["dist/*.whl", "report.json"],
)
print(r.artifacts)          # e.g. ["/abs/work/dist/app-1.0.whl", "/abs/work/report.json"]
print(r.result)             # parsed work/result.json if the program wrote one
```

### 4. Lock down egress (k8s only — untrusted code)

Default-deny egress NetworkPolicy: only the listed CIDRs on TCP/443 + kube-dns
UDP/53. IMDS (`169.254.169.254`) is ALWAYS blocked. CIDR-only — no FQDNs.

```python
import os
from resoluto_sandbox import Sandbox
from resoluto_sandbox.backends.substrate import SubstrateBackend, store_env_for_pod
from resoluto_sandbox.conduit.factory import store_from_env
from resoluto_sandbox.runtime.k8s import K8sSandboxRuntime, EgressConfig   # NOT from top-level resoluto_sandbox

egress = EgressConfig(
    store_cidr="192.168.1.197/32",      # object store (minio / S3-compatible)
    llm_cidr="160.79.104.0/23",         # resolve api.anthropic.com → CIDR yourself
    git_cidrs=["140.82.112.0/20"],      # optional; [] = no git egress
)
runtime = K8sSandboxRuntime(
    namespace="resoluto-sandboxes",
    context=os.environ.get("RESOLUTO_SANDBOX_KUBECONTEXT"),
    egress=egress,
)
sb = Sandbox(backend=SubstrateBackend(
    runtime=runtime,
    conduit=store_from_env(),
    image="<registry>/resoluto-lane:dev",
    store_env=store_env_for_pod(os.environ),
))
```

`EgressConfig.__post_init__` rejects any value without `/` (no hostnames). Needs
a NetworkPolicy-capable CNI (see `docs/networking.md`). An in-guest canary
fail-closes if the policy was not enforced. Full table: [docs/networking.md](../../../../docs/networking.md).

### 5. Bring your own OCI image

The image is a `SubstrateBackend` concern — pass it as `image=`. The image
must bake every runtime dep your program needs. The
entrypoint is fixed to `python -m resoluto_sandbox.runner_main`; your `argv`
is delivered via env and executed by the runner inside `/workspace`.

```python
import os
from resoluto_sandbox import Sandbox
from resoluto_sandbox.backends.substrate import SubstrateBackend, store_env_for_pod
from resoluto_sandbox.conduit.factory import store_from_env
from resoluto_sandbox.runtime.k8s import K8sSandboxRuntime

runtime = K8sSandboxRuntime(
    namespace="resoluto-sandboxes",
    context=os.environ.get("RESOLUTO_SANDBOX_KUBECONTEXT"),
)
sb = Sandbox(backend=SubstrateBackend(
    runtime=runtime,
    conduit=store_from_env(),
    image="myregistry/my-lane:2026-06",
    store_env=store_env_for_pod(os.environ),
))
```

For docker, `Sandbox(backend="docker", image="myregistry/my-base:tag")` runs your image
in a Docker container on this host.

### 6. Stream vs capture output

`stream=None` (default) ECHOES live to `sys.stdout` AND captures into
`RunResult.output`. To capture without polluting your console, pass any `IO[str]`:

```python
import io
buf = io.StringIO()
r = sb.run(["uv", "run", "agent.py", "prompt"], stream=buf)
captured_live = buf.getvalue()    # streamed lines
full = r.output                   # same content, fully captured
```

Both backends merge stdout→`output` (errors stays `""`).

---

## FOOTGUNS

- **`docker` = Docker (OS-level isolation, NOT egress-locked).** `backend="docker"` runs a Docker
  container with OS-level isolation (separate namespaces/cgroups), but NO egress NetworkPolicy.
  Needs Docker running + an image (default `resoluto-sandbox-runner:dev`; override with `image=`).
  The image must contain python + the resoluto-sandbox wheel + your program's deps. Trusted code
  only. For untrusted/adversarial workloads use `backend="k8s"` (Kata microVM + egress policy).

- **k8s default egress is UNRESTRICTED.** `egress=None` (default) gives the pod
  Kata kernel isolation but NO NetworkPolicy — it can reach anything. Pass an
  `EgressConfig` for untrusted code (recipe 4).

- **`stdin` raises on both backends.** Neither `local` nor `k8s` supports `stdin=`.
  Pass inputs via argv, env, or workspace files.

- **`-e VAR` with nothing exported = empty auth.** `docker run -e
  CLAUDE_CODE_OAUTH_TOKEN` (no `=value`) forwards the host value — empty if never
  exported. The CLI then sees no auth and returns `Not logged in`, which the SDK
  rethrows as the misleading `Claude Code returned an error result: success`.
  `export` the token first, or mount the credentials file. Same trap for any
  `env=` key you forward expecting a value.

- **Do NOT set `ANTHROPIC_API_KEY` for a subscription.** If a key is present the
  CLI uses it and bills the API, silently NOT your Max/Pro subscription. For
  subscription billing use `CLAUDE_CODE_OAUTH_TOKEN` or
  `~/.claude/.credentials.json` and ensure the key is ABSENT. Mount only the
  single `.credentials.json` file (RO), not the whole `~/.claude` dir (the CLI
  writes cache/history there and fails on a RO mount).

- **Image tag MUST match the wheel.** The baked image and the host runner code
  drift independently — a code change on the host does NOT reach a stale image.
  Republish the image when the in-sandbox code changes; pin a concrete tag, never
  rely on a floating `:dev` matching your local source.

- **No wall-clock timeouts anywhere.** Liveness is substrate-silence watchdog +
  heartbeat (`dead_after_s=600` is silence, not a deadline). Do not wrap `run()`
  in `wait_for(timeout=)` / `timeout N` — a live workload is allowed to run as
  long as it keeps producing output.

- **pytest piped to head/tail needs `set -o pipefail`.** Without it the pipe
  masks pytest's exit code and a failing suite reports PASS. If a program you run
  shells out to a test runner through a pipe, it must set `-o pipefail`.

- **`GcsConduit` is EXPERIMENTAL / unverified.** Proven conduits: local/stdout
  (local backend) and S3-against-minio (k8s). `GcsConduit` is validated only by
  contract parity with S3 — no real-GCS integration test. Run the conformance
  suite against a live bucket before relying on it.

- **`EgressConfig` must come from `resoluto_sandbox.runtime.k8s`,** not the
  top-level package (the top-level import would eagerly pull in
  `kubernetes_asyncio`).

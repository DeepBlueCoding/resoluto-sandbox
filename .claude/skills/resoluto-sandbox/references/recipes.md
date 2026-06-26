# COOKBOOK + FOOTGUNS

Action-first recipes for running programs in this sandbox + the footguns that bite.
For wire protocol see [spec/PROTOCOL.md](../../../../spec/PROTOCOL.md). For deeper
prose see [docs/auth.md](../../../../docs/auth.md),
[docs/networking.md](../../../../docs/networking.md),
[docs/concepts.md](../../../../docs/concepts.md).

## The one API

```python
from resoluto_sandbox import Sandbox

Sandbox(backend="local" | "k8s" | <Backend instance>)   # default "local"
sb.run(
    argv,                       # Sequence[str] — the program, e.g. ["uv","run","agent.py","prompt"]
    *,
    workspace=None,             # str dir → program cwd; staged in (k8s) / used in place (local)
    stdin=None,                 # str|bytes — local only; k8s raises NotImplementedError
    env=None,                   # dict[str,str] — overlays host env
    output_paths=None,          # Sequence[str] globs → collected into RunResult.artifacts
    stream=None,                # IO[str] — live output sink; None → echoes to sys.stdout
) -> RunResult
```

```python
class RunResult(BaseModel):
    exit_code: int
    output: str                 # k8s: MERGED stdout+stderr; errors stays ""
    errors: str                 # local only; "" on k8s by design
    artifacts: list[str] = []   # collected output_paths (absolute paths)
    result: dict | None = None  # parsed result.json if the program wrote one, else None
    reason: str = ""            # substrate forensics (evicted/OOMKilled/canary fail); "" for local
    @property
    def ok(self) -> bool: ...   # exit_code == 0
```

The program is PLAIN: reads argv/stdin, writes stdout/files. It never imports
`resoluto_sandbox`. Same program runs byte-identically local and k8s.

Dependencies are your program's concern — put `uv run`/`pip install` in your argv, or use a prebuilt image.

---

## RECIPES

### 1. Claude agent on a Max subscription — local

Inherits host env, so an already-logged-in `claude` CLI just works. NO key wiring.

```python
from resoluto_sandbox import Sandbox

r = Sandbox().run(["uv", "run", "examples/claude_agent.py", "Say hello in five words"])
print(r.output)
```

Prereq: `claude` once (interactive login) so `~/.claude/.credentials.json` exists.
Do NOT set `ANTHROPIC_API_KEY` (see footguns). Auth detail: [docs/auth.md](../../../../docs/auth.md).

### 2. Claude agent on a Max subscription — k8s / image

Inject a configured `K8sBackend`. The image bakes the CLI+SDK but no creds; the
pod authenticates to the store via a scoped token. Requires `RESOLUTO_STORE_KIND`
+ `RESOLUTO_SANDBOX_KUBECONTEXT` in the host env.

```python
from resoluto_sandbox import Sandbox
from resoluto_sandbox.backends.k8s import K8sBackend

sb = Sandbox(backend=K8sBackend(image="<registry>/resoluto-lane:dev"))
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
from resoluto_sandbox import Sandbox
from resoluto_sandbox.backends.k8s import K8sBackend
from resoluto_sandbox.runtime.k8s import EgressConfig      # NOT from top-level resoluto_sandbox

egress = EgressConfig(
    store_cidr="192.168.1.197/32",      # object store (minio / S3-compatible)
    llm_cidr="160.79.104.0/23",         # resolve api.anthropic.com → CIDR yourself
    git_cidrs=["140.82.112.0/20"],      # optional; [] = no git egress
)
sb = Sandbox(backend=K8sBackend(image="<registry>/resoluto-lane:dev", egress=egress))
```

`EgressConfig.__post_init__` rejects any value without `/` (no hostnames). Needs
a NetworkPolicy-capable CNI (k3s Flannel has it). An in-guest canary
fail-closes if the policy was not enforced. Full table: [docs/networking.md](../../../../docs/networking.md).

### 5. Bring your own OCI image

The image is a backend concern — pass it to `K8sBackend(image=...)`. The image
must bake every runtime dep your program needs. The
pod entrypoint is fixed to `python -m resoluto_sandbox.runner_main`; your `argv`
is delivered via env and executed by the runner inside `/workspace`.

```python
sb = Sandbox(backend=K8sBackend(image="myregistry/my-lane:2026-06"))
```

`K8sBackend()` with no image raises `ValueError` at `run()` time.

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

Local tees stdout→`stream` and stderr→`sys.stderr` separately. k8s merges both
into `output` (errors stays `""`).

---

## FOOTGUNS

- **`local` is NOT isolation.** `LocalBackend` runs a plain host subprocess with
  the full host env, host network, host filesystem. No kernel/network/fs boundary.
  Trusted code only. For untrusted/adversarial workloads use `backend="k8s"`
  (Kata microVM + egress policy).

- **k8s default egress is UNRESTRICTED.** `egress=None` (default) gives the pod
  Kata kernel isolation but NO NetworkPolicy — it can reach anything. Pass an
  `EgressConfig` for untrusted code (recipe 4).

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
  Republish the image when the in-pod code changes; pin a concrete tag, never
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

- **k8s hard limit:** `stdin` raises `NotImplementedError`. Feed inputs via argv/files/`env`;
  bake deps into the image.

- **`EgressConfig` must come from `resoluto_sandbox.runtime.k8s`,** not the
  top-level package (the top-level import would eagerly pull in
  `kubernetes_asyncio`).

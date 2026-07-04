# Bring your own agent (any language) + auth

The sandbox runs a **plain program**. Your program reads argv and writes
stdout/files; it NEVER imports `resoluto_sandbox`. What runs as
`uv run agent.py` (or `./agent`, `node agent.js`, …) on your machine runs
unchanged under `Sandbox().run(...)`. On `backend="local"` it runs in a Kata
microVM via nerdctl; on `backend="k8s"` it runs in a Kata microVM pod.

This is the agent-author view. For the wire protocol see [`spec/PROTOCOL.md`](../../../../spec/PROTOCOL.md);
for host→pod config/credential flow see `operations.md` (this dir) and the parent `SKILL.md`.

## The program contract

Your program is the unit of work. The contract is the OS process contract — nothing custom:

| Channel | Direction | Meaning |
|---|---|---|
| `argv` (`argv[1:]`) | host → program | the task/prompt/args |
| `stdin` | NOT SUPPORTED | raises `NotImplementedError` on both backends |
| `env` | host → program | overlaid on sandbox env |
| `stdout` | program → host | the answer → `RunResult.output` |
| `stderr` | program → host | merged with stdout → `RunResult.output` (both backends emit as `log` events) |
| exit code | program → host | `0` = ok → `RunResult.exit_code`, `RunResult.ok` |
| files under `workspace` | program → host | collected via `output_paths` → `RunResult.artifacts` |
| `result.json` in workspace | program → host | optional typed verdict → `RunResult.result` (dict or `None`) |

Minimal program — read prompt from argv, print the answer, exit 0:

```python
#!/usr/bin/env python3
import sys
prompt = " ".join(sys.argv[1:]).strip()
if not prompt:
    print("usage: agent.py <prompt>", file=sys.stderr)
    raise SystemExit(2)
print(do_work(prompt))          # whatever your agent does
```

### Optional `result.json`

If your program writes `result.json` into its workspace (cwd), the host parses it
into `RunResult.result`. Schema: [`spec/result.schema.json`](../../../../spec/result.schema.json).
The self-report fields you may write: `node_id`, `status` (`"success"|"failure"`),
`exit_code`, `output_archive`. The `observed_*`/`reason`/`substrate_logs` fields are
filled by the ORCHESTRATOR from out-of-guest signals — do NOT write them. Carries no
gate/lane/git vocabulary by design. `result.json` is optional; absence ⇒ `RunResult.result is None`.

## The API

```python
from resoluto_sandbox import Sandbox

Sandbox(*, backend: Backend | str = "local")    # "local" | "k8s" | injected Backend instance

.run(
    argv: Sequence[str],
    *,
    workspace: str | None = None,       # program cwd; staged in, artifacts extracted back in place
    stdin: str | bytes | None = None,   # NOT SUPPORTED — NotImplementedError on both backends
    env: dict[str, str] | None = None,  # overlays sandbox env
    output_paths: Sequence[str] | None = None,  # globs collected into RunResult.artifacts
    stream: IO[str] | None = None,      # live output sink, default sys.stdout
    egress: Sequence[str] | None = None,  # THIS run's allowed domains (e.g. ["api.anthropic.com"]);
                                          # None/[] = deny all. Per-step networking on the fly (local
                                          # backend, via the SNI proxy); no re-provision.
) -> RunResult
```

```python
class RunResult(BaseModel):
    exit_code: int
    output: str
    errors: str                # always "" by design (output carries merged stdout+stderr)
    artifacts: list[str] = []   # filesystem paths under workspace
    result: dict | None = None  # parsed result.json, else None
    reason: str = ""            # substrate forensics (e.g. OOMKilled/evicted pod); empty for local
    @property
    def ok(self) -> bool: ...    # exit_code == 0
```

Dependencies are your program's concern — put `uv run`/`pip install` in your argv, or use a prebuilt image.

## Any binary / any language as a plain program

`argv[0]` is just an executable. None of these touch the sandbox SDK:

```python
Sandbox().run(["uv", "run", "agent.py", "Say hi"], workspace="examples")        # python
Sandbox().run(["node", "agent.js", "Say hi"], workspace="examples")             # node
Sandbox().run(["./agent", "Say hi"], workspace="examples")                      # compiled binary
Sandbox().run(["bash", "-c", "echo hi && ls"], workspace="examples")            # shell
```

LangChain / LangGraph / OpenAI-Agents are libraries your program imports — there is
NO special integration. Write a normal script that imports them and prints to stdout;
run it like any other program. Use the prebuilt images (below) so the libs are present.

## Prebuilt SDK images

Layered on `resoluto-sandbox-base` (see `images/`). Each pins one anchor SDK package/version
(`images.py:SDK_VERSION`) and tags itself by that package + version — never a floating install:

| Image (`images/*.Dockerfile`) | Tag (`resoluto-sandbox image build --provider ...`) | Bakes |
|---|---|---|
| `claude.Dockerfile` | `resoluto-sandbox:claude-agent-sdk-0.2.110` | `@anthropic-ai/claude-code` (npm) + `claude-agent-sdk==0.2.110` (pip) |
| `langchain.Dockerfile` | `resoluto-sandbox:langchain-1.3.11` | `langchain==1.3.11` + `langgraph langchain-anthropic` (resolver-picked) |
| `openai.Dockerfile` | `resoluto-sandbox:openai-agents-0.17.7` | `openai-agents==0.17.7` |

The wheel version (must match the running `resoluto-sandbox` package) travels as the
`resoluto.wheel_version` OCI label plus the `RESOLUTO_IMAGE_VERSION` env guard — not in the tag.

To extend: copy a Dockerfile, `FROM ${BASE_IMAGE}`, add your `pip install`/`npm install -g`,
keep `USER 1000` last. On k8s, pass the image to `SubstrateBackend(image="your-image:tag")`.

## Claude Max/Pro subscription auth

The sandbox NEVER handles credentials. The `claude` CLI that the SDK forks resolves
auth itself, in this preference order (full detail in [`docs/auth.md`](../../../../docs/auth.md)):

1. `CLAUDE_CODE_OAUTH_TOKEN` — long-lived OAuth token (`claude setup-token` prints it)
2. `~/.claude/.credentials.json` — subscription login file (under `$HOME`, or `$CLAUDE_CONFIG_DIR`)
3. `ANTHROPIC_API_KEY` — pay-as-you-go API billing

> **To bill your subscription, use (1) or (2) and ensure `ANTHROPIC_API_KEY` is NOT set.**
> If an API key is present the CLI uses it and bills the API instead of your subscription.

### Local backend — supply credentials explicitly

`Sandbox(backend="local")` runs your program in a Kata microVM via nerdctl. The guest does NOT
automatically inherit your host environment — credentials must reach the guest via `env=`
or a baked credentials file. If you are already logged in to Claude Code on this machine,
pass the credentials explicitly:

```bash
# One-time interactive login on your Max/Pro account, if needed:
claude

python -c "from resoluto_sandbox import Sandbox; \
  print(Sandbox().run(['uv','run','examples/claude_agent.py','Say hello in five words']).output)"
```

### Container image — supply auth at `docker run`

Long-lived token (best for containers / CI):

```bash
claude setup-token                 # prints an OAuth token; copy it
export CLAUDE_CODE_OAUTH_TOKEN=... # the value from above

docker run --rm -e CLAUDE_CODE_OAUTH_TOKEN \
  -v "$PWD/examples:/workspace" \
  resoluto-sandbox:claude-agent-sdk-0.2.110 python claude_agent.py "Say hello in five words"
```

Or mount just the subscription login file, read-only:

```bash
docker run --rm \
  -v "$HOME/.claude/.credentials.json:/root/.claude/.credentials.json:ro" \
  -v "$PWD/examples:/workspace" \
  resoluto-sandbox:claude-agent-sdk-0.2.110 python claude_agent.py "Say hello in five words"
```

Mount the single `.credentials.json` file, NOT the whole `~/.claude` dir — the CLI
writes history/cache into its config dir and would fail against a read-only dir mount.
In both cases `ANTHROPIC_API_KEY` is intentionally absent ⇒ subscription billing.

### Footgun: `-e VAR` with nothing exported

`docker run -e CLAUDE_CODE_OAUTH_TOKEN` (no `=value`) forwards the host's value —
which is **empty** if you never exported it. The container then has no auth; the CLI
returns `Not logged in`, which the SDK rethrows as the confusing
`Claude Code returned an error result: success`. Fix: `export` the token first, or use
the credentials-file mount.

## k8s backend — real Kata pod

`backend="k8s"` launches a real Kata pod via `drive_node` (fully implemented — not a
stub). Inject a configured `SubstrateBackend`; the image is a backend concern.

**An s3 store needs a SCOPED write token.** Host AWS creds are NEVER forwarded to the pod —
`store_env_for_pod(os.environ)` raises if `AWS_*` are set and there is no
`RESOLUTO_STORE_WRITE_TOKEN`. Mint a prefix-scoped STS token (the substrate writes under
`run/...`) and hand it to the pod via `store_env`. The host conduit keeps the full creds for
staging; the pod gets only the scoped token:

```python
import asyncio, json, os
from resoluto_sandbox import Sandbox
from resoluto_sandbox.backends.substrate import SubstrateBackend
from resoluto_sandbox.conduit.factory import store_from_env
from resoluto_sandbox.conduit.s3 import mint_scoped_credential
from resoluto_sandbox.runtime.k8s import K8sSandboxRuntime
from resoluto_sandbox.egress import EgressConfig

token = asyncio.run(mint_scoped_credential(
    bucket=os.environ["RESOLUTO_STORE_BUCKET"], prefix="run",
    endpoint_url=os.environ["RESOLUTO_STORE_ENDPOINT"],
    region=os.environ.get("RESOLUTO_STORE_REGION", "us-east-1"),
    access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
    secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
    sts_role_arn=os.environ["RESOLUTO_STORE_STS_ROLE_ARN"],
))
store_env = {k: v for k, v in os.environ.items() if k.startswith("RESOLUTO_STORE_")}
store_env["RESOLUTO_STORE_WRITE_TOKEN"] = json.dumps(token)   # pod authenticates with THIS only

runtime = K8sSandboxRuntime(
    namespace="resoluto-sandboxes",
    context=os.environ.get("RESOLUTO_SANDBOX_KUBECONTEXT"),   # pin the cluster; ambient is refused
    egress=EgressConfig.from_store_env(),                     # default-deny egress (or None = unrestricted)
)
sb = Sandbox(backend=SubstrateBackend(
    runtime=runtime,
    conduit=store_from_env(),              # host keeps full creds for staging; needs RESOLUTO_STORE_KIND
    image=os.environ["RESOLUTO_LANE_IMAGE"],   # REQUIRED, and present in the cluster's containerd
    store_env=store_env,
))
res = sb.run(["python", "echo_agent.py", "ping-42"], workspace="examples",
             output_paths=["result.json"], env={"SMOKE_TAG": "x"})
```

Requires `RESOLUTO_STORE_KIND` (the conduit is how the pod's workspace/artifacts travel).
Workspace is staged into the store and extracted back into your `workspace` dir in place.

> **Egress enforcement is the CNI's job.** `EgressConfig` is a default-deny NetworkPolicy, but it
> only blocks traffic if your CNI enforces NetworkPolicy (Cilium/Calico — **not** stock Flannel).
> The in-guest egress canary is fail-closed, so on a non-enforcing CNI a lane will refuse to run
> (and there can be a brief startup window where a fast pod out-races policy programming). On a
> single host, prefer `backend="local"`, which enforces egress host-side on its own bridge.

### Verify both backends — the smoke test

`examples/smoke_both_backends.py` runs the minimal `examples/echo_agent.py` through BOTH
backends and asserts the full input→agent→output contract (argv + env in; stdout + `result.json`
out). Run it from `resoluto-sandbox/`:

```bash
set -a; source store.env; source ../local.env; set +a     # store + local-Kata config
uv run python examples/smoke_both_backends.py              # both  (--local-only / --k8s-only)
```

`local` is GREEN when its bootstrap is up; `k8s` is GREEN when the CNI enforces egress in time,
else `BLOCKED` (a clearly-reported environment limit, not a code failure).

**See a REAL LLM call's input and output** with `examples/smoke_llm.py` — it runs
`examples/llm_agent.py` (a real claude-agent-sdk program) through the sandbox and prints the
prompt (input) and Claude's answer (output):

```bash
uv run python examples/smoke_llm.py "In five words, why do sandboxes matter?"
#   INPUT  (prompt to the LLM): 'In five words, why do sandboxes matter?'
#   OUTPUT (the LLM's answer) : 'They prevent untrusted code escaping containment.'
```

Auth is `CLAUDE_CODE_OAUTH_TOKEN` (or, as a convenience, the OAuth access token read from your
subscription `~/.claude/.credentials.json`); `ANTHROPIC_API_KEY` stays unset for subscription
billing. NOTE: the workspace tar does NOT carry dot-dirs, so a `.claude/` staged into `workspace`
won't reach the guest — pass auth via `env=`, not a staged file.

### k8s hard limit

- **No `stdin`** → `NotImplementedError`. Pass input via argv, env, or workspace files.

Everything else works. `RunResult.errors` is empty by design — the in-pod runner
emits stdout+stderr as merged `log` events, so `RunResult.output` carries both. `RunResult.reason`
carries pod forensics (e.g. `OOMKilled`, evicted) when present.

### `EgressConfig` (default-deny pod egress)

`from resoluto_sandbox.egress import EgressConfig` — backend-neutral frozen dataclass (also re-exported
from `resoluto_sandbox.runtime.k8s` for back-compat). SECURE BY DEFAULT — `EgressConfig()` allows only
store + DNS; there is NO `llm_cidr`/`git_cidrs`, you open HTTPS via `allow=[...]` or `public_https`:

```python
EgressConfig(
    store_cidr="10.0.0.5/32",              # k8s object store CIDR (REQUIRED for k8s; local ignores it — file mount)
    store_port=9100,                       # the store's port (default 443)
    allow=["api.anthropic.com", "registry.npmjs.org", "pypi.org"],    # open specific destinations — hostnames OR CIDRs — on allow_port
    allow_port=443,                        # port for `allow` (default 443; e.g. 22 for git-over-SSH)
    public_https=False,                    # DEFAULT deny all :443; True = allow ALL public :443 (escape hatch)
)
# or: EgressConfig.from_store_env()   # store_cidr:port + RESOLUTO_EGRESS_* knobs, from env
```

It is **backend-neutral** — the same config renders to a k8s NetworkPolicy (`k8s_egress_rules()`) OR
local iptables (`local_egress_iptables()`). It ALWAYS allows: **store_cidr:store_port (TCP)** and
**DNS 53**; opt-in adds each **`allow` entry on `allow_port`** and **all public 443** (only when
`public_https=True`) — IMDS `169.254.169.254` is always denied. **SECURE BY DEFAULT: github /
api.anthropic.com / any HTTPS do NOT work until you open them** — use `allow=[...]` (least privilege)
or `public_https=True` (escape hatch, trusted code). `egress=None` ⇒ opt OUT of isolation (no
NetworkPolicy, unrestricted egress) — distinct from `EgressConfig()`, which denies by default. Env
knobs `RESOLUTO_EGRESS_ALLOW` / `_ALLOW_PORT` / `_PUBLIC_HTTPS` (default 0/deny) work for both
backends — see `networking.md`.

## Conduits (where workspace/artifacts travel)

Selected by `RESOLUTO_STORE_KIND` via `store_from_env()`, or inject `SubstrateBackend(conduit=...)`.

| Kind | Conduit | Status |
|---|---|---|
| `stdout` | `StdoutConduit` (write-only) | local/stdout path — **proven** |
| `localfs` | `LocalConduit` (`RESOLUTO_STORE_ROOT`) | local dev — **proven** |
| `s3` | `S3Conduit` (minio locally / any S3 API) | k8s default — **proven** |
| `gcs` | `GcsConduit` (`RESOLUTO_STORE_BUCKET`) | **experimental / unverified — do not rely on it** |

See [`spec/PROTOCOL.md`](../../../../spec/PROTOCOL.md)
for the key namespace and chunk/tail semantics.

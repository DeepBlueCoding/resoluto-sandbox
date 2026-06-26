# Bring your own agent (any language) + auth

The sandbox runs a **plain program**. Your program reads argv and writes
stdout/files; it NEVER imports `resoluto_sandbox`. What runs as
`uv run agent.py` (or `./agent`, `node agent.js`, …) on your machine runs
unchanged under `Sandbox().run(...)`. On `backend="local"` it runs in a Docker
container with OS-level isolation; on `backend="k8s"` it runs in a Kata microVM.

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

Sandbox(*, backend: Backend | str = "local")   # "local" | "k8s" | injected Backend instance

.run(
    argv: Sequence[str],
    *,
    workspace: str | None = None,       # program cwd; staged in, artifacts extracted back in place
    stdin: str | bytes | None = None,   # NOT SUPPORTED — NotImplementedError on both backends
    env: dict[str, str] | None = None,  # overlays sandbox env
    output_paths: Sequence[str] | None = None,  # globs collected into RunResult.artifacts
    stream: IO[str] | None = None,      # live output sink, default sys.stdout
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

Layered on `resoluto-sandbox-base` (see `images/`). Each just `pip install`s a stack:

| Image (`images/*.Dockerfile`) | Bakes |
|---|---|
| `claude.Dockerfile` → `resoluto-sandbox:claude` | `@anthropic-ai/claude-code` (npm) + `claude-agent-sdk` (pip) |
| `langchain.Dockerfile` | `langchain langgraph langchain-anthropic` |
| `openai.Dockerfile` | `openai-agents` |

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

### Local backend — nothing to configure

`Sandbox(backend="local")` runs your program in a Docker container that inherits the
env you pass. If you are already logged in to Claude Code on this machine, pass the
credentials via env or mount:

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
  resoluto-sandbox:claude python claude_agent.py "Say hello in five words"
```

Or mount just the subscription login file, read-only:

```bash
docker run --rm \
  -v "$HOME/.claude/.credentials.json:/root/.claude/.credentials.json:ro" \
  -v "$PWD/examples:/workspace" \
  resoluto-sandbox:claude python claude_agent.py "Say hello in five words"
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
stub). Inject a configured `SubstrateBackend`; the image is a backend concern:

```python
import os
from resoluto_sandbox import Sandbox
from resoluto_sandbox.backends.substrate import SubstrateBackend, store_env_for_pod
from resoluto_sandbox.conduit.factory import store_from_env
from resoluto_sandbox.runtime.k8s import K8sSandboxRuntime, EgressConfig

runtime = K8sSandboxRuntime(
    namespace="resoluto-sandboxes",
    context=os.environ.get("RESOLUTO_SANDBOX_KUBECONTEXT"),
    egress=None,                           # None → unrestricted egress (Kata isolation only)
)
sb = Sandbox(backend=SubstrateBackend(
    runtime=runtime,
    conduit=store_from_env(),              # needs RESOLUTO_STORE_KIND
    image="resoluto-sandbox:claude",       # REQUIRED
    store_env=store_env_for_pod(os.environ),
))
res = sb.run(["python", "claude_agent.py", "Say hi"], workspace="examples",
             output_paths=["*.json"], env={"CLAUDE_CODE_OAUTH_TOKEN": "..."})
```

Requires `RESOLUTO_STORE_KIND` in the environment (the conduit is how the pod's
workspace/artifacts travel). Workspace is staged into the store and extracted back into
your `workspace` dir in place.

### k8s hard limit

- **No `stdin`** → `NotImplementedError`. Pass input via argv, env, or workspace files.

Everything else works. `RunResult.errors` is empty by design — the in-pod runner
emits stdout+stderr as merged `log` events, so `RunResult.output` carries both. `RunResult.reason`
carries pod forensics (e.g. `OOMKilled`, evicted) when present.

### `EgressConfig` (default-deny pod egress)

`from resoluto_sandbox.runtime.k8s import EgressConfig` — frozen dataclass; ALL fields
MUST be CIDR notation (NetworkPolicy `ipBlock` has no FQDN support — resolve hostnames
to IPs yourself, else `ValueError`):

```python
EgressConfig(
    store_cidr="10.0.0.5/32",          # object store endpoint
    llm_cidr="160.79.104.0/23",        # LLM provider API (e.g. api.anthropic.com), resolve to a CIDR
    git_cidrs=[],                       # git hosts; default [] = no git egress
)
```

Applied: default-deny + the declared CIDRs on TCP/443 + kube-dns UDP/53. `egress=None`
⇒ unrestricted egress (Kata kernel isolation only).

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

# resoluto-sandbox

Run a program in isolation and exchange data through a durable store. Your program stays plain — it
reads `argv`, writes `stdout`/files, exits, and never imports `resoluto.sandbox`. A script that runs
with `uv run agent.py` on your machine runs unchanged inside the sandbox.

<p align="left">
  <img alt="python" src="https://img.shields.io/badge/python-3.12%2B-blue">
  <img alt="status" src="https://img.shields.io/badge/status-alpha-orange">
</p>

---

## Install

```bash
pip install resoluto-sandbox   # published wheel coming; for now: pip install -e .
```

---

## Quickstart

```python
from resoluto.sandbox import Sandbox

result = Sandbox(backend="local").run(
    ["python", "-c", "print('hello from the sandbox')"]
)
print(result.output)   # hello from the sandbox
print(result.ok)       # True
```

The result captures the output, the exit code, and any files you asked to collect (`output_paths`).
`stdin` is not supported — pass inputs via argv, env, or workspace files.

> The local backend runs in a Kata microVM and needs a sandbox image present in its **dedicated**
> containerd (not your regular Docker daemon — see [Prebuilt provider
> images](#prebuilt-provider-images) for the transfer step) — pass `Sandbox(backend="local",
> image="…")` (default `resoluto-sandbox-base:<installed wheel version>` — `default_local_image()`,
> never a floating tag; build it from `Dockerfile.base` or `resoluto-sandbox image build`). Run argv with
> the **guest's** `python` and paths relative to `workspace`, not host absolute paths.

**Run an arbitrary program, isolated** — the sandbox runs any untrusted program; a real LLM agent is
just one example. `run_agent_in_sandbox.py` runs a plain Claude agent (`examples/payloads/claude_agent.py`,
which never imports the library) inside a Kata microVM with egress locked to the LLM, then prints its
input → output:

```bash
set -a; source local.env; set +a          # exports RESOLUTO_SANDBOX_IMAGE (provision the backend first)
uv run python examples/run_agent_in_sandbox.py "In five words, why isolate an agent?"
#   INPUT  (prompt) : 'In five words, why isolate an agent?'
#   OUTPUT (answer) : 'Untrusted code cannot escape containment.'
```

For the bare mechanics without an LLM, see `examples/run_hello_in_sandbox.py`. The end-to-end
verification harnesses that drive a minimal agent through BOTH backends (local + k8s) live in
`tests/smoke/` (`smoke_both_backends.py`, `smoke_llm.py`).

---

## The program contract

A sandbox program reads `argv`, writes to `stdout` / files, exits with a code, and never imports
`resoluto.sandbox`. A script that works as `uv run agent.py` works unchanged inside the sandbox; test
runners, LLM agents, and shell scripts all qualify. Dependencies are your program's concern — put
`uv run` / `pip install` in your argv, or bake them into the image.

---

## How it works

The host and the sandbox never hold a live connection. They rendezvous through a durable key/value
store (the **Conduit**): the sandbox is passive — it writes append-only JSONL chunks and a final
`result.json`; the host launches it, tails the chunks, and reaps it. The same flow works whether the
sandbox is a microVM next to you or a pod in a cluster, and a network blip can't wedge a run.

### Components

| Component | What it is | What it does |
|---|---|---|
| **Your program** | Any script/binary — plain | Reads `argv`/env, writes `stdout`/files, exits. Never imports `resoluto.sandbox`. |
| **`Sandbox`** | Thin Python facade | `Sandbox(backend=...).run(argv, ...)` — one call, identical for every backend. |
| **`SubstrateBackend`** | The one backend impl | Drives the 3-phase flow (stage → run → collect). Holds one `SandboxRuntime` + one `Conduit`. |
| **`SandboxRuntime`** (ABC) | The isolation/placement seam | Launches, checks status, destroys the isolated sandbox. Impls: `KataNerdctlSandboxRuntime` (local), `K8sSandboxRuntime` (k8s). |
| **`Conduit`** (ABC) | The durable exchange seam | `put` / `get` / `list_prefix` on a key/value store — the ONLY channel between host and sandbox. Impls: `LocalConduit` (bind-mounted dir), `S3Conduit` (minio/S3), `StdoutConduit`, `GcsConduit` (unverified). |
| **`runner_main`** | The in-guest entrypoint | Runs inside the sandbox only — your program never sees it. Verifies the egress canary, stages inputs from the Conduit, execs your `argv`, ships spans/heartbeat/result back to the Conduit. |
| **sandbox image** | The OCI image the sandbox boots | Must contain your program's runtime + the `resoluto-sandbox` wheel. Prebuilt overlays: `resoluto-sandbox:claude-agent-sdk-<ver>`, `:langchain-<ver>`, `:openai-agents-<ver>` (see [Images](#prebuilt-provider-images) below). |

### Architecture — local vs. k8s

Same isolation model on both backends — a Kata microVM with its own guest kernel, default-deny
egress until you open exactly what you need, and a fail-closed canary that verifies isolation
*before* your program runs. Only the placement and the Conduit implementation differ:

```
LOCAL BACKEND  (single host, no cluster)
────────────────────────────────────────
┌────────────────────────────────────────────────┐
│ Host process (your code)                       │
│   Sandbox(backend="local")                     │
└────────────────────────────────────────────────┘
                    nerdctl
                    (dedicated containerd)
                     │
                     ▼
┌────────────────────────────────────────────────┐
│ ISOLATED: Kata microVM                         │
│ (own guest kernel)                             │
│ runner_main -> your argv                       │
│ egress: host CNI bridge, default-deny          │
│ (DNS + store only, until opened)               │
└────────────────────────────────────────────────┘
                    bind mount
                     │
                     ▼
LocalConduit (host dir)
```

```
K8S BACKEND  (Kubernetes cluster)
─────────────────────────────────
┌────────────────────────────────────────────────┐
│ Host process (your code)                       │
│   Sandbox(backend=SubstrateBackend(            │
│     K8sSandboxRuntime(...)))                   │
└────────────────────────────────────────────────┘
                    kube API
                    (pinned context)
                     │
                     ▼
┌────────────────────────────────────────────────┐
│ ISOLATED: Kata microVM pod                     │
│ (own guest kernel)                             │
│ runner_main -> your argv                       │
│ egress: k8s NetworkPolicy, default-deny        │
│ (DNS + store only, until opened)               │
└────────────────────────────────────────────────┘
                    HTTPS (S3 API)
                     │
                     ▼
S3Conduit (minio / S3 bucket)
```

`local` needs `/dev/kvm` + `nerdctl` + a dedicated containerd, provisioned by
`scripts/local-backend-up.sh` (ends in a green Kata-microVM canary). `k8s` needs a cluster with Kata
Containers installed, an S3-compatible store, and a pinned kube context. Full setup —
including the vendor-neutral k8s stack — is in [`docs/backends.md`](docs/backends.md).

### Data flow — the Conduit

The host and the sandbox **never talk to each other directly** — every arrow below touches the
Conduit, never the other side. That's what makes a network blip harmless: nothing is waiting on a
live connection.

| Phase | Host does | Conduit holds | Sandbox does |
|---|---|---|---|
| **1. Stage** | `put_dir(workspace)` — writes the input archive | the input archive, keyed to this run | *(not launched yet)* |
| **2. Run** | launches the sandbox, then only tails the Conduit | spans + heartbeat + `result.json` + output archive, as `runner_main` writes them | canary → stages inputs *from* the Conduit → execs your `argv` → ships output *to* the Conduit |
| **3. Collect** | `list_prefix` + `get` → assembles `RunResult`, reaps the sandbox | the final chunks, until reaped | destroyed |

Live output still streams to `stream` (default `sys.stdout`) as it happens during **2. Run** — the
table above is what's durable, not the only thing you see.

### Security model — layers of defense

A sandbox for untrusted code is locked down at every layer, outside-in:

| Layer | What it does |
|---|---|
| **1. Network** | Default-deny egress — host CNI bridge (`local`) or `NetworkPolicy` (`k8s`). A fresh sandbox reaches only DNS + its own Conduit; you open exactly the domains a step needs, per `run()`. |
| **2. Isolation** | The program runs in a Kata microVM — a real, separate guest kernel, not a namespace/cgroup container. In-guest root cannot escape it or see the host's devices. |
| **3. Verification** | An in-guest egress canary runs fail-closed before your program does — if isolation can't be proven, the run refuses to start rather than silently running unprotected. |
| **4. Blocked destinations** | Cloud IMDS (`169.254.169.254`) and RFC1918 private ranges are rejected even on an allowlist match — an opened domain can never pivot into the host's private network. |

### Egress — DENY by default (secure)

A sandbox for untrusted code is **locked down by default**: a fresh sandbox can reach **only DNS and its
object store** — no internet, no LLM, no registries. It cannot phone home. You **opt in** to exactly
the **domains** each step needs, **per `run()`** — no re-provision between steps:

```python
Sandbox(backend="local").run(argv, egress=["api.anthropic.com"])                    # only Anthropic
Sandbox(backend="local").run(argv, egress=["registry.npmjs.org", "*.openai.com"])   # npm + any OpenAI host
Sandbox(backend="local").run(argv)                                                  # None → deny all (secure default)
```

Under the hood a built-in **SNI egress proxy** reads that step's allowlist live and forwards only
connections whose TLS **SNI** matches — exact (`api.anthropic.com`) or `*.wildcard` (`*.openai.com`).
It allows by **domain, not IP** (so it never goes stale for CDN-backed APIs behind rotating IPs), does
no IP pinning and no CA/MITM, and refuses internal/IMDS destinations even on a match. `None`/`[]` →
the secure default (DNS + object store only). One-time setup runs the proxy: `scripts/local-backend-up.sh`.

Verified end-to-end, back-to-back with **no re-provision**: `pnpm add is-odd` installs only when
`registry.npmjs.org` is in that step's `egress`; a real Claude agent answers only when
`api.anthropic.com` is.

> `run(egress=[...])` is enforced by the `local` backend today. On `k8s`, egress is set per-runtime via
> a backend-neutral `EgressConfig` (renders to a default-deny `NetworkPolicy`); `public_https=True` is
> the escape hatch to allow ALL outbound HTTPS for trusted code. Details in
> [`docs/networking.md`](docs/networking.md).

---

## `Sandbox.run()` reference

```python
Sandbox(backend="local").run(
    argv,                 # program + arguments
    *,
    workspace=None,       # dir staged into the sandbox at /workspace; None = nothing staged at all
    stdin=None,           # unsupported — raises NotImplementedError
    env=None,             # dict overlaid on the sandbox environment
    env_file=None,        # dotenv file merged UNDER env (env wins) — convenience, not security (see below)
    secrets=None,         # dict[str, str | SecretKeyRef] — see "Secrets" below
    output_paths=None,    # glob patterns collected back as artifacts
    stream=None,          # live output sink; None echoes to sys.stdout
    egress=None,          # domains allowed for THIS run (local); None/[] = deny all but DNS + store
) -> RunResult
```

`RunResult`: `exit_code`, `output`, `errors` (always empty — stdout/stderr are merged), `artifacts`,
`result` (parsed `result.json` if the program wrote one), `ok` (`exit_code == 0`).

---

## Secrets

Three mechanisms, for three different jobs — none is a drop-in replacement for another:

| Mechanism | Where the plaintext lands | Use for |
|---|---|---|
| `env_file="path/to/.env"` | Literal env entry (pod spec / nerdctl `-e`) — same exposure as `env=` | Local dev config/secrets; pure convenience, not a security boundary |
| `secrets={"VAR": SecretKeyRef("my-secret", "key")}` | Never touches resoluto-sandbox at all — kubelet materializes it | k8s only. References a `Secret` object that already exists (created by `kubectl`, [External Secrets Operator](https://github.com/external-secrets/external-secrets), or anything else) |
| `secrets={"VAR": "vault:secret/data/x#key"}` | Resolved **inside the guest** by a `SecretProvider` — the host never sees the value | Portable across `local`/`k8s`. Ships as an ABC only today — see `secrets.py`; implement a concrete provider (Vault, AWS Secrets Manager, GCP Secret Manager, ...) and dispatch it in `secrets_from_env()` |

```python
from resoluto.sandbox import Sandbox
from resoluto.sandbox.secrets import SecretKeyRef

# env_file — host reads a dotenv file, merges it into the env (env= wins on conflict)
Sandbox(backend="local").run(argv, env_file=".env")

# SecretKeyRef — k8s-native, zero fetch code, ignored on the local backend
Sandbox(backend="k8s", image=...).run(argv, secrets={"ANTHROPIC_API_KEY": SecretKeyRef("anthropic-key", "api_key")})

# SecretProvider ref — guest resolves it itself; host only holds a scoped RESOLUTO_SECRETS_* credential
Sandbox(backend="local").run(argv, secrets={"ANTHROPIC_API_KEY": "vault:secret/data/anthropic#api_key"})
```

The host **never mints credentials** for the `SecretProvider` path — same posture as the Conduit's
`RESOLUTO_STORE_WRITE_TOKEN` today: you mint an already-scoped, short-lived credential yourself
(a Vault token, an AWS STS `AssumeRole` triple, a GCP impersonation-minted OAuth2 token — never a
static key file) and pass it via `RESOLUTO_SECRETS_KIND`/`RESOLUTO_SECRETS_*` env vars; resoluto-sandbox
only plumbs it to the guest and calls `.get(ref)` right before your program execs.

---

## CLI

```bash
resoluto-sandbox run -- echo hi                          # local backend (default)
resoluto-sandbox run --backend k8s --image <img> -- python agent.py
resoluto-sandbox run --env-file .env -- python3 agent.py  # dotenv merged into the sandbox env
resoluto-sandbox doctor                                  # check backend readiness
```

`--` separates sandbox options from the program argv. `--env-file` is CLI-only for now — `secrets=`
(`SecretKeyRef`/`SecretProvider`) is a Python API construct (needs a typed value, not just a string)
and has no CLI flag yet.

---

## Prebuilt provider images

Each overlay pins one SDK version and tags itself by it — the tag says exactly what's inside:

```bash
resoluto-sandbox image build --provider claude      # -> resoluto-sandbox:claude-agent-sdk-0.2.110
resoluto-sandbox image build --provider langchain   # -> resoluto-sandbox:langchain-1.3.11
resoluto-sandbox image build --provider openai      # -> resoluto-sandbox:openai-agents-0.17.7
resoluto-sandbox image build --provider all         # builds the base once, then all three overlays
```

| Provider | Bakes | Example agent | Auth |
|---|---|---|---|
| `claude` | `@anthropic-ai/claude-code` + `claude-agent-sdk` | `examples/payloads/claude_agent.py` | Claude Max/Pro subscription (`claude setup-token`) or `ANTHROPIC_API_KEY` — see [`docs/auth.md`](docs/auth.md) |
| `langchain` | bare `langchain` + `langgraph` — **no LLM integration** | `examples/payloads/langchain_agent.py` | Depends which integration you add — see below |
| `openai` | `openai-agents` | `examples/payloads/openai_agent.py` | `OPENAI_API_KEY` — pay-as-you-go API only, `OPENAI_MODEL` override |

> **The `langchain` image is bare on purpose.** LangChain itself is provider-agnostic — it has no
> built-in way to call an LLM. To actually use it, extend the image with the matching integration
> package for whichever provider you want:
> ```dockerfile
> FROM resoluto-sandbox:langchain-1.3.11
> RUN pip install --break-system-packages langchain-anthropic   # or langchain-openai, etc.
> ```
> `examples/payloads/langchain_agent.py` demonstrates the Anthropic integration specifically (needs
> `langchain-anthropic` + `ANTHROPIC_API_KEY`, model override `ANTHROPIC_MODEL`) — it will
> `ImportError` against the plain prebuilt `langchain` image until you extend it this way.

> **`image build` uses Docker; the local backend reads a *different*, dedicated containerd.**
> `resoluto-sandbox image build` shells out to `docker build`, landing the image in your regular
> Docker daemon. `Sandbox(backend="local")` launches via `nerdctl` against its OWN dedicated
> containerd (`scripts/local-backend-up.sh`'s namespace) — a **separate image store** that never
> sees what plain `docker build`/`docker images` produced. Transfer a built image into it once:
> ```bash
> docker save resoluto-sandbox:langchain-1.3.11 \
>   | sudo "$RESOLUTO_LOCAL_NERDCTL" --address /run/resoluto-local/containerd/containerd.sock \
>       --namespace resoluto-local load
> ```
> Skip this and `Sandbox(backend="local").run(...)` fails with `nerdctl run failed ... pull access
> denied` — it tried (and failed) to pull the tag from a registry instead of finding it locally.

Verified end to end against the real Kata sandbox (all three: canary passes, workspace stages, the
script runs and reaches its auth check). `claude` and `openai` run against the plain prebuilt image;
`langchain` needs the one-line extended image from above (built as `my-langchain-anthropic:0.1.0` here):

```python
from resoluto.sandbox import Sandbox

# workspace="examples/payloads" stages that DIRECTORY'S CONTENTS at /workspace — argv paths are
# relative to that root, never prefixed with the directory again.
r = Sandbox(backend="local", image="my-langchain-anthropic:0.1.0").run(
    ["python3", "langchain_agent.py", "Say hello in five words"],
    workspace="examples/payloads",
    env={"ANTHROPIC_API_KEY": "..."},
)
print(r.output)
```

Swap the `image=` tag + example script + env var to switch providers — everything else is identical:

```python
Sandbox(backend="local", image="resoluto-sandbox:claude-agent-sdk-0.2.110").run(
    ["python3", "claude_agent.py", "Say hello in five words"], workspace="examples/payloads",
    env={"CLAUDE_CODE_OAUTH_TOKEN": "..."},   # never set ANTHROPIC_API_KEY alongside this
)
Sandbox(backend="local", image="resoluto-sandbox:openai-agents-0.17.7").run(
    ["python3", "openai_agent.py", "Say hello in five words"], workspace="examples/payloads",
    env={"OPENAI_API_KEY": "..."},
)
```

Or via the CLI (`--workspace` is REQUIRED to stage anything — without it `/workspace` is empty and
your script won't be found; run from the repo root so `.` stages `examples/` alongside it):

```bash
resoluto-sandbox run --workspace examples/payloads --image resoluto-sandbox:openai-agents-0.17.7 -- python3 openai_agent.py "hi"
```

On `k8s`, retag + push to your registry (`docs/backends.md`), then inject the same tag through
`SubstrateBackend(image=...)` — see [`docs/concepts.md`](docs/concepts.md#k8s) for the full wiring.

---

## Status

| Feature | Status |
|---|---|
| `backend="local"` — Kata microVM via nerdctl + a dedicated containerd, host-side egress | **works today** (run `scripts/local-backend-up.sh`) |
| `backend="k8s"` — Kata microVM pod + object-store Conduit + NetworkPolicy egress | **works today** — needs a Kata cluster + store + kube context |
| `Conduit` + `LocalConduit`, `StdoutConduit`, `S3Conduit` (minio/S3) | **works today** |
| `GcsConduit` | **provided, unverified** — experimental |
| Language-neutral wire spec | **published** — see `spec/PROTOCOL.md` |
| Prebuilt image matrix + `image build` CLI | **works today** — `resoluto-sandbox image build --provider claude\|langchain\|openai\|all` |

---

## Further reading

- `docs/concepts.md` — the program contract, the run lifecycle, the Conduit
- `docs/backends.md` — backend setup + the vendor-neutral k8s stack install
- `docs/networking.md` — egress isolation (the canary + per-backend enforcement)
- `docs/auth.md` — Claude Max/Pro subscription auth (no API key needed)
- `spec/PROTOCOL.md` — the language-neutral host ↔ sandbox wire protocol
- `examples/` — start at `run_agent_in_sandbox.py` (a real Claude agent isolated in a Kata microVM)
  or `run_hello_in_sandbox.py` (the bare mechanics); `payloads/` holds the plain programs run inside
  (`hello.py`, `claude_agent.py`, `langchain_agent.py`, `openai_agent.py`, one per prebuilt provider
  image). See [`examples/README.md`](examples/README.md).

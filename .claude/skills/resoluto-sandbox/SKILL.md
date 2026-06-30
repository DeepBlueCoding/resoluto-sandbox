---
name: resoluto-sandbox
description: Use when running a program or AI agent inside the resoluto-sandbox from your own system — calling Sandbox.run(), choosing the local or k8s backend, bringing your own agent (any language), configuring egress/network isolation, the prebuilt SDK images, the CLI, or Claude Max-subscription auth.
---

# resoluto-sandbox (power user)

Run any program — script, CLI, or AI agent in any language — in an isolated sandbox. **Mental model:** your program is *plain* — reads argv, writes stdout/files, NEVER imports `resoluto_sandbox`. What runs as `uv run agent.py` on your host runs unchanged under `run()`; the backend changes only *where* (Kata microVM via nerdctl locally, Kata pod on k8s).

```python
from resoluto_sandbox import Sandbox
r = Sandbox(backend="local").run(["python", "agent.py"], workspace="./work",
                                 output_paths=["out/*.json"])
# RunResult(pydantic): exit_code:int output/errors:str artifacts:list[str] result:dict|None reason:str ok(prop ==exit0)
```

Both backends merge stdout+stderr into `output` (`errors` empty by design). `stdin` is NOT supported on either backend.

## Quick reference

| Goal | How |
|---|---|
| Run locally (Kata microVM via nerdctl) | `Sandbox(backend="local").run(argv, ...)` — needs `/dev/kvm` + nerdctl + the dedicated containerd + an image |
| Run in Kata pod | `Sandbox(backend=SubstrateBackend(runtime=K8sSandboxRuntime(...), conduit=store_from_env(), image="<tag>", store_env=store_env_for_pod(os.environ))).run(argv, ...)` |
| Collect outputs | `output_paths=["dist/*","*.json"]` → globbed into `r.artifacts`; mutated into `workspace` |
| Structured result | program writes `result.json` in workspace → `r.result` |
| Dependencies | put `uv run`/`pip install` in your argv, or use a prebuilt image |
| Restrict k8s egress | `K8sSandboxRuntime(egress=EgressConfig(store_cidr=..., store_port=443))` — store + ALL public 443 (LLM/git, no per-host) + DNS; IMDS denied |
| Pick store conduit | inject `conduit=` to `SubstrateBackend`; else `store_from_env()` via `RESOLUTO_STORE_KIND` |
| CLI | `resoluto-sandbox run [--backend local\|k8s] [--image T] -- <prog> [args]` ; also `doctor`, `image build --provider claude\|langchain\|openai\|all` |
| Build SDK image | `resoluto-sandbox image build --provider claude` (tag locked to wheel version) |
| Claude Max auth | local: log in once (`claude` / `claude setup-token`), do NOT set `ANTHROPIC_API_KEY` |

Imports: `from resoluto_sandbox import Sandbox, RunResult`; `from resoluto_sandbox.backends.substrate import SubstrateBackend, store_env_for_pod`; `from resoluto_sandbox.runtime.k8s import K8sSandboxRuntime, EgressConfig`; `from resoluto_sandbox.conduit.factory import store_from_env`.

**Limits on both backends:** no `stdin` (raises `NotImplementedError`). Dependencies must be baked into the image (or passed via argv for local). `RunResult.errors` is always empty; the in-sandbox runner merges both streams into output.

## Footguns

- **k8s egress is UNRESTRICTED by default** (`egress=None`) — Kata kernel isolation only. Pass `EgressConfig` for default-deny.
- **`local` = Kata microVM via nerdctl (hardware-virtualized, NOT a plain namespace/cgroup container).** Each sandbox runs as a Kata microVM via `nerdctl` against a dedicated, standalone containerd (own socket/root at `/run/resoluto-local/containerd/`) — VM-grade isolation at parity with k8s, on a single host, no cluster. The egress canary RUNS (fail-closed); local egress is enforced HOST-SIDE on the lane CNI bridge (default-deny; allow DNS + HTTPS-443-public; REJECT IMDS + RFC1918 private) — immune to in-guest root. Suitable for untrusted code at VM-grade isolation, same as k8s. NOT a bare host subprocess.
- **Local backend needs an image.** Default `resoluto-sandbox-base:dev`; override with `Sandbox(backend="local", image="...")`. The image must contain python + the resoluto-sandbox wheel + your program's deps. Needs `/dev/kvm`, the `nerdctl` client, and the dedicated containerd up (`scripts/local-backend-up.sh`).
- **Image tag == wheel version.** An image built for a different `resoluto-sandbox` version won't match — rebuild after upgrading.
- **No wall-clock timeouts.** Liveness = substrate-silence (`dead_after_s=600` between chunks) + heartbeat; a live program runs as long as it stays alive.

## Deep references

- `references/usage.md` — calling `run()`: full args, output collection, `result.json`, CLI.
- `references/agents.md` — bringing your own agent (any language) or Claude Max-subscription auth.
- `references/networking.md` — `EgressConfig`/NetworkPolicy, conduits, store env. local/stdout and S3-against-minio proven; `GcsConduit` experimental/unverified.
- `references/operations.md` — building/publishing images, k8s+Kata setup (any distribution), debugging pod phase/`reason`.
- `references/recipes.md` — copy-paste end-to-end snippets.

Wire protocol: `../../../spec/PROTOCOL.md`. Overview: `../../../README.md`.

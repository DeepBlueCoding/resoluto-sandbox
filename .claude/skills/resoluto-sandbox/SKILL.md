---
name: resoluto-sandbox
description: Use when running a program or AI agent inside the resoluto-sandbox from your own system — calling Sandbox.run(), choosing the local or k8s backend, bringing your own agent (any language), configuring egress/network isolation, the prebuilt SDK images, the CLI, or Claude Max-subscription auth.
---

# resoluto-sandbox (power user)

Run any program — script, CLI, or AI agent in any language — in an isolated sandbox. **Mental model:** your program is *plain* — reads argv/stdin, writes stdout/files, NEVER imports `resoluto_sandbox`. Guarantee: what runs as `uv run agent.py` on your host runs byte-identically under `run()`; the backend only changes *where*.

```python
from resoluto_sandbox import Sandbox
r = Sandbox(backend="local").run(["python", "agent.py"], workspace="./work",
                                 stdin="hi", output_paths=["out/*.json"])
# RunResult(pydantic): exit_code:int output/errors:str artifacts:list[str] result:dict|None reason:str ok(prop ==exit0)
```

k8s merges stdout+stderr into `output` (`errors` empty by design).

## Quick reference

| Goal | How |
|---|---|
| Run on host (no isolation) | `Sandbox(backend="local").run(argv, ...)` |
| Run in Kata pod | `Sandbox(backend=K8sBackend(image="<tag>")).run(argv, ...)` — needs `RESOLUTO_STORE_KIND` in env |
| Collect outputs | `output_paths=["dist/*","*.json"]` → globbed into `r.artifacts`; mutated into `workspace` |
| Structured result | program writes `result.json` in workspace → `r.result` |
| Dependencies | put `uv run`/`pip install` in your argv, or use a prebuilt image |
| Restrict k8s egress | `K8sBackend(image=..., egress=EgressConfig(store_cidr=..., llm_cidr=..., git_cidrs=[...]))` |
| Pick store conduit | `K8sBackend(conduit=...)`; else `store_from_env()` via `RESOLUTO_STORE_KIND` |
| CLI | `resoluto-sandbox run [--backend local\|k8s] [--image T] -- <prog> [args]` ; also `doctor`, `image build --provider claude\|langchain\|openai\|all` |
| Build SDK image | `resoluto-sandbox image build --provider claude` (tag locked to wheel version) |
| Claude Max auth | local: log in once (`claude` / `claude setup-token`), do NOT set `ANTHROPIC_API_KEY` |

Imports: `from resoluto_sandbox import Sandbox, RunResult`; `from resoluto_sandbox.backends.k8s import K8sBackend`; `from resoluto_sandbox.runtime.k8s import EgressConfig`.

**k8s real limit:** no `stdin` (raises `NotImplementedError`). Dependencies must be baked into the image. Otherwise k8s is fully implemented: a real Kata pod via `drive_node`.

## Footguns

- **k8s egress is UNRESTRICTED by default** (`egress=None`) — Kata kernel isolation only. Pass `EgressConfig` for default-deny.
- **`local` = NO isolation** — host subprocess inheriting the host env. Convenience, not a security boundary.
- **Image tag == wheel version.** An image built for a different `resoluto-sandbox` version won't match — rebuild after upgrading.
- **No wall-clock timeouts.** Liveness = substrate-silence (`dead_after_s=600` between chunks) + heartbeat; a live program runs as long as it stays alive.

## Deep references

- `references/usage.md` — calling `run()`: full args, output collection, `result.json`, CLI.
- `references/agents.md` — bringing your own agent (any language) or Claude Max-subscription auth.
- `references/networking.md` — `EgressConfig`/NetworkPolicy, conduits, store env. local/stdout and S3-against-minio proven; `GcsConduit` experimental/unverified.
- `references/operations.md` — building/publishing images, k8s+Kata setup (any distribution), debugging pod phase/`reason`.
- `references/recipes.md` — copy-paste end-to-end snippets.

Wire protocol: `../../../spec/PROTOCOL.md`. Overview: `../../../README.md`.

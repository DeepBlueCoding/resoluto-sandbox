---
hide:
  - navigation
---

<div class="hero" markdown>

# Resoluto Sandbox

<p class="tagline">Run a program in isolation and exchange data through a durable store. Your program stays plain — it reads <code>argv</code>, writes <code>stdout</code>/files, exits, and never imports <code>resoluto.sandbox</code>. A script that runs with <code>uv run agent.py</code> on your machine runs unchanged inside a Kata microVM or a Kubernetes pod.</p>

[Get started](getting-started.md){ .md-button .md-button--primary }
[API reference](api/sandbox.md){ .md-button }

</div>

```python
from resoluto.sandbox import Sandbox

result = Sandbox(backend="local").run(
    ["python", "-c", "print('hello from the sandbox')"]
)
print(result.output)   # hello from the sandbox
print(result.ok)       # True
```

<div class="grid cards" markdown>

- :material-shield-lock-outline: **VM-grade isolation**

    Every step runs in a Kata microVM — `local` via `nerdctl` on a dedicated containerd, `k8s` as a short-lived pod. Isolation never downgrades; there is no trusted-local bypass.

- :material-transit-connection-variant: **Store-mediated rendezvous**

    Host and sandbox never hold a live connection. They meet through a durable `Conduit` (localfs, S3/minio, GCS) — the sandbox writes append-only JSONL chunks; the host tails and reaps. A network blip can't wedge a run.

- :material-script-text-outline: **Plain program contract**

    Your program reads `argv`/env, writes `stdout`/files, exits — and never imports the sandbox. Test runners, LLM agents, and shell scripts all qualify unchanged.

- :material-cloud-outline: **Cloud-agnostic seams**

    One `SubstrateBackend` drives every backend; only the injected `SandboxRuntime` + `Conduit` vary. A new isolation target or store is a single subclass.

</div>

## Install

```bash
pip install resoluto-sandbox            # base — pydantic-only
pip install "resoluto-sandbox[k8s]"     # Kata pod runtime + S3 conduit
pip install "resoluto-sandbox[s3]"      # S3 / minio conduit
```

The base install is pydantic-only; concrete runtimes and conduits with platform dependencies import
lazily. See **[Getting started](getting-started.md)** for usage, **[Backends](backends.md)** for the
`local` / `k8s` substrates, **[Networking](networking.md)** for egress control, and the
**[API reference](api/sandbox.md)** for the full surface.

!!! tip "For AI agents"
    This site publishes `/llms.txt` (index) and `/llms-full.txt` (full corpus) — point any LLM tool at them to consume these docs directly.

## The Resoluto ecosystem

Three independent, plug-and-play packages under one `resoluto.*` namespace and one design language. Each stands alone — use any without the others.

| Package | Role |
|---------|------|
| [resoluto-sandbox](https://deepbluecoding.github.io/resoluto-sandbox/) | Store-mediated, Kata-isolated execution substrate |
| [resoluto-agent](https://deepbluecoding.github.io/resoluto-agent/) | Pluggable LLM agent-provider plugins |
| [resoluto-engine](https://deepbluecoding.github.io/resoluto-engine/) | Durable orchestrator of sandboxed, gate-verified agentic work |

`resoluto-engine` builds on `resoluto-agent` (the provider contract) and optionally `resoluto-sandbox` (isolation); `resoluto-sandbox` and `resoluto-agent` depend on nothing else in the ecosystem.

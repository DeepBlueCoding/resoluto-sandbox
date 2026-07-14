# Resoluto Sandbox

Run untrusted code — AI-generated, third-party, or adversarial — with a dedicated Linux kernel per run. `resoluto-sandbox` executes any program inside a Kata microVM and exchanges data through a durable store; the workload is assumed hostile and granted nothing by default. Your program stays plain — it reads `argv`, writes `stdout`/files, exits, and never imports `resoluto.sandbox`, so a script that runs with `uv run agent.py` on your machine runs unchanged inside the microVM.

[Get started](https://deepbluecoding.github.io/resoluto-sandbox/0.1.0/getting-started/index.md) [API reference](https://deepbluecoding.github.io/resoluto-sandbox/0.1.0/api/sandbox/index.md)

```python
from resoluto.sandbox import Sandbox

result = Sandbox(backend="local").run(
    ["python", "-c", "print('hello from the sandbox')"]
)
print(result.output)   # hello from the sandbox
print(result.ok)       # True
```

- **Zero-trust, VM-grade isolation**

  Every run is a Kata microVM with its own Linux kernel. The workload runs unprivileged with no capabilities, no host filesystem/devices/credentials, and no network by default. Isolation never downgrades; there is no trusted-local bypass.

- **Store-mediated rendezvous**

  Host and sandbox never hold a live connection. They meet through a durable `Conduit` (localfs, S3/minio, GCS) — the sandbox writes append-only JSONL chunks; the host tails and reaps. A network blip can't wedge a run.

- **Plain program contract**

  Your program reads `argv`/env, writes `stdout`/files, exits — and never imports the sandbox. Test runners, LLM agents, and shell scripts all qualify unchanged.

- **Cloud-agnostic seams**

  One `SubstrateBackend` drives every backend; only the injected `SandboxRuntime` + `Conduit` vary. A new isolation target or store is a single subclass.

## Install

```bash
pip install resoluto-sandbox            # base — pydantic-only
pip install "resoluto-sandbox[k8s]"     # Kata pod runtime + S3 conduit
pip install "resoluto-sandbox[s3]"      # S3 / minio conduit
```

The base install is pydantic-only; concrete runtimes and conduits with platform dependencies import lazily. See **[Getting started](https://deepbluecoding.github.io/resoluto-sandbox/0.1.0/getting-started/index.md)** for usage, **[Backends](https://deepbluecoding.github.io/resoluto-sandbox/0.1.0/backends/index.md)** for the `local` / `k8s` substrates, **[Networking](https://deepbluecoding.github.io/resoluto-sandbox/0.1.0/networking/index.md)** for egress control, and the **[API reference](https://deepbluecoding.github.io/resoluto-sandbox/0.1.0/api/sandbox/index.md)** for the full surface.

For AI agents

This site publishes `/llms.txt` (index) and `/llms-full.txt` (full corpus) — point any LLM tool at them to consume these docs directly.

## The Resoluto ecosystem

Three independent, plug-and-play packages under one `resoluto.*` namespace and one design language. Each stands alone — use any without the others.

| Package                                                                | Role                                                          |
| ---------------------------------------------------------------------- | ------------------------------------------------------------- |
| [resoluto-sandbox](https://deepbluecoding.github.io/resoluto-sandbox/) | Store-mediated, Kata-isolated execution substrate             |
| [resoluto-agent](https://deepbluecoding.github.io/resoluto-agent/)     | Pluggable LLM agent-provider plugins                          |
| [resoluto-engine](https://deepbluecoding.github.io/resoluto-engine/)   | Durable orchestrator of sandboxed, gate-verified agentic work |

`resoluto-engine` builds on `resoluto-agent` (the provider contract) and optionally `resoluto-sandbox` (isolation); `resoluto-sandbox` and `resoluto-agent` depend on nothing else in the ecosystem.

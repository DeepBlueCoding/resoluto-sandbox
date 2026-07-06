# Resoluto Sandbox

Run a program in isolation and exchange data through a durable store. Your program stays plain — it reads `argv`, writes `stdout`/files, exits, and never imports `resoluto.sandbox`. A script that runs with `uv run agent.py` on your machine runs unchanged inside a Kata microVM or a Kubernetes pod.

[Get started](https://deepbluecoding.github.io/resoluto-sandbox/main/getting-started/index.md) [API reference](https://deepbluecoding.github.io/resoluto-sandbox/main/api/sandbox/index.md)

```python
from resoluto.sandbox import Sandbox

result = Sandbox(backend="local").run(
    ["python", "-c", "print('hello from the sandbox')"]
)
print(result.output)   # hello from the sandbox
print(result.ok)       # True
```

- **VM-grade isolation**

  Every step runs in a Kata microVM — `local` via `nerdctl` on a dedicated containerd, `k8s` as a short-lived pod. Isolation never downgrades; there is no trusted-local bypass.

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

The base install is pydantic-only; concrete runtimes and conduits with platform dependencies import lazily. See **[Getting started](https://deepbluecoding.github.io/resoluto-sandbox/main/getting-started/index.md)** for usage, **[Backends](https://deepbluecoding.github.io/resoluto-sandbox/main/backends/index.md)** for the `local` / `k8s` substrates, **[Networking](https://deepbluecoding.github.io/resoluto-sandbox/main/networking/index.md)** for egress control, and the **[API reference](https://deepbluecoding.github.io/resoluto-sandbox/main/api/sandbox/index.md)** for the full surface.

For AI agents

This site publishes `/llms.txt` (index) and `/llms-full.txt` (full corpus) — point any LLM tool at them to consume these docs directly.

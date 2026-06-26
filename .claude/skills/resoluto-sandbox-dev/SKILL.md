---
name: resoluto-sandbox-dev
description: Use when developing the resoluto-sandbox itself — adding a new SandboxRuntime or Backend or Conduit, working on the substrate internals (drive_node, runner, telemetry, staging, the k8s runtime), the wire protocol, or running its tests and contributing.
---

# resoluto-sandbox — developer guide

Run a plain program (reads argv, writes stdout/files, never imports this package) in a sandbox. What works as `uv run agent.py` on the host works unchanged inside the sandbox — in a Docker container (`local`) or Kata pod (`k8s`).

## Mental model

Two seams. **SandboxRuntime** = the isolation/placement mechanism (Docker locally, Kata pod on k8s). **Conduit** = the durable store both sides rendezvous through (host never holds a connection to the guest). ONE `SubstrateBackend` drives both presets — what varies is the injected runtime + conduit.

For `k8s`: the guest self-reports append-only JSONL chunks to its Conduit prefix; `drive_node` tails + reaps. Store-mediated, so no long-lived stream to wedge.

```python
from resoluto_sandbox import Sandbox
from resoluto_sandbox.backends.substrate import SubstrateBackend, store_env_for_pod
from resoluto_sandbox.conduit.factory import store_from_env
from resoluto_sandbox.runtime.k8s import K8sSandboxRuntime, EgressConfig
import os

# local: Docker container on this host
r = Sandbox(backend="local").run(["agent.py"], workspace="/work")
print(r.output, r.ok)   # RunResult(exit_code, output, errors, artifacts, result, reason, ok)

# k8s: Kata pod — inject SubstrateBackend
egress = EgressConfig(store_cidr="10.0.0.5/32", llm_cidr="1.2.3.4/32")
runtime = K8sSandboxRuntime(
    namespace="resoluto-sandboxes",
    context=os.environ.get("RESOLUTO_SANDBOX_KUBECONTEXT"),
    egress=egress,
)
Sandbox(backend=SubstrateBackend(
    runtime=runtime,
    conduit=store_from_env(),
    image="ghcr.io/...:TAG",
    store_env=store_env_for_pod(os.environ),
)).run(["python", "agent.py"], workspace="/work", output_paths=["out/*.json"])
```

`Sandbox(backend="local"|"k8s"|<Backend instance>)`. `.run(argv, *, workspace, stdin, env, output_paths, stream) -> RunResult`. On both backends: `stdin` raises `NotImplementedError`; `RunResult.errors` is empty (output carries merged stdout+stderr). `k8s` also needs `RESOLUTO_STORE_KIND` in env.

## Quick reference

| Operation | How |
|-----------|-----|
| Run locally | `Sandbox(backend="local").run(argv, workspace=...)` — needs Docker + an image |
| Run in Kata pod | `Sandbox(backend=SubstrateBackend(runtime=K8sSandboxRuntime(...), conduit=..., image=..., store_env=store_env_for_pod(os.environ))).run(...)` |
| Restrict pod egress | `K8sSandboxRuntime(egress=EgressConfig(store_cidr=, llm_cidr=, git_cidrs=[]))` (CIDRs only, no FQDNs) |
| Collect outputs | `output_paths=["out/*.json"]` → globbed into `RunResult.artifacts` (extracted into `workspace`) |
| Read structured result | program writes `result.json` → `RunResult.result: dict | None` |
| Add a new runtime | subclass `contracts.py:SandboxRuntime` (`launch`/`status`/`destroy`/`sweep`), wire into `SubstrateBackend` |
| Add a Backend | subclass `backends/base.py:Backend`, implement `run(...) -> RunResult` |
| Add a Conduit | subclass `contracts.py:Conduit` (`put`/`get`/`list_prefix`; override `copy_prefix` for server-side copy) |
| Build conduit from env | `conduit/factory.py:store_from_env()` keys on `RESOLUTO_STORE_KIND` |
| Run tests | `uv run pytest`; live cluster tests are `@integration` (default run skips real pods) |

Proven conduits: `local`/`stdout` (local backend) and `s3` against minio (k8s). `GcsConduit` is experimental/unverified (no GCP creds in the env — contract-parity only; run a real-bucket conformance suite first).

## Footguns

- **k8s egress defaults to UNRESTRICTED** — Kata kernel isolation only. Pass `egress=EgressConfig(...)` for a default-deny NetworkPolicy (allow declared CIDRs on 443 + kube-dns 53).
- **`local` = Docker (OS-level isolation, NOT egress-locked)** — runs in a Docker container. Needs Docker + an image (default `resoluto-sandbox-runner:dev`). NOT a bare host subprocess.
- **`stdin` NOT supported on either backend** — both raise `NotImplementedError`. Pass inputs via argv, env, or workspace files.
- **Image tag == wheel** — the `k8s` image bakes a specific build; sandbox-side code changes need a rebuild+republish. The host gets source changes instantly → they drift. Bump the tag.
- **No wall-clock timeouts** — liveness is substrate-silence (`dead_after_s` since last chunk) + heartbeat, never a duration cap on the work.

## Deep references

- `references/architecture.md` — load this when you need the layer map (SubstrateBackend, SandboxRuntime vs Conduit seams) and the store-mediated comms model.
- `references/extending.md` — load this when adding a new SandboxRuntime, Backend, or Conduit, or wiring a new `RESOLUTO_STORE_KIND`.
- `references/internals.md` — load this when working inside `drive_node`, `runner`/`runner_main`, `telemetry` (ChunkShipper/ChunkReader), `staging`, or the k8s runtime.
- `references/testing-and-contributing.md` — load this when running/writing tests (unit vs `@integration` on a Kubernetes+Kata cluster) or preparing a contribution.
- `spec/PROTOCOL.md` — load this when changing the host↔sandbox wire (JSONL chunks, result.json, gzip-tar archives) or implementing a non-Python client.

---
name: resoluto-sandbox-dev
description: Use when developing the resoluto-sandbox itself — adding a new Backend or Conduit, working on the substrate internals (drive_node, runner, telemetry, staging, the k8s runtime), the wire protocol, or running its tests and contributing.
---

# resoluto-sandbox — developer guide

Run a plain program (reads argv/stdin, writes stdout/files, never imports this package) in a sandbox. It runs byte-identically to `uv run agent.py` on the host.

## Mental model

Two seams. **Backend** = WHERE it runs (`local` subprocess / `k8s` Kata pod). **Conduit** = the durable store both sides rendezvous through (host never holds a connection to the guest). For `k8s`: the guest self-reports append-only JSONL chunks to its Conduit prefix; `drive_node` tails + reaps. Store-mediated, so no long-lived stream to wedge.

```python
from resoluto_sandbox import Sandbox
from resoluto_sandbox.backends.k8s import K8sBackend
from resoluto_sandbox.runtime.k8s import EgressConfig

r = Sandbox(backend="local").run(["agent.py"], workspace="/work", stdin="hi")
print(r.output, r.ok)   # RunResult(exit_code, output, errors, artifacts, result, reason, ok)

# k8s: backend is injected and configured (image is a backend concern)
egress = EgressConfig(store_cidr="10.0.0.5/32", llm_cidr="1.2.3.4/32")
Sandbox(backend=K8sBackend(image="ghcr.io/...:TAG", egress=egress)).run(
    ["python", "agent.py"], workspace="/work", output_paths=["out/*.json"])
```

`Sandbox(backend="local"|"k8s"|<Backend instance>)`. `.run(argv, *, workspace, stdin, env, output_paths, stream) -> RunResult`. On `k8s`: `stdin` raises `NotImplementedError` (bake deps into the image); `RunResult.errors` is empty (output carries merged stdout+stderr); needs `RESOLUTO_STORE_KIND` in env.

## Quick reference

| Operation | How |
|-----------|-----|
| Run locally | `Sandbox(backend="local").run(argv, workspace=...)` |
| Run in Kata pod | `Sandbox(backend=K8sBackend(image=..., conduit=..., egress=...)).run(...)` |
| Restrict pod egress | `K8sBackend(egress=EgressConfig(store_cidr=, llm_cidr=, git_cidrs=[]))` (CIDRs only, no FQDNs) |
| Collect outputs | `output_paths=["out/*.json"]` → globbed into `RunResult.artifacts` (extracted into `workspace`) |
| Read structured result | program writes `result.json` → `RunResult.result: dict | None` |
| Add a Backend | subclass `backends/base.py:Backend`, implement `run(...) -> RunResult` |
| Add a Conduit | subclass `contracts.py:Conduit` (`put`/`get`/`list_prefix`; override `copy_prefix` for server-side copy) |
| Add a runtime | subclass `contracts.py:SandboxRuntime` (`launch`/`status`/`destroy`/`sweep`) |
| Build conduit from env | `conduit/factory.py:store_from_env()` keys on `RESOLUTO_STORE_KIND` |
| Run tests | `uv run pytest`; live cluster tests are `@integration` (default run skips real pods) |

Proven conduits: `local`/`stdout` (local backend) and `s3` against minio (k8s). `GcsConduit` is experimental/unverified (no GCP creds in the env — contract-parity only; run a real-bucket conformance suite first).

## Footguns

- **k8s egress defaults to UNRESTRICTED** — Kata kernel isolation only. Pass `egress=EgressConfig(...)` for a default-deny NetworkPolicy (allow declared CIDRs on 443 + kube-dns 53).
- **`local` = NO isolation** — runs as a host subprocess inheriting the host env. Never feed it untrusted argv/code.
- **Image tag == wheel** — the `k8s` image bakes a specific build; pod-side code changes need a rebuild+republish. The host gets source changes instantly → they drift. Bump the tag.
- **No wall-clock timeouts** — liveness is substrate-silence (`dead_after_s` since last chunk) + heartbeat, never a duration cap on the work.

## Deep references

- `references/architecture.md` — load this when you need the layer map (Backend vs Conduit vs SandboxRuntime/Admission seams) and the store-mediated comms model.
- `references/extending.md` — load this when adding a new Backend, Conduit, or runtime, or wiring a new `RESOLUTO_STORE_KIND`.
- `references/internals.md` — load this when working inside `drive_node`, `runner`/`runner_main`, `telemetry` (ChunkShipper/ChunkReader), `staging`, or the k8s runtime.
- `references/testing-and-contributing.md` — load this when running/writing tests (unit vs `@integration` on a Kubernetes+Kata cluster) or preparing a contribution.
- `spec/PROTOCOL.md` — load this when changing the host↔sandbox wire (JSONL chunks, result.json, gzip-tar archives) or implementing a non-Python client.

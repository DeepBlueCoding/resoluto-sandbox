---
name: resoluto-sandbox-dev
description: Use when developing the resoluto-sandbox itself — adding a new SandboxRuntime or Backend or Conduit, working on the substrate internals (drive_node, runner, telemetry, staging, the k8s runtime), the wire protocol, or running its tests and contributing.
---

# resoluto-sandbox — developer guide

Run a plain program (reads argv, writes stdout/files, never imports this package) in a sandbox. What works as `uv run agent.py` on the host works unchanged inside the sandbox — in a Kata microVM via nerdctl (`local`) or a Kata pod on k8s (`k8s`).

## Mental model

Two seams. **SandboxRuntime** = the isolation/placement mechanism (a Kata microVM via nerdctl + a dedicated containerd locally, a Kata pod on k8s). **Conduit** = the durable store both sides rendezvous through (host never holds a connection to the guest). ONE `SubstrateBackend` drives both backends — what varies is the injected runtime + conduit.

For `k8s`: the guest self-reports append-only JSONL chunks to its Conduit prefix; `drive_node` tails + reaps. Store-mediated, so no long-lived stream to wedge.

```python
from resoluto_sandbox import Sandbox
from resoluto_sandbox.backends.substrate import SubstrateBackend, store_env_for_pod
from resoluto_sandbox.conduit.factory import store_from_env
from resoluto_sandbox.runtime.k8s import K8sSandboxRuntime
from resoluto_sandbox.egress import EgressConfig   # backend-neutral allowlist; re-exported from runtime.k8s
import os

# local: Kata microVM (via nerdctl + a dedicated containerd) on this host
r = Sandbox(backend="local").run(["agent.py"], workspace="/work")
print(r.output, r.ok)   # RunResult(exit_code, output, errors, artifacts, result, reason, ok)

# k8s: Kata pod — inject SubstrateBackend
egress = EgressConfig(store_cidr="10.0.0.5/32", store_port=443, allow=["api.anthropic.com","registry.npmjs.org","pypi.org"])   # SECURE BY DEFAULT: EgressConfig() = store + DNS only; allow=[...] opens dests (least privilege); public_https=True = escape hatch (all :443). IMDS denied
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

`Sandbox(backend="local"|"k8s"|<Backend instance>)` (default `"local"`). `.run(argv, *, workspace, stdin, env, output_paths, stream, egress) -> RunResult` (`egress=[domains]` opens those domains for THAT run on `local` via the SNI proxy, cleared after; `None`/`[]` = deny all but DNS+store). On both backends: `stdin` raises `NotImplementedError`; `RunResult.errors` is empty (output carries merged stdout+stderr). `k8s` also needs `RESOLUTO_STORE_KIND` in env.

## Quick reference

| Operation | How |
|-----------|-----|
| Run locally | `Sandbox(backend="local").run(argv, workspace=...)` — needs `/dev/kvm`, `nerdctl`, the dedicated containerd + an image |
| Run in Kata pod | `Sandbox(backend=SubstrateBackend(runtime=K8sSandboxRuntime(...), conduit=..., image=..., store_env=store_env_for_pod(os.environ))).run(...)` |
| Restrict egress (k8s + local) | `EgressConfig(store_cidr=…, allow=["api.anthropic.com","registry.npmjs.org"])` — backend-neutral (renders to NetworkPolicy OR iptables); SECURE BY DEFAULT (store + DNS only); `allow=[...]` opens dests, `public_https=True` = escape hatch (all :443); env `RESOLUTO_EGRESS_ALLOW`/`_ALLOW_PORT`/`_PUBLIC_HTTPS` (default 0/deny). On local prefer per-run `run(egress=["api.anthropic.com"])` (SNI-proxy, by domain) |
| Collect outputs | `output_paths=["out/*.json"]` → globbed into `RunResult.artifacts` (extracted into `workspace`) |
| Read structured result | program writes `result.json` → `RunResult.result: dict | None` |
| Add a new runtime | subclass `contracts.py:SandboxRuntime` (`launch`/`status`/`destroy`/`sweep`), wire into `SubstrateBackend` |
| Add a Backend | subclass `backends/base.py:Backend`, implement `run(...) -> RunResult` |
| Add a Conduit | subclass `contracts.py:Conduit` (`put`/`get`/`list_prefix`; override `copy_prefix` for server-side copy) |
| Build conduit from env | `conduit/factory.py:store_from_env()` keys on `RESOLUTO_STORE_KIND` |
| Run tests | `uv run pytest`; live cluster tests are `@integration` (default run skips real pods) |

Proven conduits: `local`/`stdout` (local backend) and `s3` against minio (k8s). `GcsConduit` is experimental/unverified (no GCP creds in the env — contract-parity only; run a real-bucket conformance suite first).

## Footguns

- **`egress=None` is the k8s opt-OUT** (no NetworkPolicy → UNRESTRICTED, Kata kernel isolation only) — DIFFERENT from `EgressConfig()`, which is SECURE BY DEFAULT (store + DNS only). Pass `egress=EgressConfig(...)` and open what you need. `EgressConfig` is **backend-neutral** (`resoluto_sandbox.egress`): two renderers — `k8s_egress_rules()` (NetworkPolicy) and `local_egress_iptables()` (iptables) — drive the SAME config on both backends; knobs `allow` / `allow_port` (least privilege) / `public_https` (escape hatch, default False) — env `RESOLUTO_EGRESS_ALLOW` / `_ALLOW_PORT` / `_PUBLIC_HTTPS` (default 0/deny). A new provider = one new renderer.
- **`local` = Kata microVM (hardware-virtualized) via nerdctl + a dedicated containerd** — each sandbox is a Kata microVM (VM-grade isolation, parity with k8s, single host, no cluster), NOT a Docker container, NOT a bare host subprocess. Needs `/dev/kvm`, `nerdctl`, the dedicated containerd up (`scripts/local-backend-up.sh`) + an image (default `resoluto-sandbox-base:<installed wheel version>` (`default_local_image()`), never a floating tag). The egress canary RUNS (fail-closed); local egress is enforced HOST-SIDE on the lane CNI bridge (default-deny: store + DNS only until you opt in via `RESOLUTO_EGRESS_ALLOW` / `_PUBLIC_HTTPS`; REJECT IMDS + RFC1918) — immune to in-guest root.
- **`stdin` NOT supported on either backend** — both raise `NotImplementedError`. Pass inputs via argv, env, or workspace files.
- **Image tag == wheel** — the `k8s` image bakes a specific build; sandbox-side code changes need a rebuild+republish. The host gets source changes instantly → they drift. Bump the tag.
- **No wall-clock timeouts** — liveness is substrate-silence (`dead_after_s` since last chunk) + heartbeat, never a duration cap on the work.

## Deep references

- `references/architecture.md` — load this when you need the layer map (SubstrateBackend, SandboxRuntime vs Conduit seams) and the store-mediated comms model.
- `references/extending.md` — load this when adding a new SandboxRuntime, Backend, or Conduit, or wiring a new `RESOLUTO_STORE_KIND`.
- `references/internals.md` — load this when working inside `drive_node`, `runner`/`runner_main`, `telemetry` (ChunkShipper/ChunkReader), `staging`, or the k8s runtime.
- `references/testing-and-contributing.md` — load this when running/writing tests (unit vs `@integration` on a Kubernetes+Kata cluster) or preparing a contribution.
- `spec/PROTOCOL.md` — load this when changing the host↔sandbox wire (JSONL chunks, result.json, gzip-tar archives) or implementing a non-Python client.

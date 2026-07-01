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
| Restrict egress (k8s + local) | `EgressConfig(store_cidr=…, allow=["anthropic","npm","pypi"])` — backend-neutral (renders to k8s NetworkPolicy OR local iptables). SECURE BY DEFAULT: `EgressConfig()` = store + DNS only, nothing else reachable; `allow=[...]` opens specific dests (least privilege), `public_https=True` is the escape hatch (all :443, trusted). Env: `RESOLUTO_EGRESS_ALLOW`/`_ALLOW_PORT`/`_PUBLIC_HTTPS` (default 0/deny, both backends) |
| Pick store conduit | inject `conduit=` to `SubstrateBackend`; else `store_from_env()` via `RESOLUTO_STORE_KIND` |
| CLI | `resoluto-sandbox run [--backend local\|k8s] [--image T] -- <prog> [args]` ; also `doctor`, `image build --provider claude\|langchain\|openai\|all` |
| Build SDK image | `resoluto-sandbox image build --provider claude` (tag locked to wheel version) |
| Claude Max auth | local: log in once (`claude` / `claude setup-token`), do NOT set `ANTHROPIC_API_KEY` |

Imports: `from resoluto_sandbox import Sandbox, RunResult`; `from resoluto_sandbox.backends.substrate import SubstrateBackend, store_env_for_pod`; `from resoluto_sandbox.runtime.k8s import K8sSandboxRuntime`; `from resoluto_sandbox.egress import EgressConfig` (canonical home; still re-exported from `resoluto_sandbox.runtime.k8s` for back-compat); `from resoluto_sandbox.conduit.factory import store_from_env`.

**Limits on both backends:** no `stdin` (raises `NotImplementedError`). Dependencies must be baked into the image (or passed via argv for local). `RunResult.errors` is always empty; the in-sandbox runner merges both streams into output.

## Footguns

- **Per-run egress on `local` — `run(argv, egress=["api.anthropic.com"])`.** Opens exactly those
  domains for THAT one run (set on the fly via the persistent SNI proxy's live allowlist file, cleared
  after) — no re-provision between steps. `egress=None`/`[]` → the secure default (DNS + object store
  only). This is the per-step knob; `EgressConfig` below is the per-runtime (k8s) / provision-time (local)
  knob. `run(egress=...)` is applied by `local` today; on `k8s` use `EgressConfig`.
- **`egress=None` is the k8s opt-OUT** — NO NetworkPolicy, unrestricted egress (Kata kernel isolation only); DIFFERENT from `EgressConfig()`, which is SECURE BY DEFAULT (store + DNS only). Pass an `EgressConfig` and open what you need. `EgressConfig` is **backend-neutral** (same config → k8s NetworkPolicy OR local iptables, via the two renderers in `resoluto_sandbox.egress`); knobs `allow` / `allow_port` (least privilege) / `public_https` (escape hatch, default False) — env `RESOLUTO_EGRESS_ALLOW` / `_ALLOW_PORT` / `_PUBLIC_HTTPS` (default 0/deny), honored by both backends.
- **`local` = Kata microVM via nerdctl (hardware-virtualized, NOT a plain namespace/cgroup container).** Each sandbox runs as a Kata microVM via `nerdctl` against a dedicated, standalone containerd (own socket/root at `/run/resoluto-local/containerd/`) — VM-grade isolation at parity with k8s, on a single host, no cluster. The egress canary RUNS (fail-closed); local egress is enforced HOST-SIDE on the lane CNI bridge (default-deny: store + DNS only until you opt in via `RESOLUTO_EGRESS_ALLOW` / `_PUBLIC_HTTPS`; REJECT IMDS + RFC1918 private) — immune to in-guest root. Suitable for untrusted code at VM-grade isolation, same as k8s. NOT a bare host subprocess.
- **Local backend needs an image.** Default `resoluto-sandbox-base:dev`; override with `Sandbox(backend="local", image="...")`. The image must contain python + the resoluto-sandbox wheel + your program's deps. Needs `/dev/kvm`, the `nerdctl` client, and the dedicated containerd up (`scripts/local-backend-up.sh`).
- **Image tag == wheel version.** An image built for a different `resoluto-sandbox` version won't match — rebuild after upgrading.
- **No wall-clock timeouts.** Liveness = substrate-silence (`dead_after_s=600` between chunks) + heartbeat; a live program runs as long as it stays alive.

## Deep references

- `references/usage.md` — calling `run()`: full args, output collection, `result.json`, CLI.
- `references/agents.md` — bringing your own agent (any language) or Claude Max-subscription auth.
- `references/networking.md` — `EgressConfig` (backend-neutral: k8s NetworkPolicy + local iptables), conduits, store env. local/stdout and S3-against-minio proven; `GcsConduit` experimental/unverified.
- `references/operations.md` — building/publishing images, k8s+Kata setup (any distribution), debugging pod phase/`reason`.
- `references/recipes.md` — copy-paste end-to-end snippets.

Wire protocol: `../../../spec/PROTOCOL.md`. Overview: `../../../README.md`.

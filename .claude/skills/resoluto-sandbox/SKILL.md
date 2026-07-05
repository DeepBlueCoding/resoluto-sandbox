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
| Restrict egress (k8s + local) | `EgressConfig(store_cidr=…, allow=["api.anthropic.com","registry.npmjs.org"])` — backend-neutral (renders to k8s NetworkPolicy OR local iptables). SECURE BY DEFAULT: `EgressConfig()` = store + DNS only, nothing else reachable; `allow=[...]` opens specific dests (least privilege), `public_https=True` is the escape hatch (all :443, trusted). Env: `RESOLUTO_EGRESS_ALLOW`/`_ALLOW_PORT`/`_PUBLIC_HTTPS` (default 0/deny, both backends). On local prefer per-run `run(egress=["api.anthropic.com"])` (SNI-proxy, by domain) |
| Pick store conduit | inject `conduit=` to `SubstrateBackend`; else `store_from_env()` via `RESOLUTO_STORE_KIND` |
| Inject secrets | `run(env_file=".env")` (host-side convenience, NOT security); `run(secrets={"VAR": SecretKeyRef("name","key")})` (k8s-native, zero fetch code); `run(secrets={"VAR": "provider:ref"})` (guest-side `SecretProvider.get(ref)` — ABC ships, no concrete provider yet, see `secrets.py`) |
| CLI | `resoluto-sandbox run [--backend local\|k8s] [--image T] -- <prog> [args]` ; also `doctor`, `image build --provider claude\|langchain\|openai\|all` |
| Build SDK image | `resoluto-sandbox image build --provider claude` (tag = pinned SDK package+version, e.g. `resoluto-sandbox:claude-agent-sdk-0.2.110`; wheel version travels as an OCI label + `RESOLUTO_IMAGE_VERSION` runtime guard) |
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
- **Local backend needs an image.** Default `resoluto-sandbox-base:<installed wheel version>` (`client.default_local_image()`, computed dynamically — never a hardcoded `:dev`/`:latest` tag); override with `Sandbox(backend="local", image="...")`. The image must contain python + the resoluto-sandbox wheel + your program's deps. Needs `/dev/kvm`, the `nerdctl` client, and the dedicated containerd up (`scripts/local-backend-up.sh`).
- **A `docker build`-produced image (incl. `resoluto-sandbox image build`) is NOT visible to the local backend until transferred.** `docker build`/`docker images` use the regular Docker daemon; `Sandbox(backend="local")` launches via `nerdctl` against its OWN dedicated containerd namespace — a separate image store. Symptom: `nerdctl run failed ... pull access denied` (it tried to pull from a registry instead of finding the tag locally). Fix once per build: `docker save <tag> | sudo "$RESOLUTO_LOCAL_NERDCTL" --address /run/resoluto-local/containerd/containerd.sock --namespace resoluto-local load`.
- **`workspace=None` means NOTHING is staged — it is NOT a cwd fallback.** `substrate.py`'s `if workspace:` skips `put_dir` entirely when falsy, so `/workspace` in the guest is empty and any relative argv path 404s. Always pass a real dir. When you do, argv paths are relative to that dir's CONTENTS (tarred with `arcname="."`) — `workspace="examples"` + `argv=["python","claude_agent.py",...]`, never `argv=["python","examples/claude_agent.py",...]`.
- **Base image tag == wheel version; provider overlay tag == pinned SDK package+version instead.** The base (`resoluto-sandbox-base:<ver>`) built for a different `resoluto-sandbox` version won't match — rebuild after upgrading. Provider overlays (`resoluto-sandbox:<sdk-package>-<sdk-version>`, e.g. `claude-agent-sdk-0.2.110`) carry the wheel version as a `resoluto.wheel_version` OCI label instead of in the tag, and `RESOLUTO_IMAGE_VERSION` still asserts the match at container start (fail loud on drift).
- **No wall-clock timeouts.** Liveness = substrate-silence (`dead_after_s=600` between chunks) + heartbeat; a live program runs as long as it stays alive.

## Deep references

- `references/usage.md` — calling `run()`: full args, output collection, `result.json`, CLI.
- `references/agents.md` — bringing your own agent (any language) or Claude Max-subscription auth.
- `references/networking.md` — `EgressConfig` (backend-neutral: k8s NetworkPolicy + local iptables), conduits, store env. local/stdout and S3-against-minio proven; `GcsConduit` experimental/unverified.
- `references/operations.md` — building/publishing images, k8s+Kata setup (any distribution), debugging pod phase/`reason`.
- `references/recipes.md` — copy-paste end-to-end snippets.

Wire protocol: `../../../spec/PROTOCOL.md`. Overview: `../../../README.md`.

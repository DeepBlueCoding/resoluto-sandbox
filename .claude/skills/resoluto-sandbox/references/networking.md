# NETWORK ISOLATION & EGRESS POLICY

Agent reference for controlling what a sandboxed workload can reach over the network.
For the run protocol and backend contracts see `../../../../spec/PROTOCOL.md`; for conduits/staging see `operations.md`.

## TL;DR decision table

| Backend | Isolation | Egress | When |
|---|---|---|---|
| `local` | Kata microVM (hardware-virtualized) via nerdctl | default-deny on the host CNI bridge (egress canary RUNS, fail-closed): store + DNS only until you opt in (`RESOLUTO_EGRESS_ALLOW` / `_PUBLIC_HTTPS`); REJECT IMDS + RFC1918 private | dev and untrusted code at VM-grade isolation |
| `k8s` + `egress=None` | Kata microVM kernel isolation | opt-OUT: UNRESTRICTED (no NetworkPolicy) | trusted, kernel isolation enough |
| `k8s` + `egress=EgressConfig(...)` | Kata microVM + default-deny egress NetworkPolicy | secure by default: store + DNS only; opt in with `allow=[...]` or `public_https=True`; IMDS always blocked | untrusted code |

Footgun: `egress=None` is an explicit opt-OUT of isolation — no NetworkPolicy, so the pod can phone home anywhere (Kata isolates the kernel, not the network). That is DIFFERENT from `EgressConfig()`, which is deny-by-default. For untrusted code pass an `EgressConfig`. (The `local` backend is always enforced host-side on the CNI bridge — immune to in-guest root.)

`EgressConfig` is **one backend-neutral config** (`resoluto.sandbox.egress`). SECURE BY DEFAULT: `EgressConfig()` denies all egress except store + DNS — github/api.anthropic.com/registries do NOT work until you open them. `allow=[hostnames/CIDRs]` + `allow_port` open specific destinations (least privilege, e.g. git-over-SSH `:22`); `public_https=True` is the escape hatch that allows ALL `:443` for trusted code. Same knobs on `k8s` and `local`. On the `local` backend the preferred path is per-run `Sandbox.run(egress=["api.anthropic.com"])` (enforced by DOMAIN via the built-in SNI proxy). See "Modifying the egress allowlist".

## API surface (verbatim)

```python
from resoluto.sandbox import Sandbox                                              # facade
from resoluto.sandbox.backends.substrate import SubstrateBackend, store_env_for_pod  # ONE backend impl
from resoluto.sandbox.conduit.factory import store_from_env                      # conduit from env
from resoluto.sandbox.runtime.k8s import K8sSandboxRuntime                       # k8s runtime
from resoluto.sandbox.egress import EgressConfig                                 # backend-neutral allowlist (also re-exported from runtime.k8s)
```

`Sandbox(backend="local" | "k8s" | <Backend instance>)` then:

```python
RunResult = Sandbox.run(
    argv,                       # Sequence[str], the program to run (plain — never imports resoluto.sandbox)
    *,
    workspace=None,             # str | None — dir staged at /workspace, outputs extracted back here; None = nothing staged
    stdin=None,                 # NOT SUPPORTED — NotImplementedError on both backends
    env=None,                   # dict[str,str] | None — overlays sandbox env
    output_paths=None,          # Sequence[str] | None — globs collected into RunResult.artifacts
    stream=None,                # IO[str] | None — live output sink (default sys.stdout)
    egress=None,                # Sequence[str] | None — domains allowed for THIS run (local); None/[] = deny all but DNS+store
)
```

**Per-run egress (local):** `egress=["api.anthropic.com"]` opens exactly those domains for that
one `run()` and clears them after — no re-provision. The runtime writes the SNI proxy's live
allowlist file (`apply_egress`/`clear_egress`); the proxy reads it per connection and splices only
TLS SNI matches. `None`/`[]` → the secure default (DNS + object store only). On `k8s`, use a
per-runtime `EgressConfig` instead.

`RunResult` (pydantic):
```
exit_code: int
output: str          # MERGED stdout+stderr (in-sandbox runner emits both as log spans)
errors: str          # always "" by design
artifacts: list[str] # collected output_paths
result: dict | None  # parsed result.json if the program wrote one, else None
reason: str          # substrate forensics (evicted/OOMKilled/observed phase); "" for local
ok -> bool           # property: exit_code == 0
```

k8s config is a backend concern — inject a configured `SubstrateBackend`:
```python
import os
SubstrateBackend(
    runtime=K8sSandboxRuntime(
        namespace="resoluto-sandboxes",
        context=os.environ.get("RESOLUTO_SANDBOX_KUBECONTEXT"),
        egress=None,   # or EgressConfig(...)
    ),
    conduit=store_from_env(),
    image="<lane-image>",
    store_env=store_env_for_pod(os.environ),
)
```
- `image` REQUIRED — `ValueError` if missing.
- `conduit` — a `Conduit`. Use `store_from_env()` (needs `RESOLUTO_STORE_KIND`), or inject directly.
- `egress` — `EgressConfig` (canonical home `resoluto.sandbox.egress`, re-exported from
  `resoluto.sandbox.runtime.k8s`). **Backend-neutral**: the SAME config renders to a k8s NetworkPolicy
  OR local iptables. `store_cidr` MUST be CIDR (`x.x.x.x/32`); `allow` entries may be hostnames OR
  CIDRs (resolved when rendered). `None` → opt OUT of isolation (no NetworkPolicy, unrestricted
  egress) — distinct from `EgressConfig()`, which denies by default.

`Sandbox(backend="k8s")` constructs the k8s backend (reads `RESOLUTO_LANE_IMAGE` + `RESOLUTO_STORE_KIND`
from env) — only useful for simple cases; inject `SubstrateBackend` for egress/conduit config.

## Status: this is implemented, not roadmap

The `k8s` backend is FULLY implemented — `SubstrateBackend.run` launches a real Kata pod via `drive_node`, applies the NetworkPolicy, stages workspace in / artifacts out. The ONLY real limit on both backends:
- `stdin is not None` → `NotImplementedError` on BOTH backends

Dependencies must be baked into the image.

Conduits: `local`/`StdoutConduit` (local backend bind-mount) and S3-against-minio (k8s) are PROVEN. `GcsConduit` is experimental/unverified — do not rely on it for isolation guarantees.

## `EgressConfig` — the backend-neutral allowlist (the REAL fields)

`@dataclass(frozen=True)`. Canonical home is now `resoluto.sandbox.egress` (still re-exported from
`resoluto.sandbox.runtime.k8s` for back-compat). It is **backend-neutral**: `egress.py` carries two
pure renderers — `k8s_egress_rules()` (NetworkPolicy) and `local_egress_iptables()` (host iptables) —
so the SAME config drives BOTH `k8s` and `local`. A new provider = one new renderer; callers don't
change. There is NO `llm_cidr`/`git_cidrs` — you open HTTPS via `allow=[...]` (specific) or
`public_https=True` (all `:443`).

```python
EgressConfig(
    allow=(),                 # extra destinations — hostnames OR CIDRs — allowed on allow_port
    allow_port=443,           # port for `allow` (e.g. 22 for git-over-SSH, or a private service port)
    public_https=False,       # DEFAULT deny all :443; True = allow ALL public :443 (escape hatch, trusted)
    store_cidr=None,          # k8s object-store CIDR (REQUIRED for k8s; local ignores it — file mount)
    store_port=443,           # the store's port (minio is often 9000/9100)
)
```

**SECURE BY DEFAULT: github / api.anthropic.com / any HTTPS do NOT work until you open them** —
`EgressConfig()` allows only store + DNS. Open what the workload needs: `allow=["api.anthropic.com",
"registry.npmjs.org", "pypi.org"]` (least privilege, the recommended way for untrusted code), or
`allow_port=22` for a non-443 destination, or `public_https=True` as the escape hatch that allows ALL
`:443` for trusted code. Hostname entries in `allow` resolve to CIDRs when rendered; pinning a
CDN-backed host (anthropic/Cloudflare, rotating IPs) is fragile — on the **local** backend prefer
per-run `Sandbox.run(egress=["api.anthropic.com"])` (enforced by DOMAIN via the built-in SNI proxy, so
it never goes stale for CDN-backed hosts), while `EgressConfig(allow=[...])` (CIDR-based) is the k8s
per-runtime path; for reliable access from otherwise-restricted code, `public_https=True` is the
pragmatic escape hatch.

`store_cidr` is CIDR-ONLY — k8s `ipBlock` rejects FQDNs; `__post_init__` raises `ValueError` on a value
missing `/`. Build it from the env with `EgressConfig.from_store_env()`, which reads
`RESOLUTO_STORE_ENDPOINT` (+ `RESOLUTO_STORE_EGRESS_CIDR`/`RESOLUTO_STORE_EGRESS_PORT` overrides for a
DNAT'd store — NetworkPolicy is evaluated POST-DNAT) AND the simple `RESOLUTO_EGRESS_*` knobs below.

Footgun: import `EgressConfig` from `resoluto.sandbox.egress` (pure stdlib, no platform deps) — NOT via
the top-level `resoluto.sandbox` import, which pulls `kubernetes_asyncio` in eagerly.

## What the generated policy allows (both backends)

`k8s_egress_rules()` renders a default-deny NetworkPolicy (applied at launch ONLY when `egress is not
None`); `local_egress_iptables()` renders the host-side iptables chain. Same config, same allows:

| Destination | Port / Proto | Gate |
|---|---|---|
| `store_cidr` | `store_port` / TCP | k8s only (the local store is a file mount) |
| `0.0.0.0/0` (public HTTPS) | 443 / TCP | ONLY when `public_https=True` (escape hatch; default False) |
| each `allow` entry (resolved to CIDR) | `allow_port` / TCP | when `allow` is non-empty |
| `0.0.0.0/0` (DNS) | 53 / UDP + TCP | always |

IMDS `169.254.169.254` is ALWAYS denied (k8s: `except` on the broad rules; local: an explicit REJECT of
`169.254.0.0/16`). The local renderer ALSO denies RFC1918 unless you `allow` a private CIDR. You cannot
allowlist IMDS.

## Modifying the egress allowlist — ONE config, both backends

Default-deny whitelist. Three simple knobs — no CIDR math or code edits for the common cases:

| Knob | Env | Meaning |
|---|---|---|
| `allow=[...]` | `RESOLUTO_EGRESS_ALLOW` (comma list of host/CIDR) | extra destinations on `allow_port` |
| `allow_port` | `RESOLUTO_EGRESS_ALLOW_PORT` (default 443) | port for `allow` — e.g. `22` for git-over-SSH |
| `public_https` | `RESOLUTO_EGRESS_PUBLIC_HTTPS` (`0`/`1`) | DEFAULT `False`/`0` = store + `allow` + DNS only; `True`/`1` = allow ALL `:443` (escape hatch) |

The env knobs are honored by BOTH backends (k8s via `from_store_env()`; local via
`scripts/local-backend-up.sh`).

**In code (k8s):**
```python
from resoluto.sandbox.egress import EgressConfig
EgressConfig(store_cidr="10.0.0.5/32", store_port=9100,
             allow=["api.anthropic.com", "registry.npmjs.org", "pypi.org"])   # least privilege: LLM + these registries
EgressConfig(store_cidr="10.0.0.5/32", public_https=True)    # escape hatch: all outbound :443 (trusted)
```

**Via env (both backends):**
```bash
export RESOLUTO_EGRESS_ALLOW="api.anthropic.com,registry.npmjs.org"   # comma list of hostnames/CIDRs
export RESOLUTO_EGRESS_ALLOW_PORT=22                        # default 443
export RESOLUTO_EGRESS_PUBLIC_HTTPS=1                       # opt IN to all :443 (default 0 = deny)
```

- **local**: `scripts/local-backend-up.sh` renders the firewall from these env knobs via the SAME
  renderer (`python -m resoluto.sandbox.egress local-iptables --chain <name>`). Set them, re-run the
  script; the Kata canary re-verifies enforcement.
- **k8s**: pass an `EgressConfig` to `K8sSandboxRuntime(egress=...)`, or `EgressConfig.from_store_env()`
  (reads the same env). `egress=None` = opt OUT of isolation (no NetworkPolicy) — distinct from
  `EgressConfig()`, which denies by default.

There is no per-rule *blacklist* primitive (the model is default-deny; IMDS/RFC1918 are hardcoded
denies). "Blacklist a host" = enumerate the hosts you DO want in `allow=[...]` and leave
`public_https=False`. To add a NEW backend,
write a renderer that maps `EgressConfig` to its mechanism — see `src/resoluto.sandbox/egress.py`.

## CNI requirement

NetworkPolicy enforcement needs a NetworkPolicy-capable CNI (Calico, Cilium, Flannel-with-NetworkPolicy). Some distributions (e.g. k3s) ship with one enabled by default; managed clusters (EKS, GKE, AKS) may require it to be enabled separately. On a cluster WITHOUT a NetworkPolicy controller the manifest is created but SILENTLY NOT ENFORCED. The egress canary (below) is the empirical backstop that catches this.

## Egress canary (`egress_canary.py`)

Runs IN-GUEST before the workload, three probes:
1. Non-allowlisted external TCP (default `1.1.1.1:80`) — must be BLOCKED.
2. IMDS TCP `169.254.169.254:80` — must be BLOCKED.
3. Store PUT sentinel (`<prefix>/_canary_ok`) — must SUCCEED.

Any unexpected result → lane aborts with a reason naming every failed probe (surfaced via `SpanEmitter`). This fires when the CNI mis-enforces the policy or the policy was not applied before the pod started.

The egress canary RUNS on the `local` backend too (fail-closed) — local egress is enforced
HOST-SIDE on the lane CNI bridge (default-deny: store + DNS only until you opt in via
`RESOLUTO_EGRESS_ALLOW` / `_PUBLIC_HTTPS`; REJECT IMDS + RFC1918 private), immune to in-guest root.
There is no bypass: isolation is never downgraded and
host `AWS_*` creds are never forwarded — the pod auths via the prefix-scoped, write-only
`RESOLUTO_STORE_WRITE_TOKEN`. The `runtime_class` guard is unconditional (Kata always).

## Related k8s knobs (env)

- `RESOLUTO_SANDBOX_KUBECONTEXT` — PINS the kube-context. If unset and not in-cluster, launch FAILS CLOSED (refuses the ambient current-context). Override only with `RESOLUTO_SANDBOX_ALLOW_AMBIENT_CONTEXT=1`. Prevents launching adversarial pods on the wrong (even prod) cluster.
- `RESOLUTO_SANDBOX_NAMESPACE` (default `resoluto-sandboxes`), `RESOLUTO_LANE_IMAGE_PULL_POLICY` (default `IfNotPresent`).
- `RESOLUTO_STORE_KIND` — required for the k8s conduit when `conduit=None`.

## Copy-paste: locked-down k8s run

```python
import os
from resoluto.sandbox import Sandbox
from resoluto.sandbox.backends.substrate import SubstrateBackend, store_env_for_pod
from resoluto.sandbox.conduit.factory import store_from_env
from resoluto.sandbox.runtime.k8s import K8sSandboxRuntime
from resoluto.sandbox.egress import EgressConfig

egress = EgressConfig(
    store_cidr="192.168.1.197/32",     # your object store (minio / S3-compatible) — k8s only
    store_port=9100,                   # the store's port (default 443)
    allow=["api.anthropic.com", "registry.npmjs.org", "pypi.org"],    # least privilege: open only what the workload needs
    # public_https=True,                     # escape hatch: allow ALL :443 (trusted code)
)                                      # secure by default: nothing else reachable (store + DNS only)
# or: egress = EgressConfig.from_store_env()   # store_cidr:port + RESOLUTO_EGRESS_* knobs, from env

runtime = K8sSandboxRuntime(
    namespace="resoluto-sandboxes",
    context=os.environ.get("RESOLUTO_SANDBOX_KUBECONTEXT"),
    egress=egress,                     # None => opt OUT (no NetworkPolicy, UNRESTRICTED egress)
)

sb = Sandbox(backend=SubstrateBackend(
    runtime=runtime,
    conduit=store_from_env(),
    image="<registry>/resoluto-lane:dev",
    store_env=store_env_for_pod(os.environ),
))

result = sb.run(
    ["python", "agent.py"],
    workspace="/abs/path/to/workspace",
    env={"TASK": "fix the bug"},
    output_paths=["out/**"],
)
assert result.ok, result.reason
print(result.output)            # merged stdout+stderr
print(result.artifacts)         # collected output_paths
```

## Copy-paste: local (Kata microVM via nerdctl, egress enforced host-side)

```python
from resoluto.sandbox import Sandbox
result = Sandbox(backend="local").run(["python", "agent.py"], workspace=".")
```
Kata microVM (hardware-virtualized) via nerdctl + a dedicated containerd on the host. Egress is
enforced HOST-SIDE on the lane CNI bridge (default-deny: store + DNS only until you opt in; REJECT
IMDS + RFC1918 private), immune to in-guest root. It is rendered from the SAME `EgressConfig` as k8s — the
`local_egress_iptables()` renderer — so the `RESOLUTO_EGRESS_ALLOW` / `_ALLOW_PORT` / `_PUBLIC_HTTPS`
knobs apply here too (set them, re-run `scripts/local-backend-up.sh`). The egress canary RUNS
(fail-closed). Suitable for untrusted code at VM-grade isolation. Needs `/dev/kvm` + nerdctl + the
dedicated containerd + an image (default `resoluto-sandbox-base:<installed wheel version>` (`default_local_image()`), never a floating tag).

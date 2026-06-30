# NETWORK ISOLATION & EGRESS POLICY

Agent reference for controlling what a sandboxed workload can reach over the network.
For the run protocol and backend contracts see `../../../../spec/PROTOCOL.md`; for conduits/staging see `operations.md`.

## TL;DR decision table

| Backend | Isolation | Egress | When |
|---|---|---|---|
| `local` | Kata microVM (hardware-virtualized) via nerdctl | default-deny on the host CNI bridge (egress canary RUNS, fail-closed): allow DNS + HTTPS-443-public; REJECT IMDS + RFC1918 private | dev and untrusted code at VM-grade isolation |
| `k8s` + `egress=None` | Kata microVM kernel isolation | UNRESTRICTED (no NetworkPolicy) | semi-trusted, kernel isolation enough |
| `k8s` + `egress=EgressConfig(...)` | Kata microVM + default-deny egress NetworkPolicy | store `:store_port` + ALL public `:443` (any HTTPS) + DNS `:53`; IMDS always blocked | untrusted code |

Footgun: `k8s` does NOT lock down egress unless you pass `EgressConfig`. Kata isolates the kernel, not the network. Untrusted code with `egress=None` can phone home anywhere. (The `local` backend is different: egress is always enforced host-side on the CNI bridge — immune to in-guest root.)

`EgressConfig` is **one backend-neutral config** (`resoluto_sandbox.egress`): `public_https=True` (default) allows all `:443` so github/api.anthropic.com/any HTTPS already work; `allow=[hosts/CIDRs]` + `allow_port` add a non-443 destination (e.g. git-over-SSH `:22`); `public_https=False` locks down to store + `allow` + DNS. Same knobs on `k8s` and `local`. See "Modifying the egress allowlist".

## API surface (verbatim)

```python
from resoluto_sandbox import Sandbox                                              # facade
from resoluto_sandbox.backends.substrate import SubstrateBackend, store_env_for_pod  # ONE backend impl
from resoluto_sandbox.conduit.factory import store_from_env                      # conduit from env
from resoluto_sandbox.runtime.k8s import K8sSandboxRuntime                       # k8s runtime
from resoluto_sandbox.egress import EgressConfig                                 # backend-neutral allowlist (also re-exported from runtime.k8s)
```

`Sandbox(backend="local" | "k8s" | <Backend instance>)` then:

```python
RunResult = Sandbox.run(
    argv,                       # Sequence[str], the program to run (plain — never imports resoluto_sandbox)
    *,
    workspace=None,             # str | None — program cwd; outputs extracted here in place
    stdin=None,                 # NOT SUPPORTED — NotImplementedError on both backends
    env=None,                   # dict[str,str] | None — overlays sandbox env
    output_paths=None,          # Sequence[str] | None — globs collected into RunResult.artifacts
    stream=None,                # IO[str] | None — live output sink (default sys.stdout)
)
```

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
- `egress` — `EgressConfig` (canonical home `resoluto_sandbox.egress`, re-exported from
  `resoluto_sandbox.runtime.k8s`). **Backend-neutral**: the SAME config renders to a k8s NetworkPolicy
  OR local iptables. `store_cidr` MUST be CIDR (`x.x.x.x/32`); `allow` entries may be hostnames OR
  CIDRs (resolved when rendered). `None` → unrestricted egress.

`Sandbox(backend="k8s")` constructs the k8s preset (reads `RESOLUTO_LANE_IMAGE` + `RESOLUTO_STORE_KIND`
from env) — only useful for simple cases; inject `SubstrateBackend` for egress/conduit config.

## Status: this is implemented, not roadmap

The `k8s` backend is FULLY implemented — `SubstrateBackend.run` launches a real Kata pod via `drive_node`, applies the NetworkPolicy, stages workspace in / artifacts out. The ONLY real limit on both backends:
- `stdin is not None` → `NotImplementedError` on BOTH backends

Dependencies must be baked into the image.

Conduits: `local`/`StdoutConduit` (local backend bind-mount) and S3-against-minio (k8s) are PROVEN. `GcsConduit` is experimental/unverified — do not rely on it for isolation guarantees.

## `EgressConfig` — the backend-neutral allowlist (the REAL fields)

`@dataclass(frozen=True)`. Canonical home is now `resoluto_sandbox.egress` (still re-exported from
`resoluto_sandbox.runtime.k8s` for back-compat). It is **backend-neutral**: `egress.py` carries two
pure renderers — `k8s_egress_rules()` (NetworkPolicy) and `local_egress_iptables()` (host iptables) —
so the SAME config drives BOTH `k8s` and `local`. A new provider = one new renderer; callers don't
change. There is NO `llm_cidr`/`git_cidrs` — any HTTPS is already allowed via `public_https`.

```python
EgressConfig(
    allow=(),                 # extra destinations — hostnames OR CIDRs — allowed on allow_port
    allow_port=443,           # port for `allow` (e.g. 22 for git-over-SSH, or a private service port)
    public_https=True,        # True = allow ALL public :443; False = ONLY store + allow + DNS (lock down)
    store_cidr=None,          # k8s object-store CIDR (REQUIRED for k8s; local ignores it — file mount)
    store_port=443,           # the store's port (minio is often 9000/9100)
)
```

**github / api.anthropic.com / any HTTPS ALREADY work** with NO config — they're public `:443`. You
only configure egress to (a) add a NON-443 destination (e.g. git-over-SSH `:22` via `allow=[...],
allow_port=22`) or (b) LOCK DOWN with `public_https=False`. Hostnames in `allow` resolve to CIDRs when
rendered; pinning a CDN-backed host (anthropic/Cloudflare, rotating IPs) is fragile — keep
`public_https=True` for those.

`store_cidr` is CIDR-ONLY — k8s `ipBlock` rejects FQDNs; `__post_init__` raises `ValueError` on a value
missing `/`. Build it from the env with `EgressConfig.from_store_env()`, which reads
`RESOLUTO_STORE_ENDPOINT` (+ `RESOLUTO_STORE_EGRESS_CIDR`/`RESOLUTO_STORE_EGRESS_PORT` overrides for a
DNAT'd store — NetworkPolicy is evaluated POST-DNAT) AND the simple `RESOLUTO_EGRESS_*` knobs below.

Footgun: import `EgressConfig` from `resoluto_sandbox.egress` (pure stdlib, no platform deps) — NOT via
the top-level `resoluto_sandbox` import, which pulls `kubernetes_asyncio` in eagerly.

## What the generated policy allows (both backends)

`k8s_egress_rules()` renders a default-deny NetworkPolicy (applied at launch ONLY when `egress is not
None`); `local_egress_iptables()` renders the host-side iptables chain. Same config, same allows:

| Destination | Port / Proto | Gate |
|---|---|---|
| `store_cidr` | `store_port` / TCP | k8s only (the local store is a file mount) |
| `0.0.0.0/0` (public HTTPS) | 443 / TCP | only when `public_https=True` (the default) |
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
| `public_https` | `RESOLUTO_EGRESS_PUBLIC_HTTPS` (`0`/`1`) | `False`/`0` = lock down to store + `allow` + DNS only |

The env knobs are honored by BOTH backends (k8s via `from_store_env()`; local via
`scripts/local-backend-up.sh`).

**In code (k8s):**
```python
from resoluto_sandbox.egress import EgressConfig
EgressConfig(store_cidr="10.0.0.5/32", store_port=9100,
             allow=["github.com"], allow_port=22)            # + git-over-SSH, keep all HTTPS
EgressConfig(store_cidr="10.0.0.5/32",
             allow=["198.51.100.7/32"], public_https=False)  # LOCK DOWN: only store + that host + DNS
```

**Via env (both backends):**
```bash
export RESOLUTO_EGRESS_ALLOW="github.com,198.51.100.0/24"   # comma list of hosts/CIDRs
export RESOLUTO_EGRESS_ALLOW_PORT=22                        # default 443
export RESOLUTO_EGRESS_PUBLIC_HTTPS=0                       # 0 = lock down to the allow-list only
```

- **local**: `scripts/local-backend-up.sh` renders the firewall from these env knobs via the SAME
  renderer (`python -m resoluto_sandbox.egress local-iptables --chain <name>`). Set them, re-run the
  script; the Kata canary re-verifies enforcement.
- **k8s**: pass an `EgressConfig` to `K8sSandboxRuntime(egress=...)`, or `EgressConfig.from_store_env()`
  (reads the same env). `egress=None` = no restriction.

There is no per-rule *blacklist* primitive (the model is default-deny; IMDS/RFC1918 are hardcoded
denies). "Blacklist a host" = `public_https=False` + `allow=[everything-else]`. To add a NEW backend,
write a renderer that maps `EgressConfig` to its mechanism — see `src/resoluto_sandbox/egress.py`.

## CNI requirement

NetworkPolicy enforcement needs a NetworkPolicy-capable CNI (Calico, Cilium, Flannel-with-NetworkPolicy). Some distributions (e.g. k3s) ship with one enabled by default; managed clusters (EKS, GKE, AKS) may require it to be enabled separately. On a cluster WITHOUT a NetworkPolicy controller the manifest is created but SILENTLY NOT ENFORCED. The egress canary (below) is the empirical backstop that catches this.

## Egress canary (`egress_canary.py`)

Runs IN-GUEST before the workload, three probes:
1. Non-allowlisted external TCP (default `1.1.1.1:80`) — must be BLOCKED.
2. IMDS TCP `169.254.169.254:80` — must be BLOCKED.
3. Store PUT sentinel (`<prefix>/_canary_ok`) — must SUCCEED.

Any unexpected result → lane aborts with a reason naming every failed probe (surfaced via `SpanEmitter`). This fires when the CNI mis-enforces the policy or the policy was not applied before the pod started.

The egress canary RUNS on the `local` backend too (fail-closed) — local egress is enforced
HOST-SIDE on the lane CNI bridge (default-deny; allow DNS + HTTPS-443-public; REJECT IMDS +
RFC1918 private), immune to in-guest root. There is no bypass: isolation is never downgraded and
host `AWS_*` creds are never forwarded — the pod auths via the prefix-scoped, write-only
`RESOLUTO_STORE_WRITE_TOKEN`. The `runtime_class` guard is unconditional (Kata always).

## Related k8s knobs (env)

- `RESOLUTO_SANDBOX_KUBECONTEXT` — PINS the kube-context. If unset and not in-cluster, launch FAILS CLOSED (refuses the ambient current-context). Override only with `RESOLUTO_SANDBOX_ALLOW_AMBIENT_CONTEXT=1`. Prevents launching adversarial pods on the wrong (even prod) cluster.
- `RESOLUTO_SANDBOX_NAMESPACE` (default `resoluto-sandboxes`), `RESOLUTO_LANE_IMAGE_PULL_POLICY` (default `IfNotPresent`).
- `RESOLUTO_STORE_KIND` — required for the k8s conduit when `conduit=None`.

## Copy-paste: locked-down k8s run

```python
import os
from resoluto_sandbox import Sandbox
from resoluto_sandbox.backends.substrate import SubstrateBackend, store_env_for_pod
from resoluto_sandbox.conduit.factory import store_from_env
from resoluto_sandbox.runtime.k8s import K8sSandboxRuntime
from resoluto_sandbox.egress import EgressConfig

egress = EgressConfig(
    store_cidr="192.168.1.197/32",     # your object store (minio / S3-compatible) — k8s only
    store_port=9100,                   # the store's port (default 443)
    # allow=["github.com"], allow_port=22,   # OPTIONAL: add git-over-SSH (or any non-443 dest)
    # public_https=False,                    # OPTIONAL: lock down to store + allow + DNS only
)                                      # github/api.anthropic.com/any HTTPS already work by default
# or: egress = EgressConfig.from_store_env()   # store_cidr:port + RESOLUTO_EGRESS_* knobs, from env

runtime = K8sSandboxRuntime(
    namespace="resoluto-sandboxes",
    context=os.environ.get("RESOLUTO_SANDBOX_KUBECONTEXT"),
    egress=egress,                     # omit / None => UNRESTRICTED egress
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
from resoluto_sandbox import Sandbox
result = Sandbox(backend="local").run(["python", "agent.py"], workspace=".")
```
Kata microVM (hardware-virtualized) via nerdctl + a dedicated containerd on the host. Egress is
enforced HOST-SIDE on the lane CNI bridge (default-deny; allow DNS + HTTPS-443-public; REJECT IMDS +
RFC1918 private), immune to in-guest root. It is rendered from the SAME `EgressConfig` as k8s — the
`local_egress_iptables()` renderer — so the `RESOLUTO_EGRESS_ALLOW` / `_ALLOW_PORT` / `_PUBLIC_HTTPS`
knobs apply here too (set them, re-run `scripts/local-backend-up.sh`). The egress canary RUNS
(fail-closed). Suitable for untrusted code at VM-grade isolation. Needs `/dev/kvm` + nerdctl + the
dedicated containerd + an image (default `resoluto-sandbox-base:dev`).

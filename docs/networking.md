# Networking

## Local backend — Kata isolation + host-side egress policy

The local backend runs the program in a Kata microVM launched via `nerdctl` against a standalone
containerd on this host — full Kata isolation, on a single host with no cluster. Egress is enforced
host-side on its CNI bridge and is **default-deny (secure by default)**: a fresh lane reaches only
DNS and its store. You opt in to what the workload needs at PROVISION time via the env knobs
(`RESOLUTO_EGRESS_ALLOW` for specific destinations, `RESOLUTO_EGRESS_PUBLIC_HTTPS=1` for all
outbound :443). IMDS `169.254.169.254` and RFC1918 private ranges are always rejected, so the
policy is immune to in-guest root. The egress canary always runs fail-closed before any workload,
the Kata runtime-class guard is unconditional, and host AWS creds are never forwarded (a scoped
store token only).

Provision the local backend with `scripts/local-backend-up.sh` (ends with a green Kata-microVM
canary). To open egress, set the env before running it, e.g.:

```bash
RESOLUTO_EGRESS_ALLOW=api.anthropic.com,registry.npmjs.org scripts/local-backend-up.sh   # least privilege
RESOLUTO_EGRESS_PUBLIC_HTTPS=1 scripts/local-backend-up.sh             # escape hatch: all :443
```

Use `backend="local"` for single-host development. For cluster-scale placement, use `backend="k8s"`.

## k8s backend — Kata isolation + egress NetworkPolicy

### `egress=None` — the opt-OUT (no NetworkPolicy)

`egress=None` on `K8sSandboxRuntime` is an explicit opt-OUT of network isolation: NO NetworkPolicy
is created, so the lane pod has **unrestricted egress**. The pod still runs inside a Kata microVM
(separate OS kernel, no host process namespace), but nothing restricts which hosts the workload can
reach. This is DIFFERENT from `EgressConfig()`, which is deny-by-default (see below). Use
`egress=None` only for trusted workloads where kernel isolation alone is acceptable; for untrusted
code always pass an `EgressConfig`.

### `EgressConfig` — deny by default (secure)

`EgressConfig()` is **secure by default**: it DENIES all egress except DNS and the object store —
a fresh lane cannot reach the internet, the LLM, or registries. You opt IN to exactly what the
workload needs. Pass it to `K8sSandboxRuntime` and it applies a default-deny egress NetworkPolicy
to the lane pod (created before the pod) so egress is enforced from the first packet:

```python
import os
from resoluto.sandbox import Sandbox
from resoluto.sandbox.backends.substrate import SubstrateBackend, store_env_for_pod
from resoluto.sandbox.conduit.factory import store_from_env
from resoluto.sandbox.runtime.k8s import K8sSandboxRuntime, EgressConfig

# The ONE shared builder — resolves RESOLUTO_STORE_ENDPOINT to the store CIDR:port (honoring the
# RESOLUTO_STORE_EGRESS_CIDR/PORT override for a DNAT'd store; NetworkPolicy is evaluated POST-DNAT).
egress = EgressConfig.from_store_env()
# ...or construct it explicitly (deny-by-default; open what you need):
egress = EgressConfig(store_cidr="192.168.1.197/32", store_port=9000,
                      allow=["api.anthropic.com", "registry.npmjs.org", "pypi.org"])  # least privilege; store port default 443
runtime = K8sSandboxRuntime(
    namespace="resoluto-sandboxes",
    context=os.environ.get("RESOLUTO_SANDBOX_KUBECONTEXT"),
    egress=egress,
)
sb = Sandbox(backend=SubstrateBackend(
    runtime=runtime,
    conduit=store_from_env(),
    image="<registry>/resoluto-lane:0.1.0",
    store_env=store_env_for_pod(os.environ),
))
```

`EgressConfig` is imported from `resoluto.sandbox.runtime.k8s`. Do not add it to the top-level
`resoluto.sandbox` import — that would pull in `kubernetes_asyncio` eagerly.

### What the NetworkPolicy allows

The generated policy is default-deny egress. It ALWAYS allows the store and DNS; the other rows are
opt-in:

| Destination | Port | Protocol | When |
|---|---|---|---|
| `store_cidr` | `store_port` | TCP | always |
| `0.0.0.0/0` (DNS) | 53 | UDP + TCP | always |
| each `allow` entry (hostname/CIDR) | `allow_port` (443 default) | TCP | when `allow=[...]` is set |
| `0.0.0.0/0` (public HTTPS) | 443 | TCP | ONLY when `public_https=True` |

The broad `0.0.0.0/0` rules include `except: ["169.254.169.254/32"]` so the cloud metadata endpoint
(IMDS) is unreachable; the `store_cidr` rule is a specific host so it carries no `except` (k8s
requires `except ⊂ cidr`). Prefer `allow=[...]` (least privilege) for untrusted code; on the
**local** backend the preferred way to open egress is per-run `Sandbox.run(egress=["api.anthropic.com"])`
(enforced by DOMAIN via the built-in SNI proxy, so it never goes stale for CDN-backed hosts); use
`public_https=True` only as a deliberate escape hatch for trusted workloads.

### CIDR-only: no FQDNs for the store

Kubernetes `NetworkPolicy` `ipBlock` does not accept hostnames, so `store_cidr` must be a CIDR
(`EgressConfig.__post_init__` rejects anything without `/`). Hostname entries in `allow`
resolve to CIDRs at render time. Build the config from the env with `EgressConfig.from_store_env()`,
which resolves `RESOLUTO_STORE_ENDPOINT` (or honors the `RESOLUTO_STORE_EGRESS_CIDR/PORT` override
for a DNAT'd store — NetworkPolicy is evaluated post-DNAT).

```python
EgressConfig(store_cidr="api.anthropic.com")  # raises ValueError — no '/'
```

### CNI requirement

NetworkPolicy enforcement requires a NetworkPolicy-capable CNI (e.g. Calico, Cilium, Flannel
with NetworkPolicy support). Some distributions (e.g. k3s) ship with Flannel + the NetworkPolicy
controller enabled by default; managed clusters (EKS, GKE, AKS) may need it enabled separately.
On a cluster without a NetworkPolicy controller the manifested policy is silently **not enforced** —
the in-guest egress canary (see below) is the empirical backstop.

### Egress canary

Before the workload runs, the pod self-verifies isolation with three probes:

1. **Non-allowlisted external TCP** — must be blocked (NetworkPolicy enforced).
2. **IMDS TCP `169.254.169.254:80`** — must be blocked (no cloud-metadata leakage).
3. **Store PUT sentinel** — must succeed (the only permitted egress channel).

If any probe returns an unexpected result the lane aborts with a reason string naming every
failed probe. This catch fires when the CNI enforces the policy incorrectly, or when the policy
was not applied before the pod started.

The canary always runs and is fail-closed. Host AWS credentials are never forwarded to the pod; a
scoped store token is the only credential it receives. The `evaluate_verdict` pure function (the
pass/fail logic) is unit-tested in `tests/test_egress_canary.py`; the in-guest execution is the live
check.

## Modifying the egress allowlist — ONE config, both backends

Egress is configured by a single backend-neutral value object, `resoluto.sandbox.egress.EgressConfig`,
which each backend renders to its own mechanism (k8s → NetworkPolicy, local → host `iptables`). It is
**deny-by-default**: `EgressConfig()` allows ONLY the store + DNS; IMDS (and on local, RFC1918) are
always denied. So github, api.anthropic.com, package mirrors, etc. do NOT work until you open them.
You opt in with `allow=[...]` (least privilege) or, for trusted code, `public_https=True` (all :443).

Three simple knobs (no CIDRs or code edits needed):

| Knob | Meaning |
|---|---|
| `allow=[...]` | extra destinations — **hostnames** OR **CIDRs** — allowed on `allow_port`. Hostnames resolve to CIDRs when rendered. The least-privilege way to open egress. |
| `allow_port` | port for `allow` (default 443; e.g. **22** for git-over-SSH, or a private service port) |
| `public_https` | `False` (default) = deny all outbound except store + `allow` + DNS; set **`True`** to allow ALL `:443` (escape hatch for trusted code) |

So an agent lane that needs the LLM + npm/pypi reads
`EgressConfig(allow=["api.anthropic.com", "registry.npmjs.org", "pypi.org"])` (or
`RESOLUTO_EGRESS_ALLOW="api.anthropic.com,registry.npmjs.org"`). Hostname entries resolve to
**current** IPs when the policy is rendered; these APIs are CDN-backed (rotating IPs), so a pinned
allowlist is best-effort and needs periodic re-resolve. On the **local** backend prefer per-run
`Sandbox.run(egress=["api.anthropic.com"])` (enforced by DOMAIN via the built-in SNI proxy, so it
never goes stale for CDN-backed hosts); `EgressConfig(allow=[...])` (CIDR-based) is the k8s
per-runtime path. When you need reliable access from otherwise-restricted code, `public_https=True`
(all :443, trusted code) is the pragmatic escape hatch.

### Allow by DOMAIN, PER STEP — `run(egress=[...])` (the primary knob)

`allow=[...]` (below) is CIDR-based (resolves hostnames to IPs at render time), so it goes stale for
CDN-backed APIs (rotating IPs) and can never match a URL path — TLS encrypts everything but the
destination IP:port and the **SNI** hostname. For a domain allowlist that scales AND is set per step,
pass `egress=[domains]` to each `run()`:

```python
Sandbox(backend="local").run(argv, egress=["api.anthropic.com"])    # this step reaches ONLY Anthropic
Sandbox(backend="local").run(argv, egress=["registry.npmjs.org"])   # this step reaches ONLY the npm registry
Sandbox(backend="local").run(argv)                                  # egress=None → deny all (secure default)
```

Each `run()` sets that step's allowed domains on the fly and clears them after — **no re-provision**.
Under the hood the built-in **SNI proxy** (`resoluto.sandbox.egress_proxy`) reads the step's allowlist
LIVE (from a file each run rewrites) and splices the (still-encrypted) stream to the original
destination ONLY if the TLS SNI matches — exact (`api.anthropic.com`) or `*.wildcard` (`*.openai.com`).
No IP pinning, no CA/MITM, works under any CNI; it refuses internal/IMDS destinations even on an SNI
match (no SSRF). The mechanism is `KataNerdctlSandboxRuntime.apply_egress()` writing the proxy's live
allowlist file; `SubstrateBackend.run` applies it before the lane and clears it after.

One-time setup runs the proxy + the static `:443` redirect: `scripts/local-backend-up.sh` (it also
seeds the file from `RESOLUTO_EGRESS_DOMAINS` if set — a default, overridden per run).

Verified end-to-end, back-to-back with NO re-provision: `run(egress=["registry.npmjs.org"])` → a lane's
`pnpm add is-odd` installs from the registry; `run(egress=["api.anthropic.com"])` → the same install is
blocked (ECONNRESET) while a real Claude agent answers. A URL *path* still can't be enforced at this
layer (that needs a MITM proxy). DNS and the CIDR FORWARD chain handle everything else; `allow=[...]`
(below) remains for non-443 ports / explicit CIDRs. NOTE: `egress=` is applied by the `local` backend
today; on `k8s` use `EgressConfig` (per-runtime).

**In code (k8s):**
```python
from resoluto.sandbox.egress import EgressConfig
EgressConfig(store_cidr="10.0.0.5/32", store_port=9100,
             allow=["github.com"], allow_port=22)        # least privilege: + git-over-SSH
EgressConfig(store_cidr="10.0.0.5/32", public_https=True)  # escape hatch: all outbound :443
```

**Via env (works for BOTH backends — k8s reads these in `from_store_env()`, the local provisioner
reads them too):**
```bash
export RESOLUTO_EGRESS_ALLOW="github.com,198.51.100.0/24"   # comma list of hostnames/CIDRs
export RESOLUTO_EGRESS_ALLOW_PORT=22                        # default 443
export RESOLUTO_EGRESS_PUBLIC_HTTPS=1                       # opt IN to all :443 (default 0 = deny)
```

- **local**: `scripts/local-backend-up.sh` renders the firewall from these env knobs via the SAME
  renderer (`python -m resoluto.sandbox.egress local-iptables`). Set them and re-run the script; the
  Kata canary re-verifies enforcement.
- **k8s**: pass an `EgressConfig` to `K8sSandboxRuntime(egress=...)`, or `EgressConfig.from_store_env()`
  (which reads the same env). `egress=None` = opt OUT of isolation entirely (no NetworkPolicy,
  unrestricted egress) — distinct from `EgressConfig()`, which denies by default.

There is no per-rule *blacklist* primitive (the model is default-deny; IMDS/RFC1918 are hardcoded
denies). "Blacklist a host" = enumerate the hosts you DO want in `allow=[...]` and leave
`public_https=False`. To add a NEW backend, write a renderer that maps `EgressConfig` to its
mechanism (see `src/resoluto/sandbox/egress.py`).

## What you can manage

| Knob | Local backend | k8s backend |
|---|---|---|
| Egress allowlist | host-side iptables on the CNI bridge (default-deny; DNS + store; opt-in `allow`/`public_https`; REJECT IMDS + RFC1918) | `EgressConfig.from_store_env()` → default-deny NetworkPolicy (store + DNS always; opt-in `allow`/public-443; IMDS denied), enforced by the cluster's NetworkPolicy controller (k3s kube-router) |
| IMDS block | always on (host-side REJECT of `169.254.169.254`) | always on when `EgressConfig` is passed |
| Egress canary | on by default, fail-closed | on by default, fail-closed |
| Runtime class | Kata (pinned, unconditional) | Kata (pinned, unconditional) |
| Kubecontext | not applicable | `RESOLUTO_SANDBOX_KUBECONTEXT` (fails closed if unset) |

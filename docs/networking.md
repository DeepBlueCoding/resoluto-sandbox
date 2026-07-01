# Networking

## Local backend — Kata isolation + host-side egress policy

The local backend runs the program in a Kata microVM launched via `nerdctl` against a standalone
containerd on this host — full Kata isolation, on a single host with no cluster. Egress is enforced
host-side on its CNI bridge (default-deny; allow DNS + HTTPS-443 to public; reject IMDS
`169.254.169.254` + RFC1918 private ranges), so it is immune to in-guest root. The egress canary
always runs fail-closed before any workload, the Kata runtime-class guard is unconditional, and host
AWS creds are never forwarded (a scoped store token only).

Provision the local backend with `scripts/local-backend-up.sh` (ends with a green Kata-microVM
canary).

Use `backend="local"` for single-host development. For cluster-scale placement, use `backend="k8s"`.

## k8s backend — Kata isolation + optional egress NetworkPolicy

### Default: unrestricted egress

By default (`egress=None`) a lane pod launched via `SubstrateBackend` + `K8sSandboxRuntime` has
unrestricted egress. The pod runs inside a Kata microVM (separate OS kernel, no host process
namespace), but no NetworkPolicy restricts which hosts the workload can reach. If you run untrusted
code, pass an `EgressConfig`.

### Locking down egress with EgressConfig

`EgressConfig` declares the object store the workload needs; the policy then also permits public
HTTPS (TCP/443 to anywhere — LLM + git, IMDS excepted) and DNS, and denies everything else. Pass it
to `K8sSandboxRuntime` and it applies a default-deny egress NetworkPolicy to the lane pod (created
before the pod) so egress is enforced from the first packet:

```python
import os
from resoluto_sandbox import Sandbox
from resoluto_sandbox.backends.substrate import SubstrateBackend, store_env_for_pod
from resoluto_sandbox.conduit.factory import store_from_env
from resoluto_sandbox.runtime.k8s import K8sSandboxRuntime, EgressConfig

# The ONE shared builder — resolves RESOLUTO_STORE_ENDPOINT to the store CIDR:port (honoring the
# RESOLUTO_STORE_EGRESS_CIDR/PORT override for a DNAT'd store; NetworkPolicy is evaluated POST-DNAT).
egress = EgressConfig.from_store_env()
# ...or construct it explicitly:
egress = EgressConfig(store_cidr="192.168.1.197/32", store_port=9000)  # store; port default 443
runtime = K8sSandboxRuntime(
    namespace="resoluto-sandboxes",
    context=os.environ.get("RESOLUTO_SANDBOX_KUBECONTEXT"),
    egress=egress,
)
sb = Sandbox(backend=SubstrateBackend(
    runtime=runtime,
    conduit=store_from_env(),
    image="<registry>/resoluto-lane:dev",
    store_env=store_env_for_pod(os.environ),
))
```

`EgressConfig` is imported from `resoluto_sandbox.runtime.k8s`. Do not add it to the top-level
`resoluto_sandbox` import — that would pull in `kubernetes_asyncio` eagerly.

### What the NetworkPolicy allows

The generated policy is default-deny egress with these explicit allow rules:

| Destination | Port | Protocol |
|---|---|---|
| `store_cidr` | `store_port` | TCP |
| `0.0.0.0/0` (public HTTPS — LLM + git) | 443 | TCP |
| `0.0.0.0/0` (DNS) | 53 | UDP + TCP |

The broad `0.0.0.0/0` rules include `except: ["169.254.169.254/32"]` so the cloud metadata endpoint
(IMDS) is unreachable; the `store_cidr` rule is a specific host so it carries no `except` (k8s
requires `except ⊂ cidr`). Allowing public 443 rather than pinning the LLM/git provider to a /32
avoids CDN-IP fragility while still blocking IMDS and non-443.

### CIDR-only: no FQDNs for the store

Kubernetes `NetworkPolicy` `ipBlock` does not accept hostnames, so `store_cidr` must be a CIDR
(`EgressConfig.__post_init__` rejects anything without `/`). LLM/git need no resolution — they're
covered by the public-443 rule. Build the config from the env with `EgressConfig.from_store_env()`,
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

## Modifying the egress allowlist (whitelist) — ONE config, both backends

Egress is configured by a single backend-neutral value object, `resoluto_sandbox.egress.EgressConfig`,
which each backend renders to its own mechanism (k8s → NetworkPolicy, local → host `iptables`). It is
**default-deny whitelist**: all public HTTPS (`:443`), DNS, and your extras are allowed; IMDS (and on
local, RFC1918) are always denied. So **github, api.anthropic.com, package mirrors, etc. already work**
— they're HTTPS on 443. You only configure egress to add a non-443 destination or to lock down.

Three simple knobs (no CIDRs or code edits needed):

| Knob | Meaning |
|---|---|
| `allow=[...]` | extra destinations — **preset names**, **hostnames**, OR **CIDRs** — allowed on `allow_port`. Names/hostnames resolve to CIDRs when rendered. |
| `allow_port` | port for `allow` (default 443; e.g. **22** for git-over-SSH, or a private service port) |
| `public_https` | `True` (default) allows all `:443`; set **`False`** to allow ONLY your store + `allow` + DNS |

**Presets** (for the lock-down case — expand to the provider's API hosts): LLM APIs `anthropic openai
openrouter gemini groq mistral cohere deepseek together perplexity fireworks xai` (bundle `llms`); package
registries `npm pypi uv composer cargo go rubygems github huggingface` (bundle `registries`). So a
locked-down agent lane reads `EgressConfig(allow=["anthropic", "npm", "pypi"], public_https=False)` (or
`RESOLUTO_EGRESS_ALLOW="anthropic,npm,pypi"`). Preset/hostname entries resolve to **current** IPs when
the policy is rendered; these APIs are CDN-backed (rotating IPs), so for reliable access keep
`public_https=True` (the default) — it already allows all of them — and use presets only when you
deliberately lock down and accept periodic re-resolve.

**In code (k8s):**
```python
from resoluto_sandbox.egress import EgressConfig
EgressConfig(store_cidr="10.0.0.5/32", store_port=9100,
             allow=["github.com"], allow_port=22)        # + git-over-SSH, keep all HTTPS
EgressConfig(store_cidr="10.0.0.5/32",
             allow=["198.51.100.7/32"], public_https=False)  # LOCK DOWN: only store + that host + DNS
```

**Via env (works for BOTH backends — k8s reads these in `from_store_env()`, the local provisioner
reads them too):**
```bash
export RESOLUTO_EGRESS_ALLOW="github.com,198.51.100.0/24"   # comma list of hosts/CIDRs
export RESOLUTO_EGRESS_ALLOW_PORT=22                        # default 443
export RESOLUTO_EGRESS_PUBLIC_HTTPS=0                       # 0 = lock down to allow-list only
```

- **local**: `scripts/local-backend-up.sh` renders the firewall from these env knobs via the SAME
  renderer (`python -m resoluto_sandbox.egress local-iptables`). Set them and re-run the script; the
  Kata canary re-verifies enforcement.
- **k8s**: pass an `EgressConfig` to `K8sSandboxRuntime(egress=...)`, or `EgressConfig.from_store_env()`
  (which reads the same env). `egress=None` = no restriction.

There is no per-rule *blacklist* primitive (the model is default-deny; IMDS/RFC1918 are hardcoded
denies). "Blacklist a host" = run with `public_https=False` and `allow=[everything-except-it]`. To add a
NEW backend, write a renderer that maps `EgressConfig` to its mechanism (see `src/resoluto_sandbox/egress.py`).

## What you can manage

| Knob | Local backend | k8s backend |
|---|---|---|
| Egress allowlist | host-side iptables on the CNI bridge (default-deny; DNS + 443; REJECT IMDS + RFC1918) | `EgressConfig.from_store_env()` → default-deny NetworkPolicy (store + public-443 + DNS; IMDS denied), enforced by the cluster's NetworkPolicy controller (k3s kube-router) |
| IMDS block | always on (host-side REJECT of `169.254.169.254`) | always on when `EgressConfig` is passed |
| Egress canary | on by default, fail-closed | on by default, fail-closed |
| Runtime class | Kata (pinned, unconditional) | Kata (pinned, unconditional) |
| Kubecontext | not applicable | `RESOLUTO_SANDBOX_KUBECONTEXT` (fails closed if unset) |

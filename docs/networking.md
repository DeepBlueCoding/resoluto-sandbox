# Networking

---

## Local backend — OS-level isolation, no egress policy

The local backend runs the program in a Docker container on this host. Docker provides OS-level
isolation (separate PID/mount/network namespaces, cgroups), but there is **no egress
NetworkPolicy** restricting which hosts the workload can reach. The egress canary is skipped
(`RESOLUTO_TRUSTED_LOCAL=1` is set by the local preset).

Use `backend="local"` for trusted code or development. For locked-down egress or hardware
isolation, use `backend="k8s"`.

---

## k8s backend — Kata kernel isolation + optional egress NetworkPolicy

### Default: unrestricted egress

By default (`egress=None`) a lane pod launched via `SubstrateBackend` + `K8sSandboxRuntime` has
**unrestricted egress**. The pod runs inside a Kata microVM which provides strong kernel isolation
(separate OS kernel, no host process namespace), but there is **no NetworkPolicy** restricting
which hosts the workload can reach. If you run untrusted code, pass an `EgressConfig`.

### Locking down egress with EgressConfig

`EgressConfig` holds the CIDRs your workload legitimately needs to reach. Pass it to
`K8sSandboxRuntime` and it applies a default-deny egress NetworkPolicy to the lane pod before
any workload runs:

```python
import os
from resoluto_sandbox import Sandbox
from resoluto_sandbox.backends.substrate import SubstrateBackend, store_env_for_pod
from resoluto_sandbox.conduit.factory import store_from_env
from resoluto_sandbox.runtime.k8s import K8sSandboxRuntime, EgressConfig

egress = EgressConfig(
    store_cidr="192.168.1.197/32",        # your object store (minio / S3-compatible)
    llm_cidr="160.79.104.0/23",           # e.g. api.anthropic.com — resolve FQDN to CIDR yourself
    git_cidrs=["140.82.112.0/20"],        # optional: git hosts (empty list = no git egress)
)
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

The generated policy is **default-deny egress** with these explicit allow rules:

| Destination | Port | Protocol |
|---|---|---|
| `store_cidr` | 443 | TCP |
| `llm_cidr` | 443 | TCP |
| each `git_cidrs` entry | 443 | TCP |
| `0.0.0.0/0` | 53 | UDP (kube-dns) |

**IMDS is always blocked.** Every `ipBlock` rule includes `except: ["169.254.169.254/32"]` so
the cloud metadata endpoint is unreachable even when the allowed CIDR would cover it. This
prevents the workload from reading cloud credentials, instance identity, or user data from the
hypervisor.

### CIDR-only: no FQDNs

Kubernetes `NetworkPolicy` `ipBlock` does not accept hostnames. You must resolve every hostname
to a CIDR before constructing `EgressConfig`:

```python
EgressConfig(store_cidr="api.anthropic.com", ...)  # raises ValueError — no '/'
```

`EgressConfig.__post_init__` rejects any value that does not contain `/`. Cloud provider IP
ranges can rotate — widen the CIDR conservatively or re-resolve before each deployment.

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

Set `RESOLUTO_TRUSTED_LOCAL=1` to skip the canary (dev only — also permits host AWS credentials
to be forwarded to the pod in place of a scoped store token).

No integration test for the canary is provided here — the in-guest execution is the live
check. The `evaluate_verdict` pure function (unit-tested in `tests/test_egress_canary.py`)
covers the pass/fail logic.

---

## What you can manage

| Knob | Local backend | k8s backend |
|---|---|---|
| Egress allowlist (CIDRs) | not applicable — Docker (OS-level isolation, not egress-locked) | `EgressConfig(store_cidr, llm_cidr, git_cidrs)` passed to `K8sSandboxRuntime` |
| IMDS block | not applicable | always on when `EgressConfig` is passed |
| Egress canary | skipped (`RESOLUTO_TRUSTED_LOCAL=1` set by local preset) | on by default; skip with `RESOLUTO_TRUSTED_LOCAL=1` |
| Runtime class | not applicable | `kata` (pinned; non-Kata requires `RESOLUTO_TRUSTED_LOCAL`) |
| Kubecontext | not applicable | `RESOLUTO_SANDBOX_KUBECONTEXT` (fails closed if unset) |

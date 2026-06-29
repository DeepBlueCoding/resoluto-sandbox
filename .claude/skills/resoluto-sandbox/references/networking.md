# NETWORK ISOLATION & EGRESS POLICY

Agent reference for controlling what a sandboxed workload can reach over the network.
For the run protocol and backend contracts see `../../../../spec/PROTOCOL.md`; for conduits/staging see `operations.md`.

## TL;DR decision table

| Backend | Isolation | Egress | When |
|---|---|---|---|
| `local` | Kata microVM (hardware-virtualized) via nerdctl | default-deny on the host CNI bridge (egress canary RUNS, fail-closed): allow DNS + HTTPS-443-public; REJECT IMDS + RFC1918 private | dev and untrusted code at VM-grade isolation |
| `k8s` + `egress=None` | Kata microVM kernel isolation | UNRESTRICTED (no NetworkPolicy) | semi-trusted, kernel isolation enough |
| `k8s` + `egress=EgressConfig(...)` | Kata microVM + default-deny egress NetworkPolicy | only declared CIDRs :443 + DNS :53; IMDS always blocked | untrusted code |

Footgun: `k8s` does NOT lock down egress unless you pass `EgressConfig`. Kata isolates the kernel, not the network. Untrusted code with `egress=None` can phone home anywhere. (The `local` backend is different: egress is always enforced host-side on the CNI bridge — immune to in-guest root.)

## API surface (verbatim)

```python
from resoluto_sandbox import Sandbox                                              # facade
from resoluto_sandbox.backends.substrate import SubstrateBackend, store_env_for_pod  # ONE backend impl
from resoluto_sandbox.conduit.factory import store_from_env                      # conduit from env
from resoluto_sandbox.runtime.k8s import K8sSandboxRuntime, EgressConfig         # k8s runtime + CIDR allowlist
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
- `egress` — `EgressConfig` (from `resoluto_sandbox.runtime.k8s`). All fields MUST be
  CIDR notation (`x.x.x.x/32`); NetworkPolicy `ipBlock` rejects FQDNs — resolve hostnames
  to IPs yourself first or `__post_init__` raises `ValueError`. `None` → unrestricted egress.

`Sandbox(backend="k8s")` constructs the k8s preset (reads `RESOLUTO_LANE_IMAGE` + `RESOLUTO_STORE_KIND`
from env) — only useful for simple cases; inject `SubstrateBackend` for egress/conduit config.

## Status: this is implemented, not roadmap

The `k8s` backend is FULLY implemented — `SubstrateBackend.run` launches a real Kata pod via `drive_node`, applies the NetworkPolicy, stages workspace in / artifacts out. The ONLY real limit on both backends:
- `stdin is not None` → `NotImplementedError` on BOTH backends

Dependencies must be baked into the image.

Conduits: `local`/`StdoutConduit` (local backend bind-mount) and S3-against-minio (k8s) are PROVEN. `GcsConduit` is experimental/unverified — do not rely on it for isolation guarantees.

## `EgressConfig` (CIDR allowlist)

`@dataclass(frozen=True)`, from `resoluto_sandbox.runtime.k8s`:

```python
EgressConfig(
    store_cidr: str,              # REQUIRED — object store endpoint CIDR
    llm_cidr: str,                # REQUIRED — LLM provider API CIDR (e.g. api.anthropic.com)
    git_cidrs: list[str] = [],    # OPTIONAL — git host CIDRs; empty = NO git egress allowed
)
```

CIDR-ONLY — NO FQDNs. k8s `NetworkPolicy` `ipBlock` does not accept hostnames. `__post_init__` raises `ValueError` on any value missing `/`:
```python
EgressConfig(store_cidr="api.anthropic.com", llm_cidr="...")  # ValueError — not a CIDR
```
Resolve hostnames to CIDRs YOURSELF before constructing. Cloud provider ranges rotate — widen conservatively or re-resolve before each deploy. A single host = `/32`.

Footgun: do NOT add `EgressConfig` to the top-level `resoluto_sandbox` import — that pulls `kubernetes_asyncio` in eagerly. Import from `resoluto_sandbox.runtime.k8s`.

## What the generated NetworkPolicy does

Built by `K8sSandboxRuntime._network_policy` and applied at launch ONLY when `egress is not None`. It is `policyTypes: ["Egress"]` with `podSelector` matching the lane's labels = **default-deny egress** plus exactly these allow rules:

| Destination | Port / Proto |
|---|---|
| `store_cidr` | 443 / TCP |
| `llm_cidr` | 443 / TCP |
| each `git_cidrs` entry | 443 / TCP |
| `0.0.0.0/0` | 53 / UDP (kube-dns) |

IMDS ALWAYS BLOCKED: every `ipBlock` rule (including the broad `0.0.0.0/0` DNS rule) carries `except: ["169.254.169.254/32"]`. The cloud metadata endpoint is unreachable even if an allowed CIDR would otherwise cover it — no credential/identity/user-data leakage from the hypervisor. This is unconditional; you cannot allowlist IMDS through `EgressConfig`.

Note: only TCP/443 is opened to allowlisted hosts. A non-443 destination (e.g. plain HTTP :80, custom ports) is denied — your store/LLM/git endpoints must be reachable on 443.

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
from resoluto_sandbox.runtime.k8s import K8sSandboxRuntime, EgressConfig

egress = EgressConfig(
    store_cidr="192.168.1.197/32",     # your object store (minio / S3-compatible)
    llm_cidr="160.79.104.0/23",        # api.anthropic.com — resolve FQDN to CIDR yourself
    git_cidrs=["140.82.112.0/20"],     # optional git hosts; [] = no git egress
)

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
Kata microVM (hardware-virtualized) via nerdctl + a dedicated containerd on the host. `EgressConfig`
(a k8s NetworkPolicy knob) does not apply here — local egress is enforced HOST-SIDE on the lane CNI
bridge (default-deny; allow DNS + HTTPS-443-public; REJECT IMDS + RFC1918 private), immune to in-guest
root. The egress canary RUNS (fail-closed). Suitable for untrusted code at VM-grade isolation. Needs
`/dev/kvm` + nerdctl + the dedicated containerd + an image (default `resoluto-sandbox-base:dev`).

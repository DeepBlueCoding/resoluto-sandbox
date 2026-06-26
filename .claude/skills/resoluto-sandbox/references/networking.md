# NETWORK ISOLATION & EGRESS POLICY

Agent reference for controlling what a sandboxed workload can reach over the network.
For the run protocol and backend contracts see `../../../../spec/PROTOCOL.md`; for conduits/staging see `operations.md`.

## TL;DR decision table

| Backend | Isolation | Egress | When |
|---|---|---|---|
| `local` | NONE — host subprocess, host network | unrestricted, inherits host firewall/DNS | trusted code only |
| `k8s` + `egress=None` | Kata microVM kernel isolation | UNRESTRICTED (no NetworkPolicy) | semi-trusted, kernel isolation enough |
| `k8s` + `egress=EgressConfig(...)` | Kata microVM + default-deny egress NetworkPolicy | only declared CIDRs :443 + DNS :53; IMDS always blocked | untrusted code |

Footgun: `k8s` does NOT lock down egress unless you pass `EgressConfig`. Kata isolates the kernel, not the network. Untrusted code with `egress=None` can phone home anywhere.

## API surface (verbatim)

```python
from resoluto_sandbox import Sandbox                  # facade
from resoluto_sandbox.backends.k8s import K8sBackend  # k8s backend (lazy k8s deps)
from resoluto_sandbox.runtime.k8s import EgressConfig # CIDR allowlist — import from here ONLY
```

`Sandbox(backend="local" | "k8s" | <Backend instance>)` then:

```python
RunResult = Sandbox.run(
    argv,                       # Sequence[str], the program to run (plain — never imports resoluto_sandbox)
    *,
    workspace=None,             # str | None — program cwd; outputs extracted here in place
    stdin=None,                 # str | bytes | None  (k8s: NotImplementedError — see footguns)
    env=None,                   # dict[str,str] | None — overlays host env
    output_paths=None,          # Sequence[str] | None — globs collected into RunResult.artifacts
    stream=None,                # IO[str] | None — live output sink (default sys.stdout)
)
```

`RunResult` (pydantic):
```
exit_code: int
output: str          # k8s: MERGED stdout+stderr (in-pod runner emits both as log spans)
errors: str          # k8s: always "" by design
artifacts: list[str] # collected output_paths
result: dict | None  # parsed result.json if the program wrote one, else None
reason: str          # substrate forensics (evicted/OOMKilled/observed phase); "" for local
ok -> bool           # property: exit_code == 0
```

k8s config is a backend concern — inject a configured `K8sBackend`, never a string:
```python
K8sBackend(image=None, conduit=None, egress=None)
```
- `image: str | None` — REQUIRED for a real run (`run()` raises `ValueError` if `None`). The lane image.
- `conduit: Conduit | None` — object store the pod self-reports through. `None` → built from env via `store_from_env()` (needs `RESOLUTO_STORE_KIND`).
- `egress: EgressConfig | None` — the allowlist below. `None` → unrestricted egress.

`Sandbox(backend="k8s")` constructs a bare `K8sBackend()` (no image) — only useful for wiring tests; a real run needs the injected form.

## Status: this is implemented, not roadmap

The `k8s` backend is FULLY implemented — `K8sBackend.run` launches a real Kata pod via `drive_node`, applies the NetworkPolicy, stages workspace in / artifacts out. The ONLY real limit on `k8s`:
- `stdin is not None` → `NotImplementedError("stdin is not supported on backend='k8s'")`

Dependencies must be baked into the image.

Conduits: `local`/`StdoutConduit` (local backend) and S3-against-minio (k8s) are PROVEN. `GcsConduit` is experimental/unverified — do not rely on it for isolation guarantees.

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

NetworkPolicy enforcement needs a NetworkPolicy-capable CNI (Calico, Cilium, Flannel-with-NetworkPolicy). k3s ships one enabled by default. On a cluster WITHOUT a NetworkPolicy controller the manifest is created but SILENTLY NOT ENFORCED. The egress canary (below) is the empirical backstop that catches this.

## Egress canary (`egress_canary.py`)

Runs IN-GUEST before the workload, three probes:
1. Non-allowlisted external TCP (default `1.1.1.1:80`) — must be BLOCKED.
2. IMDS TCP `169.254.169.254:80` — must be BLOCKED.
3. Store PUT sentinel (`<prefix>/_canary_ok`) — must SUCCEED.

Any unexpected result → lane aborts with a reason naming every failed probe (surfaced via `SpanEmitter`). This fires when the CNI mis-enforces the policy or the policy was not applied before the pod started.

API:
```python
run_egress_canary(store: Conduit, prefix: str,
                  probe_host: str = "1.1.1.1", probe_port: int = 80) -> CanaryVerdict
evaluate_verdict(results: list[ProbeResult]) -> CanaryVerdict   # pure; unit-testable, no network
```
`CanaryVerdict(passed: bool, results: list[ProbeResult], reason: str)`; `reason == ""` on pass.

### `RESOLUTO_TRUSTED_LOCAL=1` bypass (DEV ONLY)

Set `RESOLUTO_TRUSTED_LOCAL=1` to skip the canary. Side effects you must understand:
- Also permits host `AWS_*` creds to be forwarded into the pod in place of a scoped store token (`_store_env_for_pod`). Without it, a k8s run with host AWS creds but no `RESOLUTO_STORE_WRITE_TOKEN` raises `RuntimeError` — the pod is meant to auth via the prefix-scoped `RESOLUTO_STORE_WRITE_TOKEN`.
- Also gates the non-Kata `runtime_class` guard.
Never set it for untrusted code.

## Related k8s knobs (env)

- `RESOLUTO_SANDBOX_KUBECONTEXT` — PINS the kube-context. If unset and not in-cluster, launch FAILS CLOSED (refuses the ambient current-context). Override only with `RESOLUTO_SANDBOX_ALLOW_AMBIENT_CONTEXT=1`. Prevents launching adversarial pods on the wrong (even prod) cluster.
- `RESOLUTO_SANDBOX_NAMESPACE` (default `resoluto-sandboxes`), `RESOLUTO_LANE_IMAGE_PULL_POLICY` (default `IfNotPresent`).
- `RESOLUTO_STORE_KIND` — required for the k8s conduit when `conduit=None`.

## Copy-paste: locked-down k8s run

```python
from resoluto_sandbox import Sandbox
from resoluto_sandbox.backends.k8s import K8sBackend
from resoluto_sandbox.runtime.k8s import EgressConfig

egress = EgressConfig(
    store_cidr="192.168.1.197/32",     # your object store (minio / S3-compatible)
    llm_cidr="160.79.104.0/23",        # api.anthropic.com — resolve FQDN to CIDR yourself
    git_cidrs=["140.82.112.0/20"],     # optional git hosts; [] = no git egress
)

sb = Sandbox(backend=K8sBackend(
    image="<registry>/resoluto-lane:dev",
    egress=egress,                     # omit / None => UNRESTRICTED egress
))

result = sb.run(
    ["python", "agent.py"],
    workspace="/abs/path/to/workspace",
    env={"TASK": "fix the bug"},
    output_paths=["out/**"],
)
assert result.ok, result.reason
print(result.output)            # merged stdout+stderr on k8s
print(result.artifacts)         # collected output_paths
```

## Copy-paste: local (NO isolation)

```python
from resoluto_sandbox import Sandbox
result = Sandbox(backend="local").run(["python", "agent.py"], workspace=".")
```
Host network, host firewall/DNS, full host connectivity. `EgressConfig` does not apply. Trusted code only.

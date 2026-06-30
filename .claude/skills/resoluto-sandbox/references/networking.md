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

## `EgressConfig` (the k8s allowlist) — the REAL fields

`@dataclass(frozen=True)`, from `resoluto_sandbox.runtime.k8s`. It has exactly TWO fields — there is
NO `llm_cidr`/`git_cidrs` (any HTTPS is already allowed via the blanket public-443 rule below):

```python
EgressConfig(
    store_cidr: str,          # REQUIRED — object store endpoint CIDR (e.g. "10.0.0.5/32")
    store_port: int = 443,    # the store's port (minio is often 9000/9100)
)
```

`store_cidr` is CIDR-ONLY — k8s `NetworkPolicy` `ipBlock` rejects FQDNs; `__post_init__` raises
`ValueError` on a value missing `/`. Build it from the env with `EgressConfig.from_store_env()`,
which reads `RESOLUTO_STORE_ENDPOINT` and honors `RESOLUTO_STORE_EGRESS_CIDR` / `RESOLUTO_STORE_EGRESS_PORT`
overrides (use these when the store is behind a DNAT — NetworkPolicy is evaluated POST-DNAT).

Footgun: do NOT add `EgressConfig` to the top-level `resoluto_sandbox` import — that pulls
`kubernetes_asyncio` in eagerly. Import from `resoluto_sandbox.runtime.k8s`.

## What the generated NetworkPolicy does

Built by `K8sSandboxRuntime._network_policy` and applied at launch ONLY when `egress is not None`.
`policyTypes: ["Egress"]`, `podSelector` = the lane's labels → **default-deny egress** plus exactly
these three allow rules:

| Destination | Port / Proto | Note |
|---|---|---|
| `store_cidr` | `store_port` / TCP | your object store (the ONE you configure) |
| `0.0.0.0/0` | 443 / TCP | ALL public HTTPS — LLM + git + any HTTPS API, no per-host config |
| `0.0.0.0/0` | 53 / UDP + TCP | DNS |

IMDS ALWAYS BLOCKED: the two broad `0.0.0.0/0` rules carry `except: ["169.254.169.254/32"]`, so the
cloud metadata endpoint is unreachable on 443/53 even though they are otherwise open. The `store_cidr`
rule is a specific host so it carries no `except`. Unconditional — you cannot allowlist IMDS.

> **The whitelist is essentially fixed: `store_cidr:store_port` + every host on 443 + DNS.** There is
> no per-host LLM/git allowlist and no blacklist primitive (it's default-deny; the only hardcoded deny
> is IMDS). See "Modifying the egress allowlist" below to change it.

## Modifying the egress allowlist (whitelist / blacklist)

The model is **default-deny whitelist** on both backends. There is no per-rule "blacklist" — you
"blacklist" by NOT allowing (or by removing an allow), and IMDS + (local) RFC1918 are hardcoded denies.

### k8s — what you CAN do with `EgressConfig` vs what needs a code change

- **Move/relax the store rule:** `EgressConfig(store_cidr=..., store_port=...)`, or the env overrides
  `RESOLUTO_STORE_EGRESS_CIDR` / `RESOLUTO_STORE_EGRESS_PORT`. This is the only allowlist knob exposed.
- **Allow any HTTPS host:** already allowed — the blanket `0.0.0.0/0:443` rule covers all LLM/git/API
  traffic. Nothing to configure.
- **Turn egress OFF (allow everything):** `egress=None` (Kata kernel isolation only).
- **Tighten 443 to specific hosts, allow a NON-443 destination, or add an explicit deny:** NOT possible
  through `EgressConfig` — edit `K8sSandboxRuntime._network_policy` (in `runtime/k8s.py`) and append
  `ipBlock` rules to the `egress` list, e.g. a CIDR-pinned 443 rule instead of `0.0.0.0/0`, or an extra
  `{"ports": [{"port": 6379, "protocol": "TCP"}], "to": [{"ipBlock": {"cidr": "<cidr>"}}]}`. Remember a
  k8s `ipBlock` is allow-only with `except` carve-outs; there is no DROP rule — to "blacklist" a host you
  must replace the broad `0.0.0.0/0` allow with narrower CIDRs that exclude it.

### local — edit the host-side firewall

The local backend's egress is plain `iptables` on the lane bridge, set up by the provisioner
`scripts/local-backend-up.sh` (in the workspace root), step "4/7 egress firewall". The chain (first
match wins) is:

```bash
$CHAIN  -m state --state ESTABLISHED,RELATED -j ACCEPT
$CHAIN  -p udp/tcp --dport 53            -j ACCEPT     # DNS
$CHAIN  -d 169.254.0.0/16                 -j REJECT     # IMDS
$CHAIN  -d 10.0.0.0/8 / 172.16.0.0/12 / 192.168.0.0/16 -j REJECT   # RFC1918 (no lateral movement)
$CHAIN  -p tcp --dport 443                -j ACCEPT     # HTTPS public
$CHAIN                                     -j REJECT     # default-deny
```

- **Whitelist** an extra destination: add an `ACCEPT` BEFORE the final `-j REJECT` (and, for a private
  target, before the RFC1918 `REJECT`s):
  `sudo iptables -A "$CHAIN" -p tcp --dport 6379 -d 203.0.113.5/32 -j ACCEPT`.
- **Blacklist** a specific public host: add a `REJECT` for it BEFORE the `--dport 443 ACCEPT`:
  `sudo iptables -A "$CHAIN" -d 203.0.113.9/32 -j REJECT`.
- Edit the rules in `scripts/local-backend-up.sh` (so they survive a re-provision) and re-run it; the
  Kata canary at the end re-verifies enforcement.

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
    store_port=9100,                   # the store's port (default 443). LLM/git need NO config —
)                                      # they're covered by the blanket public-443 allow rule.
# or: egress = EgressConfig.from_store_env()   # derive store_cidr:port from RESOLUTO_STORE_ENDPOINT

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

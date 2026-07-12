# Backends

`Sandbox` delegates every run to a pluggable `Backend`. This page covers the two
built-in backends, how to install the k8s stack, and how to wire in a custom backend.

## Overview

| backend | isolation | where it runs | needs | use for |
|---------|-----------|---------------|-------|---------|
| `local` | Kata microVM + host-side egress policy | your machine | containerd + nerdctl + an image | dev and most workloads, single host, no cluster |
| `k8s` | Kata microVM + egress policy | a Kubernetes cluster | k8s + Kata + S3 store + image | untrusted code at scale, locked-down egress, production |

Both backends share one substrate: `SubstrateBackend` drives a run through a
`SandboxRuntime` (the isolation/placement seam) and a `Conduit` (the host/sandbox
exchange). The backends differ only in which runtime and conduit are wired in.

> For the components glossary, the layered local-vs-k8s architecture diagram, the Conduit
> data-flow table, and the security-layers table, see the [README's "How it works"
> section](https://github.com/DeepBlueCoding/resoluto-sandbox#how-it-works) — this page
> picks up from there with per-backend setup detail.

## Run lifecycle

One flow for both backends; only the runtime and conduit differ. Liveness is a silence
watchdog: if no chunk arrives for 600 seconds the sandbox is considered dead. There is no
wall-clock timeout on the work itself — a live sandbox runs as long as it keeps emitting.

`stdin` is not supported on either backend — `NotImplementedError` if you pass
`stdin=`. Pass inputs via argv, env, or workspace files.

The in-sandbox runner merges stdout and stderr as `log` span events, so
`RunResult.output` carries the merged stream and `RunResult.errors` is always `""`.
This is intentional, not a dropped field. On k8s, `RunResult.reason` carries
substrate forensics when a pod is evicted or OOM-killed, and is `""` on a normal exit.

## Sizing — memory, CPU, disk

The facade defaults each sandbox to 4 GiB / 2 CPU. To size it, pass a `Resources` to the injected
`SubstrateBackend` — **both backends honor it** (k8s renders pod requests/limits, `local` renders
`nerdctl --memory`/`--cpus`):

```python
from resoluto.sandbox import Sandbox
from resoluto.sandbox.contracts import Resources
from resoluto.sandbox.backends.substrate import SubstrateBackend

sb = Sandbox(backend=SubstrateBackend(
    runtime=runtime, conduit=conduit, image="<registry>/resoluto-sandbox-base:0.1.0",
    store_env=store_env,
    resources=Resources.from_quantities(memory="16Gi", cpu="4", disk="40Gi"),  # default: 4Gi / 2 cpu
))
```

`Resources.from_quantities` takes human quantity strings (`"16Gi"`, `"4"`, `"40Gi"`). The disk-backed
docker image graph (`graph_backend="block"`, `dind_graph=...`) is a dind-only knob on the direct
`drive_node` path — see [Concurrency & direct control](concurrency.md#advanced-the-direct-drive_node-path).

## local

`backend="local"` runs the program in a Kata microVM launched via `nerdctl` against a
dedicated, standalone containerd on this host (its own socket/root at
`/run/resoluto-local/containerd/containerd.sock`), via `KataNerdctlSandboxRuntime`.
It never assumes k3s and never touches Docker's or k3s's containerd. The host and
microVM share a `LocalConduit` over a bind-mounted directory (`/conduit`). No cluster,
no S3 — everything stays on your machine:

```
Sandbox(backend="local").run(argv, workspace, output_paths)
   └─ SubstrateBackend (KataNerdctlSandboxRuntime + LocalConduit)
        nerdctl run --rm -v <conduit>:/conduit <image>   # Kata microVM, standalone containerd
          runner_main stages workspace → /workspace
          runs argv
          ships spans + heartbeat to /conduit
          writes result.json + outbox to /conduit
   → RunResult(output, exit_code, artifacts)     # no k8s, no S3
```

Egress is denied by default, and the default is absolute: a run with no allowlist gets **no network
interface**. The store is a host bind mount, not a network endpoint, so a run works with zero network.
Opening an allowlist attaches a NIC on the CNI bridge, enforced host-side (immune to in-guest root),
with IMDS `169.254.169.254` + RFC1918 private ranges always rejected. Grant domains per run with
`Sandbox.run(egress=[...])`, or set provision-time defaults (`RESOLUTO_EGRESS_ALLOW` for specific
destinations, `RESOLUTO_EGRESS_PUBLIC_HTTPS=1` for all outbound :443). The egress canary runs
fail-closed before your workload; there is no trusted-local bypass.

### What you need

- KVM + Kata + a `nerdctl-full` bundle + Docker + a `localhost:5000` registry on this Linux host —
  see the README's [Requirements](https://github.com/DeepBlueCoding/resoluto-sandbox#requirements-host) for how to install each. Provision
  the rest with `scripts/local-backend-up.sh` (ends with a green Kata-microVM canary).
- An image with python + the resoluto-sandbox wheel + your program's deps. Default:
  `default_local_image()` = the base image, registry-qualified (`localhost:5000/resoluto-sandbox-base:<installed
  wheel version>`) — computed from the running package version, never a floating tag. Override with
  `Sandbox(backend="local", image="…")`.
- **How a built image reaches this backend — via the registry.** `docker build` (including
  `resoluto-sandbox image build`) lands the image in your regular Docker daemon, a **separate** store
  from the dedicated containerd this backend reads. The bridge is the on-box registry: `image build`
  **pushes** there, and the backend **pulls on demand** (`localhost` is insecure/HTTP by default). For
  your own image, `docker build … && resoluto-sandbox image push <tag>`, then reference
  `localhost:5000/<tag>`. Opt out with `RESOLUTO_SANDBOX_REGISTRY=""` (bare tags + `docker save |
  nerdctl load`). See the README's [Prebuilt provider images](https://github.com/DeepBlueCoding/resoluto-sandbox#prebuilt-provider-images).

## k8s

Each `run()` call launches one short-lived Kata microVM pod. The host and pod exchange
data through a `Conduit` (an S3-compatible object store); there is no long-lived
connection between them.

```
   host (your process)            Conduit  (S3 / minio / …)        Kata microVM pod (k8s)
   ───────────────────           ───────────────────────         ──────────────────────
   put_dir(workspace) ─────────────▶  inbox/ *.tar.gz ───────────▶  stage inputs -> /workspace
   drive_node: launch pod ───────────────────────────────────────▶  run argv (RESOLUTO_WORKLOAD_ARGV)
   tail ChunkReader  ◀───────────── events-000001.jsonl ◀──────────  ship spans + heartbeat
   read result.json  ◀───────────── result.json ◀─────────────────  write verdict
   fetch_outputs     ◀───────────── outbox/ *.tar.gz ◀────────────  collect output_paths
   reap pod
   → RunResult(output reconstructed from chunks, exit_code, artifacts)
```

Dependencies must be baked into the image — the pod has no package-manager access at
runtime.

### Usage

The image is not a `Sandbox` concern — inject a configured `SubstrateBackend`:

```python
import os
from resoluto.sandbox import Sandbox
from resoluto.sandbox.backends.substrate import SubstrateBackend, store_env_for_pod
from resoluto.sandbox.conduit.factory import store_from_env
from resoluto.sandbox.runtime.k8s import K8sSandboxRuntime

runtime = K8sSandboxRuntime(
    namespace=os.environ.get("RESOLUTO_SANDBOX_NAMESPACE", "resoluto-sandboxes"),
    context=os.environ.get("RESOLUTO_SANDBOX_KUBECONTEXT"),
)
sb = Sandbox(backend=SubstrateBackend(
    runtime=runtime,
    conduit=store_from_env(),
    image="<registry>/resoluto-sandbox-base:0.1.0",
    store_env=store_env_for_pod(os.environ),
))
result = sb.run(["bash", "-lc", "echo hi"], workspace="./proj", output_paths=["*.txt"])
print(result.output)   # "hi"
print(result.ok)       # True
```

Or use the convenience shortcut (reads `RESOLUTO_SANDBOX_IMAGE` and `RESOLUTO_STORE_KIND` from env):

```python
Sandbox(backend="k8s", image="<registry>/resoluto-sandbox-base:0.1.0").run(...)
```

### Optional: egress lockdown

By default Kata provides kernel isolation but places no restriction on network egress.
For untrusted code, add `EgressConfig` to apply a default-deny `NetworkPolicy` that
permits only: the object store (`store_cidr:store_port`), public HTTPS (TCP/443 to
anywhere — covers the LLM API + git, no fragile FQDN→/32 pinning), and DNS. IMDS
(`169.254.169.254`) and everything else are denied.

Same wiring as [Usage](#usage) above — just pass `egress=` to the runtime:

```python
from resoluto.sandbox.runtime.k8s import K8sSandboxRuntime, EgressConfig

# EgressConfig.from_store_env() builds this from RESOLUTO_STORE_ENDPOINT.
runtime = K8sSandboxRuntime(
    namespace="resoluto-sandboxes",
    context=os.environ.get("RESOLUTO_SANDBOX_KUBECONTEXT"),
    egress=EgressConfig(store_cidr="10.0.0.5/32", store_port=9000),  # store; port default 443
)
# ...then inject it into SubstrateBackend exactly as in Usage.
```

> NetworkPolicy is evaluated post-DNAT. If the store is reached via DNAT (a dockerized
> minio published on the node, a NodePort), allowlist the *real backend* IP:port, not the
> published endpoint — set `RESOLUTO_STORE_EGRESS_CIDR` (+ `RESOLUTO_STORE_EGRESS_PORT`)
> and `from_store_env()` honors it. The policy is created before the pod, and the
> in-guest egress canary briefly retries to absorb the NetworkPolicy controller's
> programming lag.

`store_cidr` must be `x.x.x.x/nn` — `NetworkPolicy` `ipBlock` does not accept hostnames;
resolve FQDNs yourself. IMDS (`169.254.169.254/32`) is always blocked regardless of the
allowlist. See `docs/networking.md` for the full egress reference.

## Installing the k8s stack

The k8s backend works on any Kubernetes distribution — self-hosted (k3s, kind, microk8s)
or managed (EKS, GKE, AKS). Follow these steps in order.

### 1. A Kubernetes cluster

- Self-hosted (local/dev): [k3s](https://k3s.io), [kind](https://kind.sigs.k8s.io),
  [minikube](https://minikube.sigs.k8s.io), [microk8s](https://microk8s.io)
- Managed (production): EKS (AWS), GKE (GCP), AKS (Azure)

The cluster must be reachable from your host via `kubectl`. Confirm:

```bash
kubectl cluster-info
```

### 2. Kata Containers (hardware isolation)

Install the Kata Containers runtime on each node: https://katacontainers.io/docs/

Then create a `RuntimeClass` named `kata`:

```yaml
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: kata
handler: kata
```

```bash
kubectl apply -f kata-runtimeclass.yaml
```

Verify:

```bash
kubectl get runtimeclass kata
```

> Some managed clusters offer Kata as an optional add-on (e.g. GKE Sandbox). Check your
> provider's documentation before installing from scratch.

### 3. A NetworkPolicy-enforcing CNI (optional)

Only needed if you plan to use `EgressConfig`. Common choices: Calico, Cilium, or Flannel
with the NetworkPolicy controller. Many managed clusters (EKS with VPC CNI + Network
Policy, GKE Dataplane V2, AKS with Azure CNI) support NetworkPolicy natively — check
whether it needs to be enabled first.

> Without a NetworkPolicy-capable CNI the policy manifest is applied but silently not
> enforced. The egress canary (run in-guest before your workload) detects this.

### 4. An S3-compatible object store

The host and pods exchange data through a shared object store reachable from both your
host machine and the pods inside the cluster.

Option A — minio (local/dev):

```bash
docker run -d --name minio \
  -p 9000:9000 -p 9001:9001 \
  -e MINIO_ROOT_USER=minioadmin \
  -e MINIO_ROOT_PASSWORD=minioadmin \
  quay.io/minio/minio server /data --console-address ":9001"
```

Create a bucket named `resoluto`:

```bash
# with the mc CLI (https://min.io/docs/minio/linux/reference/minio-mc.html):
mc alias set local http://localhost:9000 minioadmin minioadmin
mc mb local/resoluto
```

> The endpoint must be routable from the pods. `localhost` from the host is not reachable
> inside pods — use the host's LAN IP or a cluster-internal service address.

Option B — cloud S3: create a bucket on AWS S3 (or any S3-compatible provider) and set
the bucket policy to allow the credentials you export below.

### 5. Build and push a provider image

`image build` tags by the pinned SDK package + version, e.g. `resoluto-sandbox:claude-agent-sdk-0.2.110`
(see `images.py:SDK_VERSION`). Retag to your registry path before pushing:

```bash
tag=$(resoluto-sandbox image build --provider claude | awk '{print $1}')   # first field = the built tag
docker tag "$tag" <registry>/resoluto-sandbox-base:0.1.0
docker push <registry>/resoluto-sandbox-base:0.1.0
```

Set the image in the environment:

```bash
export RESOLUTO_SANDBOX_IMAGE=<registry>/resoluto-sandbox-base:0.1.0
```

### 6. Export environment variables

```bash
# Pin the kube context (required — backend refuses to launch without this)
export RESOLUTO_SANDBOX_KUBECONTEXT=<your-context-name>

# Namespace for sandbox pods (default: resoluto-sandboxes)
export RESOLUTO_SANDBOX_NAMESPACE=resoluto-sandboxes

# Image to run inside each pod
export RESOLUTO_SANDBOX_IMAGE=<registry>/resoluto-sandbox-base:0.1.0

# Conduit: S3-compatible store
export RESOLUTO_STORE_KIND=s3
export RESOLUTO_STORE_ENDPOINT=http://<minio-host>:9000   # omit for AWS S3
export RESOLUTO_STORE_BUCKET=resoluto
export RESOLUTO_STORE_REGION=us-east-1

# Store credentials — use a scoped token (preferred) or ambient AWS creds:
export AWS_ACCESS_KEY_ID=<your-key-id>
export AWS_SECRET_ACCESS_KEY=<your-secret>
```

Replace all `<placeholders>` with your real values. Do not commit secrets.

`RESOLUTO_SANDBOX_KUBECONTEXT` is required — the backend fails closed if it is unset, to
prevent accidentally targeting the wrong cluster. Use
`RESOLUTO_SANDBOX_ALLOW_AMBIENT_CONTEXT=1` only in carefully controlled environments.

### 7. Smoke test

Wire `sb` exactly as in [Usage](#usage) (here reading `image=os.environ["RESOLUTO_SANDBOX_IMAGE"]`), then:

```python
result = sb.run(["bash", "-lc", "echo hi from kata"])
print(result.output)   # "hi from kata"
assert result.ok
```

If the run hangs, check that the pod can reach the store endpoint (`kubectl logs -n
resoluto-sandboxes <pod-name>`) and that `RESOLUTO_SANDBOX_KUBECONTEXT` points at the
right cluster.

## Adding another backend

To add a new isolation target, implement `SandboxRuntime` (the isolation/placement seam)
and wire it into `SubstrateBackend`:

```python
from resoluto.sandbox.backends.substrate import SubstrateBackend
from resoluto.sandbox.conduit import LocalConduit

sb = Sandbox(backend=SubstrateBackend(
    runtime=MyRuntime(...),
    conduit=LocalConduit("/tmp/conduit"),
    image="my-image:tag",
    store_env={"RESOLUTO_STORE_KIND": "localfs", "RESOLUTO_STORE_ROOT": "/conduit"},
))
```

For a completely new run approach (not store-mediated), implement the `Backend` ABC
(one method: `run(...) -> RunResult`) and inject it:

```python
from resoluto.sandbox.backends.base import Backend, RunResult

class MyBackend(Backend):
    def run(self, argv, *, workspace=None, stdin=None, env=None, env_file=None,
            secrets=None, output_paths=None, stream=None, egress=None) -> RunResult:
        ...

Sandbox(backend=MyBackend(...)).run(argv, ...)
```

No facade changes are needed — `Sandbox` holds any `Backend` instance. `Conduit` and
`SandboxRuntime` are the other two extension points: implement the ABC, pass your instance
to `Sandbox`, and the facade drives it unchanged. See the [Runtime & Contracts](api/runtime.md)
reference for the full protocol each one must satisfy.

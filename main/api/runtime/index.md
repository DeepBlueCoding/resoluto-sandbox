# Runtime & Contracts

The isolation/placement seam (`SandboxRuntime`), the backend that drives the stage → run → collect flow, the platform-neutral launch spec, and the two concrete runtimes (`K8sSandboxRuntime`, `KataNerdctlSandboxRuntime`). `store_env_for_pod` derives the store environment a pod needs from the host environment.

## resoluto.sandbox.SandboxRuntime

Bases: `ABC`

The platform-specific surface that launches, polls, and destroys a sandbox.

### sweep

```python
sweep(labels)
```

Destroy every sandbox whose labels include all given pairs; return count destroyed.

Source code in `src/resoluto/sandbox/contracts.py`

```python
@abstractmethod
async def sweep(self, labels: dict[str, str]) -> int:
    """Destroy every sandbox whose labels include all given pairs; return count destroyed."""
```

### logs

```python
logs(handle, *, tail=200)
```

Return tail lines of substrate-side logs for forensics.

Source code in `src/resoluto/sandbox/contracts.py`

```python
async def logs(self, handle: SandboxHandle, *, tail: int = 200) -> str:
    """Return tail lines of substrate-side logs for forensics."""
    raise NotImplementedError
```

## resoluto.sandbox.Backend

Bases: `ABC`

Runs a program and returns a RunResult.

## resoluto.sandbox.SubstrateBackend

```python
SubstrateBackend(
    *,
    runtime,
    conduit,
    image,
    store_env,
    resources=None,
    dead_after_s=600.0,
)
```

Bases: `Backend`

Runs a program in a sandbox via an injected runtime, conduit, image, and store env.

Source code in `src/resoluto/sandbox/backends/substrate.py`

```python
def __init__(
    self,
    *,
    runtime: SandboxRuntime,
    conduit: Conduit,
    image: str,
    store_env: dict[str, str],
    resources: Resources | None = None,
    dead_after_s: float = 600.0,
) -> None:
    if not image:
        raise ValueError("SubstrateBackend requires image=...")
    self._runtime = runtime
    self._conduit = conduit
    self._image = image
    self._store_env = store_env
    self._resources = resources or Resources.from_quantities(memory="4Gi", cpu="2")
    self._dead_after_s = dead_after_s
```

## resoluto.sandbox.SandboxLaunchSpec

Bases: `BaseModel`

Platform-neutral spec the caller hands a runtime to launch one sandbox.

## resoluto.sandbox.SandboxHandle

Bases: `BaseModel`

## resoluto.sandbox.SandboxStatus

Bases: `BaseModel`

## resoluto.sandbox.backends.substrate.store_env_for_pod

```python
store_env_for_pod(environ)
```

Select the RESOLUTO_STORE\_\* env the sandbox may inherit; host AWS creds are never forwarded.

Source code in `src/resoluto/sandbox/backends/substrate.py`

```python
def store_env_for_pod(environ: "os._Environ[str] | dict[str, str]") -> dict[str, str]:
    """Select the RESOLUTO_STORE_* env the sandbox may inherit; host AWS creds are never forwarded."""
    selected = {k: v for k, v in environ.items() if k.startswith("RESOLUTO_STORE_")}
    if selected.get("RESOLUTO_STORE_WRITE_TOKEN"):
        return selected
    if any(k.startswith("AWS_") for k in environ) and selected.get("RESOLUTO_STORE_KIND") == "s3":
        raise RuntimeError(
            "the sandbox needs a scoped RESOLUTO_STORE_WRITE_TOKEN for an s3 store — "
            "host AWS creds are never forwarded (no trusted-local bypass)."
        )
    return selected
```

## resoluto.sandbox.runtime.k8s.K8sSandboxRuntime

```python
K8sSandboxRuntime(
    *,
    namespace="resoluto-sandboxes",
    kubeconfig=None,
    context=None,
    image_pull_policy="IfNotPresent",
    egress=None,
    node_allocatable_memory=None,
    runtime_class="kata",
)
```

Bases: `SandboxRuntime`

Source code in `src/resoluto/sandbox/runtime/k8s.py`

```python
def __init__(
    self,
    *,
    namespace: str = "resoluto-sandboxes",
    kubeconfig: str | None = None,
    context: str | None = None,
    image_pull_policy: str = "IfNotPresent",
    egress: EgressConfig | None = None,
    node_allocatable_memory: str | None = None,
    runtime_class: str = "kata",
) -> None:
    self._ns = namespace
    self._kubeconfig = kubeconfig
    self._runtime_class = runtime_class
    self._context = context
    self._ipp = image_pull_policy
    self._egress = egress
    self._node_allocatable_memory = node_allocatable_memory
    self._api = None
    self._net_api = None
```

### node_allocatable_memory

```python
node_allocatable_memory()
```

Return minimum allocatable RAM in bytes across Ready nodes, 0 if unknown.

Source code in `src/resoluto/sandbox/runtime/k8s.py`

```python
async def node_allocatable_memory(self) -> int:
    """Return minimum allocatable RAM in bytes across Ready nodes, 0 if unknown."""
    return await self._get_node_allocatable_ram()
```

### ensure_run_owner

```python
ensure_run_owner(run_id)
```

Create-or-get the per-run owner ConfigMap; return (name, uid).

Source code in `src/resoluto/sandbox/runtime/k8s.py`

```python
async def ensure_run_owner(self, run_id: str) -> tuple[str, str]:
    """Create-or-get the per-run owner ConfigMap; return (name, uid)."""
    from kubernetes_asyncio.client.exceptions import ApiException

    api = await self._client()
    name = f"run-owner-{_dns_safe(run_id)}"
    body = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": name,
            "namespace": self._ns,
            "labels": {"resoluto.run_id": run_id[:63]},
        },
    }
    try:
        cm = await api.create_namespaced_config_map(namespace=self._ns, body=body)
        return (name, cm.metadata.uid)
    except ApiException as exc:
        if exc.status == 409:
            cm = await api.read_namespaced_config_map(name=name, namespace=self._ns)
            return (name, cm.metadata.uid)
        raise
```

### delete_run_owner

```python
delete_run_owner(run_id)
```

Delete the per-run owner ConfigMap, triggering k8s cascade GC (404-safe).

Source code in `src/resoluto/sandbox/runtime/k8s.py`

```python
async def delete_run_owner(self, run_id: str) -> None:
    """Delete the per-run owner ConfigMap, triggering k8s cascade GC (404-safe)."""
    from kubernetes_asyncio.client.exceptions import ApiException

    api = await self._client()
    name = f"run-owner-{_dns_safe(run_id)}"
    try:
        await api.delete_namespaced_config_map(name=name, namespace=self._ns)
    except ApiException as exc:
        if exc.status != 404:
            raise
```

### reap_stale_run_owners

```python
reap_stale_run_owners(keep_run_id, max_age_s=7200.0)
```

Delete run-owner ConfigMaps older than max_age_s and not keep_run_id; return count.

Source code in `src/resoluto/sandbox/runtime/k8s.py`

```python
async def reap_stale_run_owners(self, keep_run_id: str, max_age_s: float = 7200.0) -> int:
    """Delete run-owner ConfigMaps older than max_age_s and not keep_run_id; return count."""
    from datetime import UTC, datetime

    api = await self._client()
    cms = await api.list_namespaced_config_map(
        namespace=self._ns, label_selector="resoluto.run_id"
    )
    n = 0
    for cm in cms.items:
        rid = (cm.metadata.labels or {}).get("resoluto.run_id", "")
        if not rid or rid == keep_run_id[:63]:
            continue
        created = cm.metadata.creation_timestamp
        if created is not None and (datetime.now(UTC) - created).total_seconds() < max_age_s:
            continue
        await self.delete_run_owner(rid)
        n += 1
    return n
```

### count_active_pods

```python
count_active_pods(kind=None)
```

Count non-terminal sandbox pods in the namespace, optionally filtered by resoluto.kind.

Source code in `src/resoluto/sandbox/runtime/k8s.py`

```python
async def count_active_pods(self, kind: str | None = None) -> int:
    """Count non-terminal sandbox pods in the namespace, optionally filtered by resoluto.kind."""
    api = await self._client()
    label_selector = "resoluto_sandbox=true"
    if kind is not None:
        label_selector += f",resoluto.kind={kind}"
    pods = await api.list_namespaced_pod(namespace=self._ns, label_selector=label_selector)
    terminal = {"Succeeded", "Failed"}
    return sum(1 for pod in pods.items if (pod.status.phase or "") not in terminal)
```

## resoluto.sandbox.runtime.kata_nerdctl.KataNerdctlSandboxRuntime

```python
KataNerdctlSandboxRuntime(
    *,
    address,
    namespace,
    conduit_host_dir,
    conduit_mount="/conduit",
    runtime="io.containerd.kata.v2",
    cni_path=None,
    cni_netconfpath=None,
    network="bridge",
    nerdctl="nerdctl",
    sudo=False,
    egress_domains_file=None,
    dind_graph_dir="/var/lib/resoluto-local/dind-graph",
)
```

Bases: `SandboxRuntime`

Launches each sandbox as a Kata microVM via nerdctl against a dedicated containerd.

Source code in `src/resoluto/sandbox/runtime/kata_nerdctl.py`

```python
def __init__(
    self,
    *,
    address: str,
    namespace: str,
    conduit_host_dir: str,
    conduit_mount: str = "/conduit",
    runtime: str = "io.containerd.kata.v2",
    cni_path: str | None = None,
    cni_netconfpath: str | None = None,
    network: str = "bridge",
    nerdctl: str = "nerdctl",
    sudo: bool = False,
    egress_domains_file: str | None = None,
    dind_graph_dir: str = "/var/lib/resoluto-local/dind-graph",
) -> None:
    check_runtime_class_guard(runtime)
    self._address = address
    self._namespace = namespace
    self._conduit_host_dir = conduit_host_dir
    self._conduit_mount = conduit_mount
    self._runtime = runtime
    self._cni_path = cni_path
    self._cni_netconfpath = cni_netconfpath
    self._network = network
    self._nerdctl = nerdctl
    self._sudo = sudo
    # the live SNI allowlist file the persistent egress proxy reads (set per-run, see apply_egress)
    self._egress_domains_file = egress_domains_file
    # The active egress allowlist for THIS run, set by apply_egress. None = never applied (use the
    # configured network); [] = deny-all (launch with --network none — no NIC, no host firewall);
    # non-empty = allowlist (needs the bridge + SNI proxy).
    self._active_egress: list[str] | None = None
    # Base dir (on real DISK, never /run tmpfs) for a block-backed dind graph: each dind step
    # binds its own subdir at /var/lib/docker so image layers live on disk, keeping RAM free.
    self._dind_graph_dir = dind_graph_dir
    self._graph_dirs: dict[str, str] = {}  # container id → its host graph dir (for cleanup)
```

### apply_egress

```python
apply_egress(domains)
```

Set THIS run's SNI egress allowlist. Deny-all (empty/None) provisions NOTHING host-side — the guest launches with `--network none` (see `launch`), so there is no proxy to feed and no domains file to write. A non-empty allowlist writes the proxy's live domains file (per-run, no re-provision). Idempotent.

Source code in `src/resoluto/sandbox/runtime/kata_nerdctl.py`

```python
async def apply_egress(self, domains: "list[str] | None") -> None:
    """Set THIS run's SNI egress allowlist. Deny-all (empty/None) provisions NOTHING host-side —
    the guest launches with `--network none` (see `launch`), so there is no proxy to feed and no
    domains file to write. A non-empty allowlist writes the proxy's live domains file (per-run,
    no re-provision). Idempotent."""
    self._active_egress = [d.strip() for d in (domains or []) if d.strip()]
    if not self._egress_domains_file:
        return
    if self._active_egress:
        with open(self._egress_domains_file, "w", encoding="utf-8") as f:
            f.write(",".join(self._active_egress))
    elif os.path.exists(self._egress_domains_file):
        # Reset a stale allowlist back to deny; never CREATE the file for deny-all (no NIC needs it).
        with open(self._egress_domains_file, "w", encoding="utf-8") as f:
            f.write("")
```

### clear_egress

```python
clear_egress()
```

Reset this run's egress allowlist to deny-all (write the domains file empty).

Source code in `src/resoluto/sandbox/runtime/kata_nerdctl.py`

```python
async def clear_egress(self) -> None:
    """Reset this run's egress allowlist to deny-all (write the domains file empty)."""
    await self.apply_egress([])
```

### from_env

```python
from_env(*, conduit_host_dir, conduit_mount='/conduit')
```

Builds an instance from the RESOLUTO_LOCAL\_\* environment knobs.

Source code in `src/resoluto/sandbox/runtime/kata_nerdctl.py`

```python
@classmethod
def from_env(
    cls, *, conduit_host_dir: str, conduit_mount: str = "/conduit"
) -> "KataNerdctlSandboxRuntime":
    """Builds an instance from the RESOLUTO_LOCAL_* environment knobs."""
    return cls(
        address=os.environ.get(
            "RESOLUTO_LOCAL_CONTAINERD_ADDRESS",
            "/run/resoluto-local/containerd/containerd.sock",
        ),
        namespace=os.environ.get("RESOLUTO_LOCAL_CONTAINERD_NAMESPACE", "resoluto-local"),
        conduit_host_dir=conduit_host_dir,
        conduit_mount=conduit_mount,
        runtime=os.environ.get("RESOLUTO_LOCAL_KATA_RUNTIME", "io.containerd.kata.v2"),
        cni_path=os.environ.get("RESOLUTO_LOCAL_CNI_PATH", "/opt/resoluto-local/libexec/cni"),
        cni_netconfpath=os.environ.get(
            "RESOLUTO_LOCAL_CNI_NETCONFPATH", "/etc/resoluto-local/cni/net.d"
        ),
        network=os.environ.get("RESOLUTO_LOCAL_NETWORK", "resoluto-local"),
        nerdctl=os.environ.get("RESOLUTO_LOCAL_NERDCTL", "/opt/resoluto-local/bin/nerdctl"),
        sudo=_resolve_sudo(),
        egress_domains_file=os.environ.get(
            "RESOLUTO_LOCAL_EGRESS_DOMAINS_FILE", "/run/resoluto-local/egress-domains"
        ),
        dind_graph_dir=os.environ.get(
            "RESOLUTO_LOCAL_DIND_GRAPH_DIR", "/var/lib/resoluto-local/dind-graph"
        ),
    )
```

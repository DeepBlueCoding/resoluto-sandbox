"""K8sSandboxRuntime — the first concrete `SandboxRuntime` backend.

Maps launch/status/destroy/sweep onto Pods with `runtimeClassName: kata`, a
hardened securityContext, labels for sweep, and an optional
`activeDeadlineSeconds` (only when the spec sets one — orphan protection is the
label-based sweep, not a per-pod self-destruct). Platform deps (kubernetes_asyncio)
import lazily so the core package stays dependency-light.

dind lanes run privileged (GUEST-scoped under Kata via privileged_without_host_
devices — host stays unprivileged) with an emptyDir docker graph; plain lanes get
the full restricted profile (runAsNonRoot, drop ALL caps, no privilege escalation).
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field

from resoluto_sandbox.contracts import (
    SandboxHandle,
    SandboxLaunchSpec,
    SandboxRuntime,
    SandboxStatus,
    check_runtime_class_guard,
    parse_k8s_memory,
)

logger = logging.getLogger(__name__)

# Shared, canonical parser (lives in contracts so the pool budget and this runtime's
# pod-memory accounting use ONE implementation). Aliased to keep existing call sites.
_parse_k8s_memory = parse_k8s_memory


_PHASE_MAP = {
    "Pending": "pending",
    "Running": "running",
    "Succeeded": "succeeded",
    "Failed": "failed",
    "Unknown": "unknown",
}

_IMDS_CIDR = "169.254.169.254/32"


@dataclass(frozen=True)
class EgressConfig:
    """Allowlist CIDRs for the lane pod's egress NetworkPolicy.

    All fields MUST be CIDR notation (e.g. "1.2.3.4/32"). k8s NetworkPolicy
    ipBlock does not support FQDNs — the caller must resolve hostnames to IPs
    before constructing this object.

    store_cidr: CIDR for the object store endpoint.
    llm_cidr:   CIDR for the LLM provider API (e.g. api.anthropic.com).
    git_cidrs:  CIDRs for git hosts (default empty — no git egress allowed).
    """

    store_cidr: str
    llm_cidr: str
    git_cidrs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        for cidr in [self.store_cidr, self.llm_cidr, *self.git_cidrs]:
            if "/" not in cidr:
                raise ValueError(
                    f"EgressConfig: {cidr!r} is not a CIDR (missing '/'); "
                    "k8s NetworkPolicy ipBlock requires CIDR notation"
                )


def _no_local_kubeconfig_errors() -> tuple[type[BaseException], ...]:
    """Exceptions that mean 'no usable local kube-config' → fall back to in-cluster."""
    from kubernetes_asyncio.config.config_exception import ConfigException

    return (ConfigException, FileNotFoundError)


def _dns_safe(s: str) -> str:
    out = "".join(c if (c.isalnum() or c == "-") else "-" for c in s.lower())
    return out.strip("-")[:40] or "x"


class K8sSandboxRuntime(SandboxRuntime):
    def __init__(
        self,
        *,
        namespace: str = "resoluto-sandboxes",
        kubeconfig: str | None = None,
        context: str | None = None,
        image_pull_policy: str = "IfNotPresent",
        egress: EgressConfig | None = None,
        node_allocatable_memory: str | None = None,
    ) -> None:
        self._ns = namespace
        self._kubeconfig = kubeconfig
        # The kube CONTEXT this runtime targets. PIN it — never follow the ambient
        # current-context, which can wander to an unrelated (even production) cluster
        # and launch adversarial lane pods there. None = current-context (logged loud).
        self._context = context
        self._ipp = image_pull_policy
        self._egress = egress
        # node_allocatable_memory: explicit bytes-string override for the dind tmpfs
        # preflight (bypasses node API query — used in tests and offline envs).
        # Falls back to RESOLUTO_NODE_ALLOCATABLE_MEMORY env var, then the k8s API.
        self._node_allocatable_memory = node_allocatable_memory
        self._api = None  # lazy CoreV1Api
        self._net_api = None  # lazy NetworkingV1Api

    async def _client(self):
        if self._api is None:
            from kubernetes_asyncio import client, config

            in_cluster = False
            try:
                await config.load_kube_config(config_file=self._kubeconfig, context=self._context)
            except _no_local_kubeconfig_errors():
                # No usable local kube-config (missing file / empty context) → assume
                # we're running inside the cluster. Any OTHER error propagates.
                config.load_incluster_config()
                in_cluster = True

            if not in_cluster and self._context is None and os.environ.get(
                "RESOLUTO_SANDBOX_ALLOW_AMBIENT_CONTEXT"
            ) != "1":
                raise RuntimeError(
                    "refusing to launch lane pods on the ambient kube-context — set "
                    "RESOLUTO_SANDBOX_KUBECONTEXT, or RESOLUTO_SANDBOX_ALLOW_AMBIENT_CONTEXT=1 "
                    "to override"
                )

            self._api = client.CoreV1Api()
            host = self._api.api_client.configuration.host
            if in_cluster:
                logger.info("[k8s-runtime] targeting in-cluster API at %s (ns=%s)", host, self._ns)
            elif self._context:
                logger.info("[k8s-runtime] PINNED to kube-context %r → %s (ns=%s)", self._context, host, self._ns)
            else:
                logger.warning(
                    "[k8s-runtime] no kube-context pinned — using the AMBIENT current-context → %s "
                    "(RESOLUTO_SANDBOX_ALLOW_AMBIENT_CONTEXT=1). An unpinned context can launch lane "
                    "pods on the wrong (even production) cluster.", host,
                )
            await self._ensure_namespace()
        return self._api

    async def _networking_client(self):
        if self._net_api is None:
            from kubernetes_asyncio import client

            self._net_api = client.NetworkingV1Api(api_client=self._api.api_client)
        return self._net_api

    async def _ensure_namespace(self) -> None:
        from kubernetes_asyncio.client.exceptions import ApiException

        try:
            await self._api.create_namespace(
                body={"metadata": {"name": self._ns, "labels": {"resoluto.sandbox": "true"}}}
            )
        except ApiException as exc:
            if exc.status != 409:  # already exists
                raise

        quota = self._quota_manifest()
        try:
            await self._api.create_namespaced_resource_quota(namespace=self._ns, body=quota)
        except ApiException as exc:
            if exc.status == 409:
                await self._api.patch_namespaced_resource_quota(
                    name="resoluto-sandbox-quota", namespace=self._ns, body=quota
                )
            else:
                raise

        lr = self._limit_range_manifest()
        try:
            await self._api.create_namespaced_limit_range(namespace=self._ns, body=lr)
        except ApiException as exc:
            if exc.status == 409:
                await self._api.patch_namespaced_limit_range(
                    name="resoluto-sandbox-limits", namespace=self._ns, body=lr
                )
            else:
                raise

    def _quota_manifest(self) -> dict:
        max_pods = os.environ.get("RESOLUTO_SANDBOX_MAX_PODS", "20")
        max_memory = os.environ.get("RESOLUTO_SANDBOX_MAX_MEMORY", "96Gi")
        return {
            "apiVersion": "v1",
            "kind": "ResourceQuota",
            "metadata": {"name": "resoluto-sandbox-quota", "namespace": self._ns},
            "spec": {"hard": {"pods": max_pods, "limits.memory": max_memory}},
        }

    def _limit_range_manifest(self) -> dict:
        pod_max_memory = os.environ.get("RESOLUTO_SANDBOX_POD_MAX_MEMORY", "24Gi")
        pod_max_cpu = os.environ.get("RESOLUTO_SANDBOX_POD_MAX_CPU", "4")
        return {
            "apiVersion": "v1",
            "kind": "LimitRange",
            "metadata": {"name": "resoluto-sandbox-limits", "namespace": self._ns},
            "spec": {
                "limits": [{"type": "Pod", "max": {"memory": pod_max_memory, "cpu": pod_max_cpu}}]
            },
        }

    def _security_context(self, spec: SandboxLaunchSpec) -> dict:
        if spec.flavor == "dind":
            # privileged is GUEST-scoped under Kata; host pod is not host-privileged.
            # runAsUser 0 lets the entrypoint start the inner dockerd, then it drops to
            # the lane user (uid 1000) for the workload itself.
            return {"privileged": spec.privileged, "runAsUser": 0}
        return {
            "privileged": False,
            "allowPrivilegeEscalation": False,
            "runAsNonRoot": True,
            "capabilities": {"drop": ["ALL"]},
            "seccompProfile": {"type": "RuntimeDefault"},
        }

    def _manifest(
        self,
        spec: SandboxLaunchSpec,
        name: str,
        *,
        owner_name: str | None = None,
        owner_uid: str | None = None,
    ) -> dict:
        env = [{"name": k, "value": v} for k, v in spec.env.items()]
        # store wiring the sandbox self-reports through (object store + write-only token)
        env.append({"name": "RESOLUTO_STORE_PREFIX", "value": spec.store_prefix})
        if spec.store_write_token:
            env.append({"name": "RESOLUTO_STORE_WRITE_TOKEN", "value": spec.store_write_token})

        resource_qty = {
            "cpu": spec.cpu,
            "memory": spec.memory,
            "ephemeral-storage": spec.ephemeral_storage,
        }
        container: dict = {
            "name": "lane",
            "image": spec.image,
            "imagePullPolicy": self._ipp,
            "securityContext": self._security_context(spec),
            "env": env,
            # Honest requests == limits: the pod reserves what it will use, so the
            # kube-scheduler (and any external quota layer like Kueue) right-sizes it
            # correctly rather than over- or under-reserving. The dind tmpfs graph is a
            # medium:Memory emptyDir already counted WITHIN spec.memory (not added); the
            # block/virtio-blk graph is off-RAM and correctly not requested here.
            "resources": {"requests": dict(resource_qty), "limits": dict(resource_qty)},
        }
        if spec.command is not None:
            container["command"] = spec.command
        if spec.args is not None:
            container["args"] = spec.args

        volumes: list[dict] = []
        if spec.flavor == "dind":
            container.setdefault("volumeMounts", []).append(
                {"name": "docker-graph", "mountPath": "/var/lib/docker"}
            )
            if spec.graph_backend == "block":
                # Kata maps emptyDir without medium to a virtio-blk block device inside the
                # guest. The lane-entrypoint formats it ext4 and remounts before dockerd starts.
                # overlay2 on ext4/virtio-blk is proven (spike #1); no RAM tax (not counted
                # against pod memory unlike the tmpfs path).
                volumes.append(
                    {"name": "docker-graph",
                     "emptyDir": {"sizeLimit": spec.docker_graph_block_size}}
                )
            else:
                # Default tmpfs path: RAM-backed (medium: Memory) — overlay2 proven on tmpfs.
                # The size counts against the pod's memory; the image bytes must fit.
                # On Kata the virtiofs rootfs does NOT work: vfs exhausts host-side fd handles
                # and overlay2/fuse-overlayfs fail — tmpfs is the only non-virtiofs fallback.
                volumes.append(
                    {"name": "docker-graph",
                     "emptyDir": {"medium": "Memory", "sizeLimit": spec.docker_graph_size}}
                )

        pod_spec: dict = {
            "runtimeClassName": spec.runtime_class or None,
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "containers": [container],
            "volumes": volumes,
        }
        if spec.deadline_seconds is not None:
            pod_spec["activeDeadlineSeconds"] = spec.deadline_seconds
        # Stamp opaque caller-supplied scheduling gates VERBATIM (the seam an external
        # admitter like Kueue gates through). Empty → no gates → normal scheduling. The
        # substrate never constructs, names, or removes a gate; it only relays what the
        # caller put on the spec, so it stays Kueue-agnostic.
        if spec.scheduling_gates:
            pod_spec["schedulingGates"] = [{"name": g} for g in spec.scheduling_gates]

        # All sandbox pods carry resoluto.sandbox=true for deployment-wide counting.
        pod_labels = {"resoluto.sandbox": "true", **dict(spec.labels)}
        metadata: dict = {"name": name, "namespace": self._ns, "labels": pod_labels}
        if spec.annotations:
            metadata["annotations"] = dict(spec.annotations)
        if owner_name and owner_uid:
            metadata["ownerReferences"] = [{
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "name": owner_name,
                "uid": owner_uid,
                "blockOwnerDeletion": True,
            }]

        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": metadata,
            "spec": pod_spec,
        }

    def _network_policy(
        self,
        spec: SandboxLaunchSpec,
        pod_name: str,
        pod_uid: str,
        *,
        owner_name: str | None = None,
        owner_uid: str | None = None,
    ) -> dict:
        """Build the NetworkPolicy manifest for a lane pod.

        Creates a default-deny egress policy that allows only:
          - store endpoint on TCP/443
          - LLM provider on TCP/443
          - each git host on TCP/443
          - kube-dns on UDP/53 (broad CIDR; IMDS always excepted)

        Every ipBlock rule includes except=[_IMDS_CIDR] to block the cloud
        metadata endpoint regardless of the allowed CIDR range.

        When owner_name/owner_uid are provided the ownerReference points to the
        per-run ConfigMap (so GC from ConfigMap deletion cascades here too);
        otherwise falls back to the pod as the owner.
        """
        assert self._egress is not None

        def _tcp443_rule(cidr: str) -> dict:
            return {
                "ports": [{"port": 443, "protocol": "TCP"}],
                "to": [{"ipBlock": {"cidr": cidr, "except": [_IMDS_CIDR]}}],
            }

        egress_rules = [
            _tcp443_rule(self._egress.store_cidr),
            _tcp443_rule(self._egress.llm_cidr),
            *[_tcp443_rule(cidr) for cidr in self._egress.git_cidrs],
            {
                "ports": [{"port": 53, "protocol": "UDP"}],
                "to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": [_IMDS_CIDR]}}],
            },
        ]

        if owner_name and owner_uid:
            owner_ref = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "name": owner_name,
                "uid": owner_uid,
                "blockOwnerDeletion": True,
            }
        else:
            owner_ref = {
                "apiVersion": "v1",
                "kind": "Pod",
                "name": pod_name,
                "uid": pod_uid,
                "blockOwnerDeletion": True,
            }

        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {
                "name": f"np-{pod_name}",
                "namespace": self._ns,
                "ownerReferences": [owner_ref],
            },
            "spec": {
                "podSelector": {"matchLabels": dict(spec.labels)},
                "policyTypes": ["Egress"],
                "egress": egress_rules,
            },
        }

    async def node_allocatable_memory(self) -> int:
        """Public: minimum allocatable RAM (bytes) across Ready nodes, 0 if unknown.

        A neutral NODE-CAPACITY query — pure substrate. What a consumer does with it
        (e.g. derive an admission budget) is the consumer's policy, not the runtime's."""
        return await self._get_node_allocatable_ram()

    async def _get_node_allocatable_ram(self) -> int:
        """Return minimum allocatable RAM in bytes across all Ready nodes.

        Resolution order: constructor override → RESOLUTO_NODE_ALLOCATABLE_MEMORY
        env var → k8s node list API. Returns 0 when no schedulable nodes are found
        (caller skips preflight with a warning rather than rejecting).
        """
        if self._node_allocatable_memory is not None:
            return _parse_k8s_memory(self._node_allocatable_memory)
        env_val = os.environ.get("RESOLUTO_NODE_ALLOCATABLE_MEMORY")
        if env_val:
            return _parse_k8s_memory(env_val)
        api = await self._client()
        nodes = await api.list_node()
        if not nodes.items:
            return 0
        ram_values = []
        for node in nodes.items:
            conditions = node.status.conditions or []
            ready = any(c.type == "Ready" and c.status == "True" for c in conditions)
            if not ready:
                continue
            alloc = (node.status.allocatable or {})
            mem_str = alloc.get("memory") if isinstance(alloc, dict) else getattr(alloc, "memory", None)
            if mem_str:
                ram_values.append(_parse_k8s_memory(str(mem_str)))
        return min(ram_values) if ram_values else 0

    async def _preflight_memory(self, spec: SandboxLaunchSpec) -> None:
        """Refuse a dind+tmpfs launch that violates Kubernetes memory accounting.

        A `medium: Memory` emptyDir (tmpfs docker graph) is counted WITHIN the pod's
        memory cgroup limit — not additively on top of it. Therefore two independent
        constraints must hold:
          (a) graph_size < pod_memory — the graph must fit inside the pod cgroup, leaving
              headroom for dockerd, layer cache, and build processes.
          (b) pod_memory <= node_allocatable — the pod must be schedulable on a node.

        Raises RuntimeError with a distinct, actionable message for each failure mode so
        the operator knows whether to shrink the graph or shrink the pod.

        Args: spec — SandboxLaunchSpec with flavor='dind' and graph_backend='tmpfs'.
        """
        node_ram = await self._get_node_allocatable_ram()
        if node_ram == 0:
            logger.warning(
                "[k8s-runtime] node allocatable RAM unknown — skipping dind tmpfs preflight"
            )
            return
        pod_mem = _parse_k8s_memory(spec.memory)
        graph_mem = _parse_k8s_memory(spec.docker_graph_size)

        def _gib(b: int) -> str:
            return f"{b / (1024 ** 3):.1f}Gi"

        if graph_mem >= pod_mem:
            raise RuntimeError(
                f"dind tmpfs preflight: graph does not fit inside pod — "
                f"graph {spec.docker_graph_size} ({_gib(graph_mem)}) >= pod memory {spec.memory} ({_gib(pod_mem)}); "
                f"a medium:Memory emptyDir is counted within the pod cgroup so the graph must be "
                f"smaller than pod memory to leave room for dockerd and build processes. "
                f"Fix: lower RESOLUTO_LANE_DIND_GRAPH (currently {spec.docker_graph_size}) "
                f"to less than RESOLUTO_LANE_DIND_MEMORY (currently {spec.memory}), "
                f"or switch to block-backed docker graph with RESOLUTO_LANE_GRAPH_BACKEND=block."
            )

        if pod_mem > node_ram:
            over = pod_mem - node_ram
            raise RuntimeError(
                f"dind tmpfs preflight: pod does not fit on node — "
                f"pod memory {spec.memory} ({_gib(pod_mem)}) > node allocatable {_gib(node_ram)}, "
                f"over by {_gib(over)}. "
                f"Fix: lower RESOLUTO_LANE_DIND_MEMORY (currently {spec.memory}) "
                f"to at most {_gib(node_ram)}, or provision a larger node."
            )

    async def launch(self, spec: SandboxLaunchSpec) -> SandboxHandle:
        check_runtime_class_guard(spec.runtime_class)
        if spec.flavor == "dind" and spec.graph_backend == "tmpfs":
            await self._preflight_memory(spec)
        api = await self._client()
        rid = spec.labels.get("resoluto.run_id", "")
        nid = spec.labels.get("resoluto.node_id", "")
        # Append the unique uuid8 AFTER truncation — `_dns_safe` caps at 40 chars and
        # `sbx-`+a 36-char run_id already hits that, so embedding the suffix inside the
        # truncated string drops it and two pods sharing a run_id (lane + per-gate dind
        # pod) collide on `sbx-<run_id>` (409 AlreadyExists). Keep the suffix outside.
        name = f"{_dns_safe(f'sbx-{rid}-{nid}')}-{uuid.uuid4().hex[:8]}"

        owner_name: str | None = None
        owner_uid: str | None = None
        if rid:
            owner_name, owner_uid = await self.ensure_run_owner(rid)

        pod = await api.create_namespaced_pod(
            namespace=self._ns,
            body=self._manifest(spec, name, owner_name=owner_name, owner_uid=owner_uid),
        )
        if self._egress is not None:
            net_api = await self._networking_client()
            await net_api.create_namespaced_network_policy(
                namespace=self._ns,
                body=self._network_policy(
                    spec, name, pod.metadata.uid,
                    owner_name=owner_name, owner_uid=owner_uid,
                ),
            )
        return SandboxHandle(id=f"{self._ns}/{name}", labels=spec.labels)

    async def status(self, handle: SandboxHandle) -> SandboxStatus:
        from kubernetes_asyncio.client.exceptions import ApiException

        api = await self._client()
        ns, name = handle.id.split("/", 1)
        try:
            pod = await api.read_namespaced_pod(name=name, namespace=ns)
        except ApiException as exc:
            if exc.status == 404:
                return SandboxStatus(phase="unknown", reason="pod not found")
            raise
        phase = _PHASE_MAP.get(pod.status.phase or "Unknown", "unknown")
        reason = pod.status.reason or ""
        exit_code = None
        for cs in pod.status.container_statuses or []:
            term = getattr(cs.state, "terminated", None)
            if term is not None:
                exit_code = term.exit_code
                reason = reason or (term.reason or "")
            # Surface the WAITING reason too (ImagePullBackOff/ErrImagePull/
            # CreateContainerError/CrashLoopBackOff) — a pod stuck waiting on one of
            # these will never run, but its pod-phase is just "Pending", so the host
            # can't tell it apart from a legitimate resource hold without this.
            wait = getattr(cs.state, "waiting", None)
            if wait is not None and getattr(wait, "reason", None):
                reason = reason or wait.reason
        return SandboxStatus(phase=phase, reason=reason, exit_code=exit_code)

    async def destroy(self, handle: SandboxHandle) -> None:
        from kubernetes_asyncio.client.exceptions import ApiException

        api = await self._client()
        ns, name = handle.id.split("/", 1)
        try:
            await api.delete_namespaced_pod(
                name=name, namespace=ns, grace_period_seconds=0, propagation_policy="Background"
            )
        except ApiException as exc:
            if exc.status != 404:
                raise

    async def sweep(self, labels: dict[str, str]) -> int:
        api = await self._client()
        selector = ",".join(f"{k}={v}" for k, v in labels.items())
        pods = await api.list_namespaced_pod(namespace=self._ns, label_selector=selector)
        n = 0
        for pod in pods.items:
            await self.destroy(SandboxHandle(id=f"{self._ns}/{pod.metadata.name}"))
            n += 1
        return n

    async def logs(self, handle: SandboxHandle, *, tail: int = 200) -> str:
        from kubernetes_asyncio.client.exceptions import ApiException

        api = await self._client()
        ns, name = handle.id.split("/", 1)
        try:
            return await api.read_namespaced_pod_log(name=name, namespace=ns, tail_lines=tail)
        except ApiException as exc:
            return f"(logs unavailable: {exc.status})"

    async def ensure_run_owner(self, run_id: str) -> tuple[str, str]:
        """Create-or-get the per-run owner ConfigMap; return (name, uid).

        The ConfigMap is the k8s GC anchor: pods and NetworkPolicies that carry
        an ownerReference to it are cascade-deleted when it is deleted, even if
        the dispatcher process that spawned them is long dead.
        """
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

    async def delete_run_owner(self, run_id: str) -> None:
        """Delete the per-run owner ConfigMap, triggering k8s cascade GC.

        404-safe: a prior fast-path sweep may have already cleaned it up.
        """
        from kubernetes_asyncio.client.exceptions import ApiException

        api = await self._client()
        name = f"run-owner-{_dns_safe(run_id)}"
        try:
            await api.delete_namespaced_config_map(name=name, namespace=self._ns)
        except ApiException as exc:
            if exc.status != 404:
                raise

    async def reap_stale_run_owners(self, keep_run_id: str, max_age_s: float = 7200.0) -> int:
        """Delete run-owner ConfigMaps from runs that are surely done — older than max_age_s
        (a single task run is ~30-60 min) and NOT the current run — which cascade-GCs their
        leaked pods. This is the backstop for runs that died (kill -9) before teardown could
        delete their owner: the dispatcher is gone, so nothing else reaps them. Safe — only
        run-owner ConfigMaps are touched, never a concurrent active run's (recent) owner."""
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
                continue  # a recent owner may belong to a concurrently-running task
            await self.delete_run_owner(rid)
            n += 1
        return n

    async def count_active_pods(self, kind: str | None = None) -> int:
        """Count non-terminal pods in the sandbox namespace (deployment-wide).

        Used as the k8s-API-backed admission gate: all replicas see the same
        count, giving cross-replica coordination without Redis or etcd.

        kind: optional resoluto.kind label value to filter by (e.g. "lane" or
        "gate"). When None, counts all sandbox pods regardless of kind.
        """
        api = await self._client()
        label_selector = "resoluto.sandbox=true"
        if kind is not None:
            label_selector += f",resoluto.kind={kind}"
        pods = await api.list_namespaced_pod(
            namespace=self._ns, label_selector=label_selector
        )
        terminal = {"Succeeded", "Failed"}
        return sum(1 for pod in pods.items if (pod.status.phase or "") not in terminal)

    async def close(self) -> None:
        if self._api is not None:
            await self._api.api_client.close()

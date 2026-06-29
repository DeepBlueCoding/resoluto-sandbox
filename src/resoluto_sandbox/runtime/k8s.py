"""Concrete `SandboxRuntime` backend that maps launch/status/destroy/sweep onto Kata Pods."""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Literal

from resoluto_sandbox.contracts import (
    SandboxHandle,
    SandboxLaunchSpec,
    SandboxRuntime,
    SandboxStatus,
    check_runtime_class_guard,
    parse_quantity,
)

logger = logging.getLogger(__name__)

_parse_k8s_memory = parse_quantity


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
    """Allowlist for the lane pod's default-deny egress NetworkPolicy: the object store at store_cidr:store_port, plus public 443 and DNS. store_cidr must be CIDR notation."""

    store_cidr: str
    store_port: int = 443

    def __post_init__(self) -> None:
        if "/" not in self.store_cidr:
            raise ValueError(
                f"EgressConfig: store_cidr {self.store_cidr!r} is not a CIDR (missing '/'); "
                "k8s NetworkPolicy ipBlock requires CIDR notation"
            )

    @classmethod
    def from_store_env(cls, env: "dict[str, str] | None" = None) -> "EgressConfig | None":
        """Build the egress allowlist from RESOLUTO_STORE_ENDPOINT, honoring RESOLUTO_STORE_EGRESS_CIDR/PORT overrides; None when no store endpoint is set."""
        import socket
        from urllib.parse import urlparse

        e = env if env is not None else os.environ
        raw = (e.get("RESOLUTO_STORE_ENDPOINT") or "").strip()
        if not raw:
            return None
        u = urlparse(raw if "://" in raw else f"http://{raw}")
        endpoint_port = u.port or (443 if u.scheme == "https" else 80)

        override = (e.get("RESOLUTO_STORE_EGRESS_CIDR") or "").strip()
        if override:
            port = e.get("RESOLUTO_STORE_EGRESS_PORT")
            return cls(store_cidr=override, store_port=int(port) if port else endpoint_port)

        if not u.hostname:
            return None
        try:
            ip = socket.gethostbyname(u.hostname)
        except OSError:
            return None
        return cls(store_cidr=f"{ip}/32", store_port=endpoint_port)


def _no_local_kubeconfig_errors() -> tuple[type[BaseException], ...]:
    """Return exceptions that mean no usable local kube-config."""
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
        runtime_class: str = "kata",
        graph_backend: Literal["tmpfs", "block"] = "tmpfs",
        graph_block_size: str = "50Gi",
    ) -> None:
        self._ns = namespace
        self._kubeconfig = kubeconfig
        self._runtime_class = runtime_class
        self._graph_backend = graph_backend
        self._graph_block_size = graph_block_size
        self._context = context
        self._ipp = image_pull_policy
        self._egress = egress
        self._node_allocatable_memory = node_allocatable_memory
        self._api = None
        self._net_api = None

    async def _client(self):
        if self._api is None:
            from kubernetes_asyncio import client, config

            in_cluster = False
            try:
                await config.load_kube_config(config_file=self._kubeconfig, context=self._context)
            except _no_local_kubeconfig_errors():
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
            if exc.status != 409:
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
        env.append({"name": "RESOLUTO_STORE_PREFIX", "value": spec.store_prefix})
        if spec.store_write_token:
            env.append({"name": "RESOLUTO_STORE_WRITE_TOKEN", "value": spec.store_write_token})

        res = spec.resources
        cpu_cores = res.cpu_cores
        resource_qty = {
            "cpu": str(int(cpu_cores)) if cpu_cores == int(cpu_cores) else str(cpu_cores),
            "memory": str(res.memory_bytes),
        }
        if res.disk_bytes is not None:
            resource_qty["ephemeral-storage"] = str(res.disk_bytes)
        container: dict = {
            "name": "lane",
            "image": spec.image,
            "imagePullPolicy": self._ipp,
            "securityContext": self._security_context(spec),
            "env": env,
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
            if self._graph_backend == "block":
                volumes.append(
                    {"name": "docker-graph",
                     "emptyDir": {"sizeLimit": self._graph_block_size}}
                )
            else:
                graph_empty_dir: dict = {"medium": "Memory"}
                if res.dind_graph_bytes is not None:
                    graph_empty_dir["sizeLimit"] = str(res.dind_graph_bytes)
                volumes.append({"name": "docker-graph", "emptyDir": graph_empty_dir})

        pod_spec: dict = {
            "runtimeClassName": self._runtime_class or None,
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "containers": [container],
            "volumes": volumes,
        }
        if spec.deadline_seconds is not None:
            pod_spec["activeDeadlineSeconds"] = spec.deadline_seconds
        if spec.scheduling_gates:
            pod_spec["schedulingGates"] = [{"name": g} for g in spec.scheduling_gates]

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
        """Build the default-deny egress NetworkPolicy manifest for a lane pod: store on store_port, public 443, DNS; IMDS excepted on the broad rules."""
        assert self._egress is not None

        egress_rules = [
            {
                "ports": [{"port": self._egress.store_port, "protocol": "TCP"}],
                "to": [{"ipBlock": {"cidr": self._egress.store_cidr}}],
            },
            {
                "ports": [{"port": 443, "protocol": "TCP"}],
                "to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": [_IMDS_CIDR]}}],
            },
            {
                "ports": [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}],
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
        """Return minimum allocatable RAM in bytes across Ready nodes, 0 if unknown."""
        return await self._get_node_allocatable_ram()

    async def _get_node_allocatable_ram(self) -> int:
        """Return minimum allocatable RAM in bytes across all Ready nodes, or 0 when none found."""
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
        """Raise RuntimeError when a dind+tmpfs spec's graph or pod memory won't fit."""
        node_ram = await self._get_node_allocatable_ram()
        if node_ram == 0:
            logger.warning(
                "[k8s-runtime] node allocatable RAM unknown — skipping dind tmpfs preflight"
            )
            return
        pod_mem = spec.resources.memory_bytes
        graph_mem = spec.resources.dind_graph_bytes or 0

        def _gib(b: int) -> str:
            return f"{b / (1024 ** 3):.1f}Gi"

        if graph_mem >= pod_mem:
            raise RuntimeError(
                f"dind tmpfs preflight: graph does not fit inside pod — "
                f"graph {_gib(graph_mem)} >= pod memory {_gib(pod_mem)}; "
                f"a medium:Memory emptyDir is counted within the pod cgroup so the graph must be "
                f"smaller than pod memory to leave room for dockerd and build processes. "
                f"Fix: lower RESOLUTO_LANE_DIND_GRAPH to less than RESOLUTO_LANE_DIND_MEMORY, "
                f"or switch to a block-backed docker graph."
            )

        if pod_mem > node_ram:
            over = pod_mem - node_ram
            raise RuntimeError(
                f"dind tmpfs preflight: pod does not fit on node — "
                f"pod memory {_gib(pod_mem)} > node allocatable {_gib(node_ram)}, "
                f"over by {_gib(over)}. "
                f"Fix: lower RESOLUTO_LANE_DIND_MEMORY to at most {_gib(node_ram)}, or provision a larger node."
            )

    async def launch(self, spec: SandboxLaunchSpec) -> SandboxHandle:
        check_runtime_class_guard(self._runtime_class)
        if spec.flavor == "dind" and self._graph_backend == "tmpfs":
            await self._preflight_memory(spec)
        api = await self._client()
        rid = spec.labels.get("resoluto.run_id", "")
        nid = spec.labels.get("resoluto.node_id", "")
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

    async def count_active_pods(self, kind: str | None = None) -> int:
        """Count non-terminal sandbox pods in the namespace, optionally filtered by resoluto.kind."""
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

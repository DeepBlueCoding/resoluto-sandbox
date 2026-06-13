"""K8sSandboxRuntime — the first concrete `SandboxRuntime` backend.

Maps launch/status/destroy/sweep onto Pods with `runtimeClassName: kata` (proven
by spike #1), the §12 hardened securityContext, labels for sweep, and an optional
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
)

logger = logging.getLogger(__name__)

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
    ) -> None:
        self._ns = namespace
        self._kubeconfig = kubeconfig
        # The kube CONTEXT this runtime targets. PIN it — never follow the ambient
        # current-context, which can wander to an unrelated (even production) cluster
        # and launch adversarial lane pods there. None = current-context (logged loud).
        self._context = context
        self._ipp = image_pull_policy
        self._egress = egress
        self._api = None  # lazy CoreV1Api
        self._net_api = None  # lazy NetworkingV1Api

    async def _client(self):
        if self._api is None:
            from kubernetes_asyncio import client, config

            in_cluster = False
            try:
                await config.load_kube_config(config_file=self._kubeconfig, context=self._context)
            except Exception:
                config.load_incluster_config()
                in_cluster = True
            self._api = client.CoreV1Api()
            host = self._api.api_client.configuration.host
            if in_cluster:
                logger.info("[k8s-runtime] targeting in-cluster API at %s (ns=%s)", host, self._ns)
            elif self._context:
                logger.info("[k8s-runtime] PINNED to kube-context %r → %s (ns=%s)", self._context, host, self._ns)
            else:
                logger.warning(
                    "[k8s-runtime] no kube-context pinned — using the AMBIENT current-context → %s. "
                    "Set RESOLUTO_SANDBOX_KUBECONTEXT to pin the target cluster; an unpinned "
                    "context can launch lane pods on the wrong (even production) cluster.", host,
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

        container: dict = {
            "name": "lane",
            "image": spec.image,
            "imagePullPolicy": self._ipp,
            "securityContext": self._security_context(spec),
            "env": env,
            "resources": {
                "limits": {
                    "cpu": spec.cpu,
                    "memory": spec.memory,
                    "ephemeral-storage": spec.ephemeral_storage,
                }
            },
        }
        if spec.command is not None:
            container["command"] = spec.command
        if spec.args is not None:
            container["args"] = spec.args

        volumes: list[dict] = []
        if spec.flavor == "dind":
            # The inner dockerd graph MUST be a RAM-backed tmpfs (medium: Memory),
            # not the default emptyDir: on Kata that default lands on the guest's
            # virtiofs (FUSE) rootfs, where kernel overlay2 is unsupported and vfs
            # exhausts virtiofsd's host-side file handles ("too many open files").
            # tmpfs is a real in-guest fs → overlay2 works, no FUSE. The size counts
            # against the pod's memory; the image bytes must fit (scale note §14).
            container.setdefault("volumeMounts", []).append(
                {"name": "docker-graph", "mountPath": "/var/lib/docker"}
            )
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

        # All sandbox pods carry resoluto.sandbox=true for deployment-wide counting.
        pod_labels = {"resoluto.sandbox": "true", **dict(spec.labels)}
        metadata: dict = {"name": name, "namespace": self._ns, "labels": pod_labels}
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

    async def launch(self, spec: SandboxLaunchSpec) -> SandboxHandle:
        api = await self._client()
        rid = spec.labels.get("resoluto.run_id", "")
        nid = spec.labels.get("resoluto.node_id", "")
        name = _dns_safe(f"sbx-{rid}-{nid}-{uuid.uuid4().hex[:8]}")

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

    async def count_active_pods(self) -> int:
        """Count non-terminal pods in the sandbox namespace (deployment-wide).

        Used as the k8s-API-backed admission gate: all replicas see the same
        count, giving cross-replica coordination without Redis or etcd.
        """
        api = await self._client()
        pods = await api.list_namespaced_pod(
            namespace=self._ns, label_selector="resoluto.sandbox=true"
        )
        terminal = {"Succeeded", "Failed"}
        return sum(1 for pod in pods.items if (pod.status.phase or "") not in terminal)

    async def close(self) -> None:
        if self._api is not None:
            await self._api.api_client.close()

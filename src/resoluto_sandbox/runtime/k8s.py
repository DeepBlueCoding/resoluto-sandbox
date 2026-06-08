"""K8sSandboxRuntime — the first concrete `SandboxRuntime` backend.

Maps launch/status/destroy/sweep onto Pods with `runtimeClassName: kata` (proven
by spike #1), the §12 hardened securityContext, labels for sweep, and
`activeDeadlineSeconds` as the substrate cap. Platform deps (kubernetes_asyncio)
import lazily so the core package stays dependency-light.

dind lanes run privileged (GUEST-scoped under Kata via privileged_without_host_
devices — host stays unprivileged) with an emptyDir docker graph; plain lanes get
the full restricted profile (runAsNonRoot, drop ALL caps, no privilege escalation).
"""
from __future__ import annotations

import uuid

from resoluto_sandbox.contracts import (
    SandboxHandle,
    SandboxLaunchSpec,
    SandboxRuntime,
    SandboxStatus,
)

_PHASE_MAP = {
    "Pending": "pending",
    "Running": "running",
    "Succeeded": "succeeded",
    "Failed": "failed",
    "Unknown": "unknown",
}


def _dns_safe(s: str) -> str:
    out = "".join(c if (c.isalnum() or c == "-") else "-" for c in s.lower())
    return out.strip("-")[:40] or "x"


class K8sSandboxRuntime(SandboxRuntime):
    def __init__(
        self,
        *,
        namespace: str = "resoluto-sandboxes",
        kubeconfig: str | None = None,
        image_pull_policy: str = "IfNotPresent",
    ) -> None:
        self._ns = namespace
        self._kubeconfig = kubeconfig
        self._ipp = image_pull_policy
        self._api = None  # lazy CoreV1Api

    async def _client(self):
        if self._api is None:
            from kubernetes_asyncio import client, config

            try:
                await config.load_kube_config(config_file=self._kubeconfig)
            except Exception:
                config.load_incluster_config()
            self._api = client.CoreV1Api()
            await self._ensure_namespace()
        return self._api

    async def _ensure_namespace(self) -> None:
        from kubernetes_asyncio.client.exceptions import ApiException

        try:
            await self._api.create_namespace(
                body={"metadata": {"name": self._ns, "labels": {"resoluto.sandbox": "true"}}}
            )
        except ApiException as exc:
            if exc.status != 409:  # already exists
                raise

    def _security_context(self, spec: SandboxLaunchSpec) -> dict:
        if spec.flavor == "dind":
            # privileged is GUEST-scoped under Kata; host pod is not host-privileged.
            return {"privileged": spec.privileged}
        return {
            "privileged": False,
            "allowPrivilegeEscalation": False,
            "runAsNonRoot": True,
            "capabilities": {"drop": ["ALL"]},
            "seccompProfile": {"type": "RuntimeDefault"},
        }

    def _manifest(self, spec: SandboxLaunchSpec, name: str) -> dict:
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
            container.setdefault("volumeMounts", []).append(
                {"name": "docker-graph", "mountPath": "/var/lib/docker"}
            )
            volumes.append(
                {"name": "docker-graph", "emptyDir": {"sizeLimit": spec.ephemeral_storage}}
            )

        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name, "namespace": self._ns, "labels": dict(spec.labels)},
            "spec": {
                "runtimeClassName": spec.runtime_class or None,
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                "activeDeadlineSeconds": spec.deadline_seconds,
                "containers": [container],
                "volumes": volumes,
            },
        }

    async def launch(self, spec: SandboxLaunchSpec) -> SandboxHandle:
        api = await self._client()
        rid = spec.labels.get("resoluto.run_id", "")
        nid = spec.labels.get("resoluto.node_id", "")
        name = _dns_safe(f"sbx-{rid}-{nid}-{uuid.uuid4().hex[:8]}")
        await api.create_namespaced_pod(namespace=self._ns, body=self._manifest(spec, name))
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

    async def close(self) -> None:
        if self._api is not None:
            await self._api.api_client.close()

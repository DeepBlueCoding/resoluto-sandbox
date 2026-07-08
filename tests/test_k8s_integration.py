"""Integration tests against the LIVE k3s + Kata cluster (spike #1 env).

Run explicitly:  uv run pytest -m integration
Excluded by default (addopts -m 'not integration'). Requires ~/.kube/config
pointing at k3s and the `kata` RuntimeClass + the docker:27-dind image imported.
"""
import asyncio
import os
import platform

import pytest

from resoluto.sandbox import SandboxLaunchSpec
from resoluto.sandbox.runtime.k8s import K8sSandboxRuntime

IMAGE = "docker.io/library/docker:27-dind"
NS = "resoluto-itest"
# Pin the local k3s context — the runtime refuses the ambient context (which may be a remote
# AKS cluster) to avoid launching adversarial sandbox pods on the wrong cluster.
KUBECONTEXT = os.environ.get("RESOLUTO_SANDBOX_KUBECONTEXT", "default")


def _spec(run_id="itest", node_id="n1", args=None):
    return SandboxLaunchSpec(
        image=IMAGE,
        flavor="dind",  # avoids runAsNonRoot vs root-image conflict; privileged stays off
        privileged=False,
        runtime_class="kata",
        labels={"resoluto.run_id": run_id, "resoluto.node_id": node_id},
        store_prefix=f"run/{run_id}/nodes/{node_id}",
        deadline_seconds=300,
        command=["sh", "-c"],
        args=args or ["echo IT_HELLO; uname -r"],
    )


async def _wait_terminal(rt, handle, tries=80):
    st = None
    for _ in range(tries):
        st = await rt.status(handle)
        if st.terminal:
            return st
        await asyncio.sleep(3)
    return st


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kata_pod_lifecycle(dind_image):
    rt = K8sSandboxRuntime(namespace=NS, image_pull_policy="Never", context=KUBECONTEXT)
    handle = await rt.launch(_spec())
    try:
        st = await _wait_terminal(rt, handle)
        assert st is not None and st.phase == "succeeded", f"phase={st}"
        assert st.exit_code == 0
        logs = await rt.logs(handle)
        assert "IT_HELLO" in logs
        # the uname -r inside the guest must differ from the host kernel → real VM
        assert platform.release() not in logs, "guest kernel == host kernel (no VM boundary!)"
    finally:
        await rt.destroy(handle)
        await rt.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sweep_by_label(dind_image):
    rt = K8sSandboxRuntime(namespace=NS, image_pull_policy="Never", context=KUBECONTEXT)
    try:
        await rt.launch(_spec(run_id="sweepme", node_id="a", args=["sleep 120"]))
        await rt.launch(_spec(run_id="sweepme", node_id="b", args=["sleep 120"]))
        await asyncio.sleep(2)
        n = await rt.sweep({"resoluto.run_id": "sweepme"})
        assert n == 2
    finally:
        await rt.close()

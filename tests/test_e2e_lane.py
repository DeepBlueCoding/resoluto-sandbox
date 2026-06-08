"""§11.6 step 5 — the WHOLE loop on real infra, end to end.

A real Kata pod (runtimeClass=kata) runs the BAKED runner image as its ENTRYPOINT.
The runner — holding no orchestrator connection — self-reports redacted span/log
chunks + result.json to LIVE minio. Host-side `drive_node` (SandboxPool +
K8sSandboxRuntime + S3ObjectStore) tails that store, reconstructs the telemetry,
collects the result, and reaps the pod. This is the store-mediated loop that the
RES-236 wedge made impossible — proven against the substrate, not a fake.

Run:  uv run pytest -m integration tests/test_e2e_lane.py
Needs: live k3s+Kata, the resoluto-sandbox-runner:dev image imported into k3s
containerd, and spike-minio on the host (0.0.0.0:9100, minioadmin/minioadmin).
"""
import platform
import uuid

import pytest

from resoluto_sandbox import SandboxLaunchSpec, SandboxPool, drive_node
from resoluto_sandbox.objectstore.s3 import S3ObjectStore
from resoluto_sandbox.runtime.k8s import K8sSandboxRuntime

RUNNER_IMAGE = "docker.io/library/resoluto-sandbox-runner:dev"
NS = "resoluto-e2e"
HOST_ENDPOINT = "http://localhost:9100"       # host-side reader → minio
POD_ENDPOINT = "http://192.168.1.197:9100"    # in-pod runner → minio (k3s node IP)
MINIO_KEY = "minioadmin"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_kata_lane_store_mediated_loop():
    run_id = f"e2e{uuid.uuid4().hex[:8]}"
    node_id = "compile"
    bucket = "resoluto-e2e"
    prefix = f"run/{run_id}/nodes/{node_id}"

    store = S3ObjectStore(
        bucket, endpoint_url=HOST_ENDPOINT, region_name="us-east-1",
        aws_access_key_id=MINIO_KEY, aws_secret_access_key=MINIO_KEY,
    )
    await store.ensure_bucket()

    spec = SandboxLaunchSpec(
        image=RUNNER_IMAGE,
        flavor="plain",            # restricted: nonroot (image runs as 65532), drop ALL caps
        runtime_class="kata",      # still a real microVM
        labels={"resoluto.run_id": run_id, "resoluto.node_id": node_id},
        store_prefix=prefix,
        deadline_seconds=300,
        env={
            "RESOLUTO_STORE_KIND": "s3",
            "RESOLUTO_STORE_BUCKET": bucket,
            "RESOLUTO_STORE_ENDPOINT": POD_ENDPOINT,
            "RESOLUTO_STORE_REGION": "us-east-1",
            "AWS_ACCESS_KEY_ID": MINIO_KEY,
            "AWS_SECRET_ACCESS_KEY": MINIO_KEY,
            "RESOLUTO_RUN_ID": run_id,
            "RESOLUTO_NODE_ID": node_id,
            "RESOLUTO_WORKLOAD_ARGV": '["sh", "-c", "echo E2E_HELLO; uname -r"]',
        },
    )

    runtime = K8sSandboxRuntime(namespace=NS, image_pull_policy="Never")
    pool = SandboxPool(runtime, max_concurrent=1)
    seen = []
    try:
        result = await drive_node(
            pool, store, spec, on_event=seen.append,
            poll_interval_s=3.0, dead_after_s=240.0,
        )
    finally:
        await runtime.close()

    # the loop closed cleanly — pod reached terminal, result collected, reaped
    assert result["status"] == "success", result
    assert result["exit_code"] == 0
    assert result["observed_phase"] == "succeeded"
    assert pool.live_count == 0

    # the host reconstructed the in-pod telemetry tree from the store alone
    logs = [e.data["line"] for e in seen if e.event == "log" and e.kind == "log"]
    assert "E2E_HELLO" in logs
    assert any(e.kind == "node" and e.event == "open" for e in seen)
    assert any(e.kind == "node" and e.event == "close" and e.status == "success" for e in seen)
    # guest kernel != host kernel → the work really ran inside a Kata VM
    assert platform.release() not in logs, "guest kernel == host kernel (no VM boundary!)"

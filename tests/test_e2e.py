"""§11.6 step 5 — the WHOLE loop on real infra, end to end.

A real Kata pod (runtimeClass=kata) runs the BAKED runner image as its ENTRYPOINT.
The runner — holding no host connection — self-reports redacted span/log
chunks + result.json to LIVE minio. Host-side `drive_node` (admission-free —
K8sSandboxRuntime + S3Conduit) tails that store, reconstructs the telemetry,
collects the result, and reaps the pod. This is the store-mediated loop that the
RES-236 wedge made impossible — proven against the substrate, not a fake.

Run:  uv run pytest -m integration tests/test_e2e.py
Needs: live k3s+Kata, the resoluto-sandbox-runner:0.1.0 image imported into k3s
containerd, and spike-minio on the host (0.0.0.0:9100, minioadmin/minioadmin).
"""

import os
import platform
import subprocess
import uuid

import pytest

from resoluto.sandbox import (
    SandboxLaunchSpec,
    drive_node,
    fetch_outputs,
    put_dir,
)
from resoluto.sandbox.conduit.s3 import S3Conduit
from resoluto.sandbox.runtime.k8s import EgressConfig, K8sSandboxRuntime

# These drive a FRESH runner image whose in-guest egress canary is always-on (the trusted-local
# bypass was removed). They therefore require a CNI that actually ENFORCES the sandbox's egress
# NetworkPolicy. This dev box (k3s + Flannel; kube-router netpol non-functional for egress) does
# NOT, so the fail-closed canary correctly refuses — external egress is genuinely open here. The
# store-mediated host→pod→minio→reap loop itself is proven GREEN by scripts/store-backend-canary.py.
# Run these on an egress-enforcing cluster (Calico/Cilium).
pytestmark = pytest.mark.skip(
    reason="needs a CNI that enforces egress NetworkPolicy (this k3s+Flannel box does not)"
)

RUNNER_IMAGE = "docker.io/library/resoluto-sandbox-runner:0.1.0"
NS = "resoluto-e2e"
HOST_ENDPOINT = "http://localhost:9100"  # host-side reader → minio
POD_ENDPOINT = "http://192.168.1.197:9100"  # in-pod runner → minio (k3s node IP)
MINIO_KEY = "minioadmin"
KUBECONTEXT = os.environ.get(
    "RESOLUTO_SANDBOX_KUBECONTEXT", "default"
)  # pin local k3s, not ambient AKS


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_kata_sandbox_store_mediated_loop(runner_image):
    run_id = f"e2e{uuid.uuid4().hex[:8]}"
    node_id = "compile"
    bucket = "resoluto-e2e"
    prefix = f"run/{run_id}/nodes/{node_id}"

    store = S3Conduit(
        bucket,
        endpoint_url=HOST_ENDPOINT,
        region_name="us-east-1",
        aws_access_key_id=MINIO_KEY,
        aws_secret_access_key=MINIO_KEY,
    )
    await store.ensure_bucket()

    spec = SandboxLaunchSpec(
        image=RUNNER_IMAGE,
        flavor="plain",  # restricted: nonroot (image runs as 65532), drop ALL caps
        runtime_class="kata",  # still a real microVM
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

    runtime = K8sSandboxRuntime(
        namespace=NS,
        image_pull_policy="Never",
        context=KUBECONTEXT,
        egress=EgressConfig.from_store_env(),
    )
    seen = []
    try:
        result = await drive_node(
            runtime,
            store,
            spec,
            on_event=seen.append,
            poll_interval_s=3.0,
            dead_after_s=240.0,
        )
    finally:
        await runtime.close()

    # the loop closed cleanly — pod reached terminal, result collected, reaped
    assert result.status == "success", result
    assert result.exit_code == 0
    assert result.observed_phase == "succeeded"

    # the host reconstructed the in-pod telemetry tree from the store alone
    logs = [e.data["line"] for e in seen if e.event == "log" and e.kind == "log"]
    assert "E2E_HELLO" in logs
    assert any(e.kind == "node" and e.event == "open" for e in seen)
    assert any(e.kind == "node" and e.event == "close" and e.status == "success" for e in seen)
    # guest kernel != host kernel → the work really ran inside a Kata VM
    assert platform.release() not in logs, "guest kernel == host kernel (no VM boundary!)"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_repo_stages_in_and_diff_comes_back_out(tmp_path, runner_image):
    """A real git repo (incl. .git history) rides into the passive Kata pod as ONE
    store object, the sandbox reads it + emits a patched artifact, and the host fetches
    that artifact back — no git egress, no creds in guest, store as the only path."""
    run_id = f"e2e{uuid.uuid4().hex[:8]}"
    node_id = "edit"
    bucket = "resoluto-e2e"
    prefix = f"run/{run_id}/nodes/{node_id}"

    # a REAL git repo on the host (history lives in .git, which the tar carries)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("ORIGINAL\n")
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "init"], cwd=repo, check=True, env={**env, "PATH": "/usr/bin:/bin"}
    )

    store = S3Conduit(
        bucket,
        endpoint_url=HOST_ENDPOINT,
        region_name="us-east-1",
        aws_access_key_id=MINIO_KEY,
        aws_secret_access_key=MINIO_KEY,
    )
    await store.ensure_bucket()
    # HOST pushes the worktree as the sandbox's single input object
    await put_dir(store, prefix, str(repo))

    spec = SandboxLaunchSpec(
        image=RUNNER_IMAGE,
        flavor="plain",
        runtime_class="kata",
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
            "RESOLUTO_WORKSPACE_DIR": "/tmp/ws",  # writable by nonroot 65532
            "RESOLUTO_OUTPUT_PATHS": '["patched.md"]',
            # proves the repo (incl .git) was staged, then produces an artifact
            "RESOLUTO_WORKLOAD_ARGV": '["sh", "-c", "cat .git/HEAD; cat README.md; '
            'sed s/ORIGINAL/PATCHED/ README.md > patched.md"]',
        },
    )

    runtime = K8sSandboxRuntime(
        namespace=NS,
        image_pull_policy="Never",
        context=KUBECONTEXT,
        egress=EgressConfig.from_store_env(),
    )
    seen = []
    try:
        result = await drive_node(
            runtime,
            store,
            spec,
            on_event=seen.append,
            poll_interval_s=3.0,
            dead_after_s=240.0,
        )
    finally:
        await runtime.close()

    assert result.status == "success", result
    assert result.output_archive == f"{prefix}/outbox/output.tar.gz"

    logs = [e.data["line"] for e in seen if e.event == "log" and e.kind == "log"]
    assert any("ref: refs/heads/" in ln for ln in logs), "guest never saw .git → repo not staged"
    assert "ORIGINAL" in logs, "guest never read README → worktree not staged"

    # HOST fetches the artifact the adversarial guest produced (traversal-safe extract)
    dest = tmp_path / "out"
    fetched = await fetch_outputs(store, prefix, str(dest))
    assert fetched == [f"{prefix}/outbox/output.tar.gz"]
    assert (dest / "patched.md").read_text() == "PATCHED\n"

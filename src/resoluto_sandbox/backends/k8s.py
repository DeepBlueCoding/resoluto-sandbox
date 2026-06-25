"""Kubernetes/Kata pod backend."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import IO, Sequence
from uuid import uuid4

from resoluto_sandbox.backends.artifacts import _collect, read_result_json
from resoluto_sandbox.backends.base import Backend, RunResult
from resoluto_sandbox.contracts import Conduit
from resoluto_sandbox.deps import Deps


class K8sBackend(Backend):
    """Runs the program in a Kata pod via ``drive_node``.

    For k8s, ``RunResult.stdout`` carries the runner's MERGED stdout+stderr (the
    in-pod runner emits both as ``log`` span events), so ``RunResult.stderr`` is
    empty by design — the divergence from the local backend is intentional, not a
    dropped field.

    ``egress`` is an ``EgressConfig`` (import from ``resoluto_sandbox.runtime.k8s``)
    that applies a default-deny egress NetworkPolicy to the lane pod, allowing only
    the declared CIDRs on TCP/443 plus kube-dns on UDP/53. When ``None`` (default)
    the pod has unrestricted egress (Kata kernel isolation only).
    """

    def __init__(
        self,
        *,
        image: str | None = None,
        conduit: Conduit | None = None,
        egress: "EgressConfig | None" = None,
    ) -> None:
        self._image = image
        self._conduit = conduit
        self._egress = egress

    def run(
        self,
        argv: Sequence[str],
        *,
        workspace: str | None = None,
        stdin: str | bytes | None = None,
        env: dict[str, str] | None = None,
        output_paths: Sequence[str] | None = None,
        stream: IO[str] | None = None,
        deps: Deps | None = None,
    ) -> RunResult:
        if stdin is not None:
            raise NotImplementedError("stdin is not supported on backend='k8s'")
        if deps is not None:
            raise NotImplementedError("deps is not supported on backend='k8s' (bake them into the image)")
        if self._image is None:
            raise ValueError("backend='k8s' requires K8sBackend(image=...)")
        return asyncio.run(self._run_async(argv, workspace=workspace, env=env,
                                           output_paths=output_paths, stream=stream))

    async def _run_async(
        self,
        argv: Sequence[str],
        *,
        workspace: str | None,
        env: dict[str, str] | None,
        output_paths: Sequence[str] | None,
        stream: IO[str] | None,
    ) -> RunResult:
        """Launch a Kata pod via drive_node, stage workspace in, fetch artifacts out.

        Output artifacts are extracted into the provided ``workspace`` dir (in place),
        matching the local backend; the caller's workspace is mutated by collected outputs."""
        from resoluto_sandbox.contracts import SandboxLaunchSpec
        from resoluto_sandbox.driver import drive_node
        from resoluto_sandbox.runner_main import store_from_env
        from resoluto_sandbox.runtime.k8s import EgressConfig, K8sSandboxRuntime
        from resoluto_sandbox.staging import fetch_outputs, put_dir

        store = self._conduit if self._conduit is not None else store_from_env()

        run_id = "run-" + uuid4().hex[:8]
        node_id = "run"
        prefix = f"run/{run_id}/nodes/{node_id}/lane-0"

        runtime = K8sSandboxRuntime(
            namespace=os.environ.get("RESOLUTO_SANDBOX_NAMESPACE", "resoluto-sandboxes"),
            context=os.environ.get("RESOLUTO_SANDBOX_KUBECONTEXT") or None,
            image_pull_policy=os.environ.get("RESOLUTO_LANE_IMAGE_PULL_POLICY", "IfNotPresent"),
            egress=self._egress,
        )

        if workspace:
            await put_dir(store, prefix, workspace)

        pod_env: dict[str, str] = {
            **_store_env_for_pod(os.environ),
            **(env or {}),
            "RESOLUTO_STORE_PREFIX": prefix,
            "RESOLUTO_RUN_ID": run_id,
            "RESOLUTO_NODE_ID": node_id,
            "RESOLUTO_WORKLOAD_ARGV": json.dumps(list(argv)),
            "RESOLUTO_WORKSPACE_DIR": "/workspace",
        }
        if output_paths:
            pod_env["RESOLUTO_OUTPUT_PATHS"] = json.dumps(list(output_paths))

        spec = SandboxLaunchSpec(
            image=self._image,
            flavor="plain",
            runtime_class="kata",
            privileged=False,
            env=pod_env,
            args=["python", "-m", "resoluto_sandbox.runner_main"],
            store_prefix=prefix,
            labels={"resoluto.run_id": run_id, "resoluto.node_id": node_id},
        )

        out_lines: list[str] = []
        sink = stream if stream is not None else sys.stdout

        def on_event(ev) -> None:
            if ev.event == "log":
                line = str(ev.data.get("line") or "")
                if line:
                    rendered = line if line.endswith("\n") else line + "\n"
                    out_lines.append(rendered)
                    sink.write(rendered)
                    sink.flush()

        result = await drive_node(runtime, store, spec, on_event=on_event, dead_after_s=600.0)

        artifacts: list[str] = []
        node_result: dict | None = None
        if output_paths and workspace:
            await fetch_outputs(store, prefix, str(Path(workspace)))
            artifacts = _collect(Path(workspace), output_paths)
            node_result = read_result_json(Path(workspace))

        exit_code = result.exit_code if result.exit_code is not None else (0 if result.status == "success" else 1)
        return RunResult(
            exit_code=exit_code,
            stdout="".join(out_lines),
            stderr="",
            artifacts=artifacts,
            result=node_result,
            reason=(result.reason or result.observed_phase or ""),
        )


def _store_env_for_pod(environ: "os._Environ[str] | dict[str, str]") -> dict[str, str]:
    """Select the env the untrusted pod is allowed to inherit.

    Forwards RESOLUTO_STORE_* and RESOLUTO_TRUSTED_LOCAL. Host AWS_* creds are
    NOT forwarded unless the standalone k8s backend is explicitly trusted-local
    (dev only) — the pod should authenticate to the store via the prefix-scoped
    RESOLUTO_STORE_WRITE_TOKEN.
    """
    selected = {
        k: v
        for k, v in environ.items()
        if k.startswith("RESOLUTO_STORE_") or k == "RESOLUTO_TRUSTED_LOCAL"
    }
    if selected.get("RESOLUTO_STORE_WRITE_TOKEN"):
        return selected

    aws = {k: v for k, v in environ.items() if k.startswith("AWS_")}
    if not aws:
        return selected
    if not environ.get("RESOLUTO_TRUSTED_LOCAL"):
        raise RuntimeError(
            "backend='k8s' needs a scoped RESOLUTO_STORE_WRITE_TOKEN, or set "
            "RESOLUTO_TRUSTED_LOCAL=1 to forward host AWS creds (dev only)"
        )
    selected.update(aws)
    return selected

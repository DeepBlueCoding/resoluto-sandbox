"""Kubernetes/Kata pod backend."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import IO, Sequence
from uuid import uuid4

from resoluto_sandbox.backends.base import Backend, RunResult
from resoluto_sandbox.backends.local import _collect
from resoluto_sandbox.deps import Deps


class K8sBackend(Backend):
    """Runs the program in a Kata pod via ``drive_node``."""

    def __init__(self, *, image: str | None = None) -> None:
        self._image = image

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
            raise ValueError("backend='k8s' requires image=")
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
        """Launch a Kata pod via drive_node, stage workspace in, fetch artifacts out."""
        from resoluto_sandbox.contracts import SandboxLaunchSpec
        from resoluto_sandbox.driver import drive_node
        from resoluto_sandbox.runner_main import store_from_env
        from resoluto_sandbox.runtime.k8s import K8sSandboxRuntime
        from resoluto_sandbox.staging import fetch_outputs, put_dir

        store = store_from_env()

        run_id = "run-" + uuid4().hex[:8]
        node_id = "run"
        prefix = f"run/{run_id}/nodes/{node_id}/lane-0"

        runtime = K8sSandboxRuntime(
            namespace=os.environ.get("RESOLUTO_SANDBOX_NAMESPACE", "resoluto-sandboxes"),
            context=os.environ.get("RESOLUTO_SANDBOX_KUBECONTEXT") or None,
            image_pull_policy=os.environ.get("RESOLUTO_LANE_IMAGE_PULL_POLICY", "IfNotPresent"),
        )

        if workspace:
            await put_dir(store, prefix, workspace)

        store_env = {
            k: v
            for k, v in os.environ.items()
            if k.startswith("RESOLUTO_STORE_") or k.startswith("AWS_") or k == "RESOLUTO_TRUSTED_LOCAL"
        }

        pod_env: dict[str, str] = {
            **store_env,
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
                msg = str(ev.data.get("msg") or ev.data.get("text") or "")
                if msg:
                    out_lines.append(msg + "\n")
                    sink.write(msg + "\n")
                    sink.flush()

        result = await drive_node(runtime, store, spec, on_event=on_event, dead_after_s=600.0)

        artifacts: list[str] = []
        if output_paths and workspace:
            await fetch_outputs(store, prefix, str(Path(workspace)))
            artifacts = _collect(Path(workspace), output_paths)

        exit_code = result.exit_code if result.exit_code is not None else (0 if result.status == "success" else 1)
        return RunResult(
            exit_code=exit_code,
            stdout="".join(out_lines),
            stderr="",
            artifacts=artifacts,
            result=None,
        )

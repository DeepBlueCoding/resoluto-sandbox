"""Runtime-agnostic substrate backend that runs a program in an injected sandbox."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import IO, Sequence
from uuid import uuid4

from resoluto_sandbox.backends.artifacts import _collect, read_result_json
from resoluto_sandbox.backends.base import Backend, RunResult
from resoluto_sandbox.contracts import Conduit, Resources, SandboxRuntime


def _append_log_event(ev, out_lines: list[str], sink) -> None:
    """Append a log SpanEvent's line to out_lines and echo it to sink."""
    if ev.event == "log":
        line = str(ev.data.get("line") or "")
        if line:
            text = line if line.endswith("\n") else line + "\n"
            out_lines.append(text)
            sink.write(text)
            sink.flush()


class SubstrateBackend(Backend):
    """Runs a program in a sandbox via an injected runtime, conduit, image, and store env."""

    def __init__(
        self,
        *,
        runtime: SandboxRuntime,
        conduit: Conduit,
        image: str,
        store_env: dict[str, str],
    ) -> None:
        if not image:
            raise ValueError("SubstrateBackend requires image=...")
        self._runtime = runtime
        self._conduit = conduit
        self._image = image
        self._store_env = store_env

    def run(
        self,
        argv: Sequence[str],
        *,
        workspace: str | None = None,
        stdin: str | bytes | None = None,
        env: dict[str, str] | None = None,
        output_paths: Sequence[str] | None = None,
        stream: IO[str] | None = None,
        egress: Sequence[str] | None = None,
    ) -> RunResult:
        if stdin is not None:
            raise NotImplementedError("stdin is not supported on the substrate backend")
        return asyncio.run(self._run_async(argv, workspace=workspace, env=env,
                                           output_paths=output_paths, stream=stream, egress=egress))

    async def _run_async(
        self,
        argv: Sequence[str],
        *,
        workspace: str | None,
        env: dict[str, str] | None,
        output_paths: Sequence[str] | None,
        stream: IO[str] | None,
        egress: Sequence[str] | None = None,
    ) -> RunResult:
        """Launch a sandbox via drive_node, stage workspace in, fetch artifacts out into ``workspace``.

        ``egress`` (a per-RUN list of allowed domains) is applied to the runtime for THIS run only —
        set up the step's networking on the fly, then torn down — if the runtime supports it.
        """
        apply_egress = getattr(self._runtime, "apply_egress", None)
        clear_egress = getattr(self._runtime, "clear_egress", None)
        if apply_egress is not None:
            await apply_egress(list(egress) if egress is not None else [])
        try:
            return await self._launch_and_collect(argv, workspace=workspace, env=env,
                                                   output_paths=output_paths, stream=stream)
        finally:
            if clear_egress is not None:
                await clear_egress()

    async def _launch_and_collect(
        self,
        argv: Sequence[str],
        *,
        workspace: str | None,
        env: dict[str, str] | None,
        output_paths: Sequence[str] | None,
        stream: IO[str] | None,
    ) -> RunResult:
        from resoluto_sandbox.contracts import SandboxLaunchSpec
        from resoluto_sandbox.driver import drive_node
        from resoluto_sandbox.staging import fetch_outputs, put_dir

        run_id = "run-" + uuid4().hex[:8]
        node_id = "run"
        prefix = f"run/{run_id}/nodes/{node_id}/lane-0"

        if workspace:
            await put_dir(self._conduit, prefix, workspace)

        pod_env: dict[str, str] = {
            **self._store_env,
            **(env or {}),
            "RESOLUTO_STORE_PREFIX": prefix,
            "RESOLUTO_RUN_ID": run_id,
            "RESOLUTO_NODE_ID": node_id,
            "RESOLUTO_WORKLOAD_ARGV": json.dumps(list(argv)),
            "RESOLUTO_WORKSPACE_DIR": "/workspace",
            **({"RESOLUTO_OUTPUT_PATHS": json.dumps(list(output_paths))} if output_paths else {}),
        }

        spec = SandboxLaunchSpec(
            image=self._image,
            flavor="plain",
            env=pod_env,
            args=["python", "-m", "resoluto_sandbox.runner_main"],
            resources=Resources.from_quantities(memory="4Gi", cpu="2"),
            store_prefix=prefix,
            labels={"resoluto.run_id": run_id, "resoluto.node_id": node_id},
        )

        out_lines: list[str] = []
        sink = stream if stream is not None else sys.stdout

        result = await drive_node(
            self._runtime, self._conduit, spec,
            on_event=lambda ev: _append_log_event(ev, out_lines, sink),
            dead_after_s=600.0,
        )

        artifacts: list[str] = []
        node_result: dict | None = None
        if output_paths and workspace:
            await fetch_outputs(self._conduit, prefix, str(Path(workspace)))
            artifacts = _collect(Path(workspace), output_paths)
            node_result = read_result_json(Path(workspace))

        exit_code = result.exit_code if result.exit_code is not None else (0 if result.status == "success" else 1)
        return RunResult(
            exit_code=exit_code,
            output="".join(out_lines),
            errors="",
            artifacts=artifacts,
            result=node_result,
            reason=(result.reason or result.observed_phase or ""),
        )


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

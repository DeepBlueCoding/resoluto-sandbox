"""Runtime-agnostic substrate backend that runs a program in an injected sandbox."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import IO, TYPE_CHECKING, Sequence
from uuid import uuid4

if TYPE_CHECKING:
    import os

from resoluto.sandbox.backends.artifacts import _collect, read_result_json
from resoluto.sandbox.backends.base import Backend, RunResult
from resoluto.sandbox.contracts import Conduit, Resources, SandboxRuntime
from resoluto.sandbox.envfile import parse_env_file
from resoluto.sandbox.secrets import SecretKeyRef


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
        resources: Resources | None = None,
        dead_after_s: float = 600.0,
    ) -> None:
        if not image:
            raise ValueError("SubstrateBackend requires image=...")
        self._runtime = runtime
        self._conduit = conduit
        self._image = image
        self._store_env = store_env
        self._resources = resources or Resources.from_quantities(memory="4Gi", cpu="2")
        self._dead_after_s = dead_after_s

    def run(
        self,
        argv: Sequence[str],
        *,
        workspace: str | None = None,
        stdin: str | bytes | None = None,
        env: dict[str, str] | None = None,
        env_file: str | None = None,
        secrets: "dict[str, str | SecretKeyRef] | None" = None,
        output_paths: Sequence[str] | None = None,
        stream: IO[str] | None = None,
        egress: Sequence[str] | None = None,
    ) -> RunResult:
        if stdin is not None:
            raise NotImplementedError("stdin is not supported on the substrate backend")
        return asyncio.run(
            self._run_async(
                argv,
                workspace=workspace,
                env=env,
                env_file=env_file,
                secrets=secrets,
                output_paths=output_paths,
                stream=stream,
                egress=egress,
            )
        )

    async def _run_async(
        self,
        argv: Sequence[str],
        *,
        workspace: str | None,
        env: dict[str, str] | None,
        env_file: str | None = None,
        secrets: "dict[str, str | SecretKeyRef] | None" = None,
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
            return await self._launch_and_collect(
                argv,
                workspace=workspace,
                env=env,
                env_file=env_file,
                secrets=secrets,
                output_paths=output_paths,
                stream=stream,
            )
        finally:
            if clear_egress is not None:
                await clear_egress()
            await self._conduit.aclose()

    async def _launch_and_collect(
        self,
        argv: Sequence[str],
        *,
        workspace: str | None,
        env: dict[str, str] | None,
        env_file: str | None = None,
        secrets: "dict[str, str | SecretKeyRef] | None" = None,
        output_paths: Sequence[str] | None,
        stream: IO[str] | None,
    ) -> RunResult:
        from resoluto.sandbox.contracts import SandboxLaunchSpec
        from resoluto.sandbox.driver import drive_node
        from resoluto.sandbox.staging import fetch_outputs, put_dir

        run_id = "run-" + uuid4().hex[:8]
        node_id = "run"
        prefix = f"run/{run_id}"

        if workspace:
            await put_dir(self._conduit, prefix, workspace)

        # env_file is a host-side convenience, NOT a security mechanism — its values land as
        # literal env entries exactly like env= does. Explicit env= wins on key conflict.
        file_env = parse_env_file(env_file) if env_file else {}
        provider_refs = {k: v for k, v in (secrets or {}).items() if isinstance(v, str)}
        k8s_refs = {
            k: (v.name, v.key) for k, v in (secrets or {}).items() if isinstance(v, SecretKeyRef)
        }

        pod_env: dict[str, str] = {
            **self._store_env,
            **file_env,
            **(env or {}),
            "RESOLUTO_STORE_PREFIX": prefix,
            "RESOLUTO_RUN_ID": run_id,
            "RESOLUTO_NODE_ID": node_id,
            "RESOLUTO_WORKLOAD_ARGV": json.dumps(list(argv)),
            "RESOLUTO_WORKSPACE_DIR": "/workspace",
            **({"RESOLUTO_OUTPUT_PATHS": json.dumps(list(output_paths))} if output_paths else {}),
            **({"RESOLUTO_SECRET_REFS": json.dumps(provider_refs)} if provider_refs else {}),
        }

        spec = SandboxLaunchSpec(
            image=self._image,
            flavor="plain",
            env=pod_env,
            args=["python", "-m", "resoluto.sandbox.runner_main"],
            resources=self._resources,
            store_prefix=prefix,
            labels={"resoluto.run_id": run_id, "resoluto.node_id": node_id},
            k8s_secret_refs=k8s_refs,
        )

        out_lines: list[str] = []
        sink = stream if stream is not None else sys.stdout

        result = await drive_node(
            self._runtime,
            self._conduit,
            spec,
            on_event=lambda ev: _append_log_event(ev, out_lines, sink),
            dead_after_s=self._dead_after_s,
        )

        artifacts: list[str] = []
        node_result: dict | None = None
        if output_paths and workspace:
            await fetch_outputs(
                self._conduit, prefix, str(Path(workspace)), allowed_paths=list(output_paths)
            )
            artifacts = _collect(Path(workspace), output_paths)
            node_result = read_result_json(Path(workspace))

        exit_code = (
            result.exit_code
            if result.exit_code is not None
            else (0 if result.status == "success" else 1)
        )
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


def secrets_env_for_pod(environ: "os._Environ[str] | dict[str, str]") -> dict[str, str]:
    """Select the RESOLUTO_SECRETS_* env the sandbox may inherit for guest-side SecretProvider
    resolution. Only explicit RESOLUTO_SECRETS_*-prefixed vars pass through — an ambient credential
    the host holds for unrelated purposes (e.g. VAULT_TOKEN) is never auto-forwarded."""
    return {k: v for k, v in environ.items() if k.startswith("RESOLUTO_SECRETS_")}

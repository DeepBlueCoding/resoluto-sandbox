"""The single public entrypoint: ``Sandbox(...).run(argv, ...)``.

The program you run is plain — it reads argv/stdin and writes stdout/files, and
never imports ``resoluto_sandbox``. The guarantee: a program that runs as
``uv run agent.py`` on your machine runs byte-identically under ``run()``.

``backend="local"`` runs the program as a subprocess on this host, inheriting
the host environment (so an already-logged-in agent CLI authenticates with no
extra wiring) and streaming its output live to ``stream`` (default stdout).

``backend="k8s"`` launches a Kata pod via the existing ``drive_node`` primitive.
Requires ``RESOLUTO_STORE_KIND`` in the environment and ``image=`` set.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import IO, Literal, Sequence
from uuid import uuid4

from pydantic import BaseModel

from resoluto_sandbox.deps import Deps, resolve_invocation

Backend = Literal["local", "k8s"]


class RunResult(BaseModel):
    """Outcome of one ``run()``. ``stdout`` is the program's answer; ``artifacts``
    are the collected ``output_paths``; ``result`` is a parsed ``result.json`` if
    the program wrote one (otherwise ``None``)."""

    exit_code: int
    stdout: str
    stderr: str
    artifacts: list[str] = []
    result: dict | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class Sandbox:
    """Run a program in a sandbox. ``backend`` selects where it runs."""

    def __init__(self, *, backend: Backend = "local", image: str | None = None) -> None:
        if backend not in ("local", "k8s"):
            raise ValueError(f"unknown backend {backend!r} (expected 'local' or 'k8s')")
        self._backend = backend
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
        """Run ``argv`` in the sandbox. ``workspace`` (a directory) is the program's
        cwd; ``stdin`` is fed on standard input; ``env`` overlays the host env;
        ``output_paths`` are globs collected into ``RunResult.artifacts``; ``stream``
        receives stdout live (default ``sys.stdout``). Returns a ``RunResult``."""
        if self._backend == "k8s":
            return self._run_k8s(argv, workspace=workspace, env=env, output_paths=output_paths, stream=stream)

        cwd = Path(workspace).resolve() if workspace else Path.cwd()
        if not cwd.is_dir():
            raise NotADirectoryError(f"workspace is not a directory: {cwd}")

        child_env = dict(os.environ)
        if env:
            child_env.update(env)

        launch_argv = resolve_invocation(argv, deps or Deps(), cwd)
        sink = stream if stream is not None else sys.stdout
        proc = subprocess.Popen(
            launch_argv,
            cwd=str(cwd),
            env=child_env,
            stdin=subprocess.PIPE if stdin is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        out_buf: list[str] = []
        err_buf: list[str] = []
        out_t = threading.Thread(target=_pump, args=(proc.stdout, sink, out_buf))
        err_t = threading.Thread(target=_pump, args=(proc.stderr, sys.stderr, err_buf))
        out_t.start()
        err_t.start()

        if stdin is not None and proc.stdin is not None:
            proc.stdin.write(stdin.decode() if isinstance(stdin, bytes) else stdin)
            proc.stdin.close()

        exit_code = proc.wait()
        out_t.join()
        err_t.join()

        artifacts = _collect(cwd, output_paths)
        return RunResult(
            exit_code=exit_code,
            stdout="".join(out_buf),
            stderr="".join(err_buf),
            artifacts=artifacts,
            result=_read_result_json(cwd),
        )

    def _run_k8s(
        self,
        argv: Sequence[str],
        *,
        workspace: str | None,
        env: dict[str, str] | None,
        output_paths: Sequence[str] | None,
        stream: IO[str] | None,
    ) -> RunResult:
        return asyncio.run(self._run_k8s_async(argv, workspace=workspace, env=env, output_paths=output_paths, stream=stream))

    async def _run_k8s_async(
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

        if self._image is None:
            raise ValueError("backend='k8s' requires image=")

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


def _pump(src: IO[str], sink: IO[str], buf: list[str]) -> None:
    """Tee a stream: append each line to ``buf`` and echo it to ``sink`` live."""
    for line in iter(src.readline, ""):
        buf.append(line)
        sink.write(line)
        sink.flush()
    src.close()


def _collect(cwd: Path, output_paths: Sequence[str] | None) -> list[str]:
    if not output_paths:
        return []
    found: list[str] = []
    for pattern in output_paths:
        found.extend(sorted(glob.glob(str(cwd / pattern), recursive=True)))
    return found


def _read_result_json(cwd: Path) -> dict | None:
    path = cwd / "result.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text())

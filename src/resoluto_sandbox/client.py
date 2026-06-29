"""The single public entrypoint ``Sandbox(...).run(argv, ...)``, selecting a ``local`` (Kata microVM via nerdctl) or ``k8s`` (Kata pod) backend, or an injected ``Backend``."""
from __future__ import annotations

import os
import tempfile
from typing import IO, Sequence

from resoluto_sandbox.backends.base import Backend, RunResult
from resoluto_sandbox.backends.substrate import SubstrateBackend, store_env_for_pod

DEFAULT_LOCAL_IMAGE = "resoluto-sandbox-base:dev"


def _build_local_backend(image: str | None) -> SubstrateBackend:
    """Build the local preset wiring a fresh temp LocalConduit to a KataNerdctlSandboxRuntime. Inputs: optional image override. Output: a SubstrateBackend."""
    from resoluto_sandbox.conduit import LocalConduit
    from resoluto_sandbox.runtime.kata_nerdctl import KataNerdctlSandboxRuntime

    conduit_dir = tempfile.mkdtemp(prefix="resoluto-sbx-")
    conduit = LocalConduit(conduit_dir, world_writable=True)
    runtime = KataNerdctlSandboxRuntime.from_env(conduit_host_dir=conduit_dir, conduit_mount="/conduit")
    store_env = {
        "RESOLUTO_STORE_KIND": "localfs",
        "RESOLUTO_STORE_ROOT": "/conduit",
    }
    return SubstrateBackend(
        runtime=runtime, conduit=conduit, image=image or DEFAULT_LOCAL_IMAGE, store_env=store_env
    )


def _build_k8s_backend(image: str | None) -> SubstrateBackend:
    """Build the k8s preset wiring a conduit from env to a K8sSandboxRuntime. Inputs: optional image override (falls back to RESOLUTO_LANE_IMAGE). Output: a SubstrateBackend."""
    from resoluto_sandbox.conduit.factory import store_from_env
    from resoluto_sandbox.runtime.k8s import EgressConfig, K8sSandboxRuntime

    image = image or os.environ.get("RESOLUTO_LANE_IMAGE")
    if not image:
        raise ValueError("backend='k8s' requires image=... or RESOLUTO_LANE_IMAGE")
    conduit = store_from_env()
    runtime = K8sSandboxRuntime(
        egress=EgressConfig.from_store_env(),
        namespace=os.environ.get("RESOLUTO_SANDBOX_NAMESPACE", "resoluto-sandboxes"),
        context=os.environ.get("RESOLUTO_SANDBOX_KUBECONTEXT") or None,
        image_pull_policy=os.environ.get("RESOLUTO_LANE_IMAGE_PULL_POLICY", "IfNotPresent"),
    )
    store_env = store_env_for_pod(os.environ)
    return SubstrateBackend(runtime=runtime, conduit=conduit, image=image, store_env=store_env)


class Sandbox:
    """Run a program in a sandbox. Holds a Backend (selected by name or injected)."""

    def __init__(self, *, backend: "Backend | str" = "local", image: str | None = None) -> None:
        if isinstance(backend, Backend):
            self._backend = backend
        elif backend == "local":
            self._backend = _build_local_backend(image)
        elif backend == "k8s":
            self._backend = _build_k8s_backend(image)
        else:
            raise ValueError(f"unknown backend {backend!r} (expected 'local', 'k8s', or a Backend)")

    def run(
        self,
        argv: Sequence[str],
        *,
        workspace: str | None = None,
        stdin: str | bytes | None = None,
        env: dict[str, str] | None = None,
        output_paths: Sequence[str] | None = None,
        stream: IO[str] | None = None,
    ) -> RunResult:
        """Run ``argv`` in the sandbox with ``workspace`` cwd, ``env`` overlay, ``output_paths`` globs collected into ``RunResult.artifacts``, and live output to ``stream``; returns a ``RunResult``."""
        return self._backend.run(argv, workspace=workspace, stdin=stdin, env=env,
                                 output_paths=output_paths, stream=stream)

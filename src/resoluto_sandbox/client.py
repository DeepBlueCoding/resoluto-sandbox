"""The single public entrypoint: ``Sandbox(...).run(argv, ...)``.

The program you run is plain — it reads argv/stdin and writes stdout/files, and
never imports ``resoluto_sandbox``. ONE substrate backend (``SubstrateBackend``)
runs it in an isolated sandbox; the only thing that varies is the injected
``SandboxRuntime``:

``backend="docker"`` runs the program in an isolated **Docker container** on this
host, sharing a ``LocalConduit`` with the container over a bind mount (no cluster,
no S3). The image must contain python + the resoluto-sandbox wheel + your program's
deps; defaults to ``DEFAULT_DOCKER_IMAGE``.

``backend="k8s"`` launches a Kata pod via the ``K8sSandboxRuntime``, reconstructing
the conduit from ``RESOLUTO_STORE_*`` in both host and pod. Requires
``RESOLUTO_STORE_KIND`` in the environment and a lane image.

You can also inject a fully-configured ``Backend`` — ``Sandbox(backend=SubstrateBackend(...))``.
"""
from __future__ import annotations

import os
import tempfile
from typing import IO, Sequence

from resoluto_sandbox.backends.base import Backend, RunResult
from resoluto_sandbox.backends.substrate import SubstrateBackend, store_env_for_pod

# A docker image containing python + the resoluto-sandbox wheel + your program's deps.
DEFAULT_DOCKER_IMAGE = "resoluto-sandbox-runner:dev"


def _build_docker_backend(image: str | None) -> SubstrateBackend:
    """Build the Docker preset: a fresh temp LocalConduit shared with the container
    over a bind mount at /conduit. Inputs: optional image override. Output: a
    SubstrateBackend wired to a DockerSandboxRuntime."""
    from resoluto_sandbox.conduit import LocalConduit
    from resoluto_sandbox.runtime.docker import DockerSandboxRuntime

    conduit_dir = tempfile.mkdtemp(prefix="resoluto-sbx-")
    conduit = LocalConduit(conduit_dir)
    runtime = DockerSandboxRuntime(conduit_host_dir=conduit_dir, conduit_mount="/conduit")
    # Plain Docker has no k8s egress NetworkPolicy, so the in-container egress canary
    # (which enforces that allowlist) does not apply — trusted-local skips it.
    store_env = {
        "RESOLUTO_STORE_KIND": "localfs",
        "RESOLUTO_STORE_ROOT": "/conduit",
        "RESOLUTO_TRUSTED_LOCAL": "1",
    }
    return SubstrateBackend(
        runtime=runtime, conduit=conduit, image=image or DEFAULT_DOCKER_IMAGE, store_env=store_env
    )


def _build_k8s_backend(image: str | None) -> SubstrateBackend:
    """Build the k8s preset: a conduit from env + a K8sSandboxRuntime + the pod
    store env. Inputs: optional image override (falls back to RESOLUTO_LANE_IMAGE).
    Output: a SubstrateBackend wired to a K8sSandboxRuntime."""
    from resoluto_sandbox.conduit.factory import store_from_env
    from resoluto_sandbox.runtime.k8s import K8sSandboxRuntime

    image = image or os.environ.get("RESOLUTO_LANE_IMAGE")
    if not image:
        raise ValueError("backend='k8s' requires image=... or RESOLUTO_LANE_IMAGE")
    conduit = store_from_env()
    runtime = K8sSandboxRuntime(
        namespace=os.environ.get("RESOLUTO_SANDBOX_NAMESPACE", "resoluto-sandboxes"),
        context=os.environ.get("RESOLUTO_SANDBOX_KUBECONTEXT") or None,
        image_pull_policy=os.environ.get("RESOLUTO_LANE_IMAGE_PULL_POLICY", "IfNotPresent"),
    )
    store_env = store_env_for_pod(os.environ)
    return SubstrateBackend(runtime=runtime, conduit=conduit, image=image, store_env=store_env)


class Sandbox:
    """Run a program in a sandbox. Holds a Backend (selected by name or injected)."""

    def __init__(self, *, backend: "Backend | str" = "docker", image: str | None = None) -> None:
        if isinstance(backend, Backend):
            self._backend = backend
        elif backend == "docker":
            self._backend = _build_docker_backend(image)
        elif backend == "k8s":
            self._backend = _build_k8s_backend(image)
        else:
            raise ValueError(f"unknown backend {backend!r} (expected 'docker', 'k8s', or a Backend)")

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
        """Run ``argv`` in the sandbox. ``workspace`` (a directory) is the program's
        cwd; ``stdin`` is unsupported (substrate backends are non-interactive); ``env``
        overlays the sandbox env; ``output_paths`` are globs collected into
        ``RunResult.artifacts``; ``stream`` receives output live (default ``sys.stdout``).
        Returns a ``RunResult``."""
        return self._backend.run(argv, workspace=workspace, stdin=stdin, env=env,
                                 output_paths=output_paths, stream=stream)

"""The single public entrypoint ``Sandbox(...).run(argv, ...)``, selecting a ``local`` (Kata microVM via nerdctl) or ``k8s`` (Kata pod) backend, or an injected ``Backend``."""
from __future__ import annotations

import os
import tempfile
from importlib.metadata import version as _pkg_version
from typing import IO, Sequence

from resoluto_sandbox.backends.base import Backend, RunResult
from resoluto_sandbox.backends.substrate import SubstrateBackend, secrets_env_for_pod, store_env_for_pod
from resoluto_sandbox.secrets import SecretKeyRef


def default_local_image() -> str:
    """The image ``backend="local"`` uses when ``image=`` is omitted: the base substrate tagged to
    the CURRENTLY INSTALLED ``resoluto-sandbox`` version — never a floating ``:dev``/``:latest`` tag.
    Build it with ``resoluto-sandbox image build`` (tags ``resoluto-sandbox-base:<this version>``
    automatically), or ``docker build -f Dockerfile.base -t resoluto-sandbox-base:<this version> ..``
    directly, then load it into the local containerd (see README: Prebuilt provider images)."""
    return f"resoluto-sandbox-base:{_pkg_version('resoluto-sandbox')}"


def _local_conduit_base() -> str:
    """Return a user-private (0o700), disk-backed base directory for local conduits.

    The Kata guest (launched via sudo nerdctl) writes into the bind-mounted conduit as a
    different uid, so the leaf must stay group/world-writable. Gating it behind a 0o700 parent
    that only the invoking user can traverse keeps lane data (and any staged tokens) unreachable
    to other local users regardless of the leaf's mode. Disk-backed (not XDG_RUNTIME_DIR tmpfs),
    since lane artifacts can be large.
    """
    base = os.path.join(tempfile.gettempdir(), f"resoluto-sandbox-{os.getuid()}")
    os.makedirs(base, mode=0o700, exist_ok=True)
    os.chmod(base, 0o700)  # enforce 0o700 even if it pre-existed with a looser mode
    return base


def _build_local_backend(image: str | None) -> SubstrateBackend:
    """Build the local backend, wiring a fresh temp LocalConduit to a KataNerdctlSandboxRuntime. Inputs: optional image override. Output: a SubstrateBackend."""
    from resoluto_sandbox.conduit import LocalConduit
    from resoluto_sandbox.runtime.kata_nerdctl import KataNerdctlSandboxRuntime

    conduit_dir = tempfile.mkdtemp(prefix="sbx-", dir=_local_conduit_base())
    conduit = LocalConduit(conduit_dir, world_writable=True)
    runtime = KataNerdctlSandboxRuntime.from_env(conduit_host_dir=conduit_dir, conduit_mount="/conduit")
    store_env = {
        "RESOLUTO_STORE_KIND": "localfs",
        "RESOLUTO_STORE_ROOT": "/conduit",
    }
    return SubstrateBackend(
        runtime=runtime, conduit=conduit, image=image or default_local_image(), store_env=store_env
    )


def _build_k8s_backend(image: str | None) -> SubstrateBackend:
    """Build the k8s backend, wiring a conduit from env to a K8sSandboxRuntime. Inputs: optional image override (falls back to RESOLUTO_LANE_IMAGE). Output: a SubstrateBackend."""
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
    store_env = {**store_env_for_pod(os.environ), **secrets_env_for_pod(os.environ)}
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
        env_file: str | None = None,
        secrets: "dict[str, str | SecretKeyRef] | None" = None,
        output_paths: Sequence[str] | None = None,
        stream: IO[str] | None = None,
        egress: Sequence[str] | None = None,
    ) -> RunResult:
        """Run ``argv`` in the sandbox with ``workspace`` cwd, ``env`` overlay, ``output_paths`` globs
        collected into ``RunResult.artifacts``, and live output to ``stream``; returns a ``RunResult``.

        ``env_file`` parses a dotenv-format file host-side and merges it under ``env`` (``env`` wins
        on conflict) — a convenience for literal config, NOT a security mechanism: values still land
        as literal env entries, same as ``env``.

        ``secrets`` maps an env var name to either a ``SecretKeyRef`` (k8s-native — references an
        existing Kubernetes Secret's key via ``valueFrom.secretKeyRef``, zero guest-side code; ignored
        on the ``local`` backend) or a plain ``str`` (a provider-specific ref resolved GUEST-SIDE by
        the configured ``SecretProvider`` — see ``secrets.py`` — so the plaintext value never touches
        the host, the pod spec, or any log).

        ``egress`` is THIS run's allowed-domain list (e.g. ``["api.anthropic.com"]``) — per-step
        networking set up on the fly and torn down after, with no re-provisioning. ``None``/``[]`` =
        deny all outbound (secure default). Currently applied by the ``local`` backend's SNI proxy.
        """
        return self._backend.run(argv, workspace=workspace, stdin=stdin, env=env, env_file=env_file,
                                 secrets=secrets, output_paths=output_paths, stream=stream, egress=egress)

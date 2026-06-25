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

from typing import IO, Sequence

from resoluto_sandbox.backends.base import Backend, RunResult
from resoluto_sandbox.backends.local import LocalBackend
from resoluto_sandbox.backends.k8s import K8sBackend
from resoluto_sandbox.deps import Deps


class Sandbox:
    """Run a program in a sandbox. Holds a Backend (selected by name or injected)."""

    def __init__(self, *, backend: "Backend | str" = "local", image: str | None = None) -> None:
        if isinstance(backend, Backend):
            self._backend = backend
        elif backend == "local":
            self._backend = LocalBackend()
        elif backend == "k8s":
            self._backend = K8sBackend(image=image)
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
        deps: Deps | None = None,
    ) -> RunResult:
        """Run ``argv`` in the sandbox. ``workspace`` (a directory) is the program's
        cwd; ``stdin`` is fed on standard input; ``env`` overlays the host env;
        ``output_paths`` are globs collected into ``RunResult.artifacts``; ``stream``
        receives stdout live (default ``sys.stdout``). Returns a ``RunResult``."""
        return self._backend.run(argv, workspace=workspace, stdin=stdin, env=env,
                                 output_paths=output_paths, stream=stream, deps=deps)

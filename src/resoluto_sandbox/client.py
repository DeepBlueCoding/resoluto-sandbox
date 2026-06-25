"""The single public entrypoint: ``Sandbox(...).run(argv, ...)``.

The program you run is plain — it reads argv/stdin and writes stdout/files, and
never imports ``resoluto_sandbox``. The guarantee: a program that runs as
``uv run agent.py`` on your machine runs byte-identically under ``run()``.

``backend="local"`` runs the program as a subprocess on this host, inheriting
the host environment (so an already-logged-in agent CLI authenticates with no
extra wiring) and streaming its output live to ``stream`` (default stdout).
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import IO, Literal, Sequence

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
        if backend == "k8s":
            raise NotImplementedError(
                "the k8s backend is not wired in this build yet — use backend='local'"
            )
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

"""Local subprocess backend."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import IO, Sequence

from resoluto_sandbox.backends.artifacts import _collect, read_result_json
from resoluto_sandbox.backends.base import Backend, RunResult


class LocalBackend(Backend):
    """Runs the program as a subprocess on this host.

    Provides NO isolation — the program runs as a normal subprocess on the host
    with the host environment. Use it for trusted code only; use the k8s backend
    for untrusted/adversarial workloads."""

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
        cwd = Path(workspace).resolve() if workspace else Path.cwd()
        if not cwd.is_dir():
            raise NotADirectoryError(f"workspace is not a directory: {cwd}")

        child_env = {**os.environ, **env} if env else None

        sink = stream if stream is not None else sys.stdout
        proc = subprocess.Popen(
            list(argv),
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

        try:
            if stdin is not None and proc.stdin is not None:
                try:
                    proc.stdin.write(stdin.decode() if isinstance(stdin, bytes) else stdin)
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
            exit_code = proc.wait()
        finally:
            out_t.join()
            err_t.join()

        artifacts = _collect(cwd, output_paths)
        return RunResult(
            exit_code=exit_code,
            output="".join(out_buf),
            errors="".join(err_buf),
            artifacts=artifacts,
            result=read_result_json(cwd),
        )


def _pump(src: IO[str], sink: IO[str], buf: list[str]) -> None:
    """Tee a stream: append each line to ``buf`` and echo it to ``sink`` live."""
    for line in iter(src.readline, ""):
        buf.append(line)
        sink.write(line)
        sink.flush()
    src.close()

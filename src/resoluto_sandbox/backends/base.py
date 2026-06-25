"""Base ABC for sandbox backends."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import IO, Sequence

from pydantic import BaseModel

from resoluto_sandbox.deps import Deps


class RunResult(BaseModel):
    """Outcome of one ``run()``. ``stdout`` is the program's answer; ``artifacts``
    are the collected ``output_paths``; ``result`` is a parsed ``result.json`` if
    the program wrote one (otherwise ``None``); ``reason`` carries substrate
    forensics (e.g. an evicted/OOMKilled pod) when available; empty for local."""

    exit_code: int
    stdout: str
    stderr: str
    artifacts: list[str] = []
    result: dict | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class Backend(ABC):
    """Runs a program and returns a RunResult. Implementations own the substrate
    (local subprocess, k8s/Kata pod, …). Inputs/outputs identical across backends."""

    @abstractmethod
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
    ) -> RunResult: ...

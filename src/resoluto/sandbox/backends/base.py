"""Base ABC for sandbox backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import IO, Sequence

from pydantic import BaseModel

from resoluto.sandbox.secrets import SecretKeyRef


class RunResult(BaseModel):
    """Outcome of one ``run()``: exit code, output/errors, collected ``artifacts`` paths, parsed ``result``, and a substrate ``reason``."""

    exit_code: int
    output: str
    errors: str
    artifacts: list[str] = []
    result: dict | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class Backend(ABC):
    """Runs a program and returns a RunResult."""

    @abstractmethod
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
    ) -> RunResult: ...

"""A write-only Conduit that prints telemetry to a stream (the local default).

Inputs are read from the workspace in place, so get/list/copy are unsupported —
this conduit exists only to surface events/results live on stdout."""
from __future__ import annotations
import sys
from typing import IO
from resoluto_sandbox.contracts import Conduit, ObjectInfo


class StdoutConduit(Conduit):
    def __init__(self, *, sink: IO[str] | None = None) -> None:
        self._sink = sink if sink is not None else sys.stdout

    async def put(self, key: str, data: bytes) -> None:
        self._sink.write(data.decode("utf-8", "replace").rstrip("\n") + "\n")
        self._sink.flush()

    async def get(self, key: str) -> bytes:
        raise NotImplementedError("StdoutConduit is write-only")

    async def list_prefix(self, prefix: str) -> list[ObjectInfo]:
        raise NotImplementedError("StdoutConduit is write-only")

    async def copy_prefix(self, src_prefix: str, dst_prefix: str) -> int:
        raise NotImplementedError("StdoutConduit is write-only")

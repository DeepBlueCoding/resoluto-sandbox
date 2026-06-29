"""Emit redacted open/close span events and log events over a ChunkShipper."""
from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from typing import Callable

from resoluto_sandbox.contracts import SpanEvent
from resoluto_sandbox.redact import redact_data, redact_text
from resoluto_sandbox.telemetry import ChunkShipper


def new_span_id() -> str:
    return uuid.uuid4().hex[:16]


class SpanEmitter:
    def __init__(self, shipper: ChunkShipper, run_id: str, *, clock: Callable[[], float] = time.time) -> None:
        self._ship = shipper
        self._run_id = run_id
        self._clock = clock

    async def _emit(self, **kw) -> None:
        await self._ship.emit(SpanEvent(run_id=self._run_id, ts=self._clock(), **kw))

    async def log(self, parent_span_id: str, text: str, *, kind: str = "log") -> None:
        await self._emit(
            span_id=new_span_id(), parent_span_id=parent_span_id, kind=kind,
            event="log", data={"line": redact_text(text)},
        )

    @asynccontextmanager
    async def span(self, parent_span_id: str, kind: str, name: str, *, inputs: dict | None = None):
        sid = new_span_id()
        await self._emit(
            span_id=sid, parent_span_id=parent_span_id, kind=kind, name=name,
            event="open", data=redact_data(inputs or {}),
        )
        status = "success"
        try:
            yield sid
        except BaseException:
            status = "failure"
            raise
        finally:
            await self._emit(
                span_id=sid, parent_span_id=parent_span_id, kind=kind, name=name,
                event="close", status=status,
            )

"""Store-mediated telemetry: the sandbox ships immutable JSONL chunks, the host tails them in index order."""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Callable

from resoluto.sandbox.contracts import Conduit, SpanEvent

_CHUNK_RE = re.compile(r"events-(\d+)\.jsonl$")
_MANIFEST = "_manifest.json"
RESULT_FILENAME = "result.json"
_RESULT = RESULT_FILENAME


def _chunk_key(prefix: str, index: int) -> str:
    return f"{prefix.rstrip('/')}/events-{index:06d}.jsonl"


def result_key(prefix: str) -> str:
    """Return the result object key for a store prefix."""
    return f"{prefix.rstrip('/')}/{_RESULT}"


def _default_heartbeat(ts: float) -> str:
    return SpanEvent(
        run_id="", span_id="hb", kind="heartbeat", event="log", ts=ts
    ).model_dump_json()


class ChunkShipper:
    """In-sandbox writer that buffers JSONL lines and flushes immutable chunks to the store, emitting a heartbeat when quiet."""

    def __init__(
        self,
        store: Conduit,
        prefix: str,
        *,
        flush_bytes: int = 64 * 1024,
        flush_interval_s: float = 5.0,
        heartbeat_s: float = 30.0,
        heartbeat_factory: Callable[[float], str] = _default_heartbeat,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._prefix = prefix.rstrip("/")
        self._flush_bytes = flush_bytes
        self._flush_interval_s = flush_interval_s
        self._heartbeat_s = heartbeat_s
        self._heartbeat_factory = heartbeat_factory
        self._clock = clock
        self._buf: list[str] = []
        self._buf_bytes = 0
        self._index = 0
        self._last_flush = clock()
        self._closed = False
        self._flush_lock = asyncio.Lock()

    async def emit_line(self, line: str) -> None:
        """Buffer one JSONL record, flushing if the byte threshold is reached."""
        self._buf.append(line)
        self._buf_bytes += len(line) + 1
        if self._buf_bytes >= self._flush_bytes:
            await self.flush()

    async def emit(self, event: SpanEvent) -> None:
        await self.emit_line(event.model_dump_json())

    async def tick(self) -> None:
        """Flush on the interval, or emit a heartbeat when idle."""
        now = self._clock()
        if self._buf and (now - self._last_flush) >= self._flush_interval_s:
            await self.flush()
        elif not self._buf and (now - self._last_flush) >= self._heartbeat_s:
            await self.emit_line(self._heartbeat_factory(now))
            await self.flush()

    async def flush(self) -> None:
        async with self._flush_lock:
            if not self._buf:
                return
            self._index += 1
            body = ("\n".join(self._buf) + "\n").encode("utf-8")
            await self._store.put(_chunk_key(self._prefix, self._index), body)
            self._buf.clear()
            self._buf_bytes = 0
            self._last_flush = self._clock()

    async def close(self) -> None:
        """Final flush plus a manifest naming the highest chunk index."""
        if self._closed:
            return
        await self.flush()
        manifest = json.dumps({"total_chunks": self._index}).encode("utf-8")
        await self._store.put(f"{self._prefix}/{_MANIFEST}", manifest)
        self._closed = True


class ChunkReader:
    """Host-side tail of a run's chunk objects in contiguous index order, with a silence-based death signal."""

    def __init__(
        self,
        store: Conduit,
        prefix: str,
        *,
        dead_after_s: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
        progress_filter: Callable[[str], bool] | None = None,
    ) -> None:
        self._store = store
        self._prefix = prefix.rstrip("/")
        self._dead_after_s = dead_after_s
        self._clock = clock
        self._progress_filter = progress_filter
        self._seen = 0
        self._last_progress = clock()
        self._last_arrival = clock()
        self._total: int | None = None
        self._armed = False

    @property
    def finished(self) -> bool:
        return self._total is not None and self._seen >= self._total

    async def poll_lines(self) -> list[str]:
        """Return newly-available JSONL records in contiguous index order, updating liveness."""
        infos = await self._store.list_prefix(self._prefix)
        present: set[int] = set()
        for info in infos:
            m = _CHUNK_RE.search(info.key)
            if m:
                present.add(int(m.group(1)))
            elif info.key.endswith(_MANIFEST) and self._total is None:
                self._total = json.loads(await self._store.get(info.key)).get("total_chunks")

        lines: list[str] = []
        nxt = self._seen + 1
        while nxt in present:
            raw = await self._store.get(_chunk_key(self._prefix, nxt))
            lines.extend(line for line in raw.decode("utf-8").splitlines() if line.strip())
            self._seen = nxt
            nxt += 1

        if lines:
            self._last_arrival = self._clock()
            if self._progress_filter is None:
                self._last_progress = self._clock()
            else:
                verdicts = [self._progress_filter(line) for line in lines]
                if any(verdicts):
                    self._last_progress = self._clock()
        return lines

    async def poll(self) -> list[SpanEvent]:
        return [SpanEvent.model_validate_json(line) for line in await self.poll_lines()]

    def arm(self) -> None:
        """Start the silence window, idempotently; until armed both death signals stay false."""
        if self._armed:
            return
        self._armed = True
        self._last_progress = self._last_arrival = self._clock()

    @property
    def armed(self) -> bool:
        """Whether the sandbox ever reached RUNNING (the silence window only arms there)."""
        return self._armed

    @property
    def seconds_since_progress(self) -> float:
        return self._clock() - self._last_progress

    @property
    def seconds_since_arrival(self) -> float:
        return self._clock() - self._last_arrival

    def is_dead(self) -> bool:
        """True when no progress line has arrived within the death window; false until armed or finished."""
        if not self._armed or self.finished:
            return False
        return (self._clock() - self._last_progress) > self._dead_after_s

    @property
    def substrate_silent(self) -> bool:
        """True when no chunk has arrived within the death window; false until armed."""
        return self._armed and self.seconds_since_arrival > self._dead_after_s

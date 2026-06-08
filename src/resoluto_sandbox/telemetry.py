"""Store-mediated telemetry — the comms + observability spine (design §11.2/§13).

Append-free: the in-sandbox `ChunkShipper` writes immutable, sequence-numbered
chunk objects (`events-000001.jsonl`, …); the orchestrator `ChunkReader` lists +
concatenates them in index order. No append semantics, no long-lived stream, no
in-sandbox server — reconnect is just "re-list, resume at index". Liveness =
monotonic chunk arrival; dead = no new chunk within the substrate timeout (the
RES-236 count-vs-time fix, now a property of object listing).
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Callable

from resoluto_sandbox.contracts import ObjectStore, SpanEvent

_CHUNK_RE = re.compile(r"events-(\d+)\.jsonl$")
_MANIFEST = "_manifest.json"
_RESULT = "result.json"


def _chunk_key(prefix: str, index: int) -> str:
    return f"{prefix.rstrip('/')}/events-{index:06d}.jsonl"


def result_key(prefix: str) -> str:
    """The single source of truth for the result object key — shared by the
    runner (writer) and driver (reader) so the rendezvous can't drift."""
    return f"{prefix.rstrip('/')}/{_RESULT}"


class ChunkShipper:
    """In-sandbox: buffer SpanEvents, flush immutable chunks to the store.

    Inputs: an `ObjectStore`, the run's `prefix`, flush thresholds, and an
    injectable `clock` (tests pass a fake; no real sleeps). A heartbeat ensures a
    chunk lands every `heartbeat_s` even when quiet, so the reader's liveness
    signal keeps ticking.
    """

    def __init__(
        self,
        store: ObjectStore,
        prefix: str,
        *,
        flush_bytes: int = 64 * 1024,
        flush_interval_s: float = 5.0,
        heartbeat_s: float = 30.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._prefix = prefix.rstrip("/")
        self._flush_bytes = flush_bytes
        self._flush_interval_s = flush_interval_s
        self._heartbeat_s = heartbeat_s
        self._clock = clock
        self._buf: list[str] = []
        self._buf_bytes = 0
        self._index = 0
        self._last_flush = clock()
        self._closed = False
        self._flush_lock = asyncio.Lock()  # emit/tick/close may flush concurrently

    async def emit(self, event: SpanEvent) -> None:
        line = event.model_dump_json()
        self._buf.append(line)
        self._buf_bytes += len(line) + 1
        if self._buf_bytes >= self._flush_bytes:
            await self.flush()

    async def tick(self) -> None:
        """Time-driven flush + heartbeat — called on an interval by the runner."""
        now = self._clock()
        if self._buf and (now - self._last_flush) >= self._flush_interval_s:
            await self.flush()
        elif not self._buf and (now - self._last_flush) >= self._heartbeat_s:
            await self.emit(SpanEvent(
                run_id="", span_id="hb", kind="heartbeat", event="log", ts=now,
            ))
            await self.flush()

    async def flush(self) -> None:
        async with self._flush_lock:  # serialize: no interleaved _index / _buf
            if not self._buf:
                return
            self._index += 1
            body = ("\n".join(self._buf) + "\n").encode("utf-8")
            await self._store.put(_chunk_key(self._prefix, self._index), body)
            self._buf.clear()
            self._buf_bytes = 0
            self._last_flush = self._clock()

    async def close(self) -> None:
        """Final flush + a manifest naming the highest index — lets the reader
        tell 'gap, still arriving' from 'gap, terminal' (§11.2/E3)."""
        if self._closed:
            return
        await self.flush()
        manifest = json.dumps({"total_chunks": self._index}).encode("utf-8")
        await self._store.put(f"{self._prefix}/{_MANIFEST}", manifest)
        self._closed = True


class ChunkReader:
    """Orchestrator: tail a run's chunk objects in contiguous index order.

    `poll()` returns newly-available events since the last call. `is_dead()` is
    true when no new chunk has arrived within `dead_after_s` AND the run isn't
    cleanly finished — the SINGLE substrate-death signal, time-bounded and distinct
    from agent-work liveness. A persistent non-contiguous gap (have chunk N+1, never
    saw N) stalls contiguous progress, so it surfaces through that same `is_dead()`
    window — no separate raise path to escape the driver's reap-on-death handling.
    """

    def __init__(
        self,
        store: ObjectStore,
        prefix: str,
        *,
        dead_after_s: float = 120.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._prefix = prefix.rstrip("/")
        self._dead_after_s = dead_after_s
        self._clock = clock
        self._seen = 0  # highest CONTIGUOUS index consumed
        self._last_progress = clock()
        self._total: int | None = None

    @property
    def finished(self) -> bool:
        return self._total is not None and self._seen >= self._total

    async def poll(self) -> list[SpanEvent]:
        infos = await self._store.list_prefix(self._prefix)
        present: set[int] = set()
        for info in infos:
            m = _CHUNK_RE.search(info.key)
            if m:
                present.add(int(m.group(1)))
            elif info.key.endswith(_MANIFEST) and self._total is None:
                # the manifest is written once and never changes — read it only once
                self._total = json.loads(await self._store.get(info.key)).get("total_chunks")

        events: list[SpanEvent] = []
        nxt = self._seen + 1
        while nxt in present:
            raw = await self._store.get(_chunk_key(self._prefix, nxt))
            for line in raw.decode("utf-8").splitlines():
                if line.strip():
                    events.append(SpanEvent.model_validate_json(line))
            self._seen = nxt
            nxt += 1

        if events:
            self._last_progress = self._clock()
        return events

    def is_dead(self) -> bool:
        if self.finished:
            return False
        return (self._clock() - self._last_progress) > self._dead_after_s

"""Store-mediated telemetry — the comms + observability spine.

Append-free: the in-sandbox `ChunkShipper` writes immutable, sequence-numbered
chunk objects (`events-000001.jsonl`, …); the orchestrator `ChunkReader` lists +
concatenates them in index order. No append semantics, no long-lived stream, no
in-sandbox server — reconnect is just "re-list, resume at index". Liveness =
monotonic chunk arrival; dead = no new chunk within the substrate timeout (the
count-vs-time liveness model, a property of object listing).
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Callable

from resoluto_sandbox.contracts import Conduit, SpanEvent

_CHUNK_RE = re.compile(r"events-(\d+)\.jsonl$")
_MANIFEST = "_manifest.json"
RESULT_FILENAME = "result.json"
_RESULT = RESULT_FILENAME


def _chunk_key(prefix: str, index: int) -> str:
    return f"{prefix.rstrip('/')}/events-{index:06d}.jsonl"


def result_key(prefix: str) -> str:
    """The single source of truth for the result object key — shared by the
    runner (writer) and driver (reader) so the rendezvous can't drift."""
    return f"{prefix.rstrip('/')}/{_RESULT}"


def _default_heartbeat(ts: float) -> str:
    return SpanEvent(run_id="", span_id="hb", kind="heartbeat", event="log", ts=ts).model_dump_json()


class ChunkShipper:
    """In-sandbox: buffer JSONL lines, flush immutable chunks to the store.

    The transport is payload-agnostic — `emit_line(str)` ships any JSONL record
    (SpanEvents via `emit()`, or the worker's PipelineEvents). Inputs: an
    `Conduit`, the run's `prefix`, flush thresholds, an injectable `clock`
    (tests pass a fake; no real sleeps), and a `heartbeat_factory` building the
    quiet-period heartbeat line so the carried vocabulary stays decodable. A
    heartbeat ensures a chunk lands every `heartbeat_s` even when quiet, so the
    reader's liveness signal keeps ticking.
    """

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
        self._flush_lock = asyncio.Lock()  # emit/tick/close may flush concurrently

    async def emit_line(self, line: str) -> None:
        """Ship one opaque JSONL record (the payload-agnostic core)."""
        self._buf.append(line)
        self._buf_bytes += len(line) + 1
        if self._buf_bytes >= self._flush_bytes:
            await self.flush()

    async def emit(self, event: SpanEvent) -> None:
        await self.emit_line(event.model_dump_json())

    async def tick(self) -> None:
        """Time-driven flush + heartbeat — called on an interval by the runner."""
        now = self._clock()
        if self._buf and (now - self._last_flush) >= self._flush_interval_s:
            await self.flush()
        elif not self._buf and (now - self._last_flush) >= self._heartbeat_s:
            await self.emit_line(self._heartbeat_factory(now))
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
        tell 'gap, still arriving' from 'gap, terminal'."""
        if self._closed:
            return
        await self.flush()
        manifest = json.dumps({"total_chunks": self._index}).encode("utf-8")
        await self._store.put(f"{self._prefix}/{_MANIFEST}", manifest)
        self._closed = True


class ChunkReader:
    """Orchestrator: tail a run's chunk objects in contiguous index order.

    `poll_lines()` returns newly-available JSONL records (the payload-agnostic
    core); `poll()` is a typed SpanEvent view over it. `is_dead()` is true when no
    PROGRESS line has arrived within `dead_after_s` AND the run isn't cleanly
    finished — the SINGLE death signal. With no `progress_filter` (the default)
    every arriving line is progress, so the window means substrate-silence; with a
    `progress_filter` installed only lines the filter accepts reset the window, so
    it becomes WORK-silence — unconditional heartbeats keep `seconds_since_arrival`
    fresh but cannot mask a hung workload. A persistent non-contiguous gap (have
    chunk N+1, never saw N) stalls contiguous progress, so it surfaces through that
    same `is_dead()` window — no separate raise path to escape the driver's
    reap-on-death handling.
    """

    def __init__(
        self,
        store: Conduit,
        prefix: str,
        *,
        dead_after_s: float = 120.0,
        clock: Callable[[], float] = time.monotonic,  # monotonic: a host suspend must not count as silence
        progress_filter: Callable[[str], bool] | None = None,
    ) -> None:
        self._store = store
        self._prefix = prefix.rstrip("/")
        self._dead_after_s = dead_after_s
        self._clock = clock
        self._progress_filter = progress_filter
        self._seen = 0  # highest CONTIGUOUS index consumed
        self._last_progress = clock()
        self._last_arrival = clock()
        self._total: int | None = None
        self._armed = False  # silence counts only once arm()ed (pod reached RUNNING)

    @property
    def finished(self) -> bool:
        return self._total is not None and self._seen >= self._total

    async def poll_lines(self) -> list[str]:
        """Contiguous-index tail — the payload-agnostic core (carries the index,
        manifest, and liveness logic the typed `poll()` and the worker both reuse)."""
        infos = await self._store.list_prefix(self._prefix)
        present: set[int] = set()
        for info in infos:
            m = _CHUNK_RE.search(info.key)
            if m:
                present.add(int(m.group(1)))
            elif info.key.endswith(_MANIFEST) and self._total is None:
                # the manifest is written once and never changes — read it only once
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
                # Feed EVERY line to the (stateful) filter — a short-circuiting
                # any() would hide later heartbeat digests from it.
                verdicts = [self._progress_filter(line) for line in lines]
                if any(verdicts):
                    self._last_progress = self._clock()
        return lines

    async def poll(self) -> list[SpanEvent]:
        return [SpanEvent.model_validate_json(line) for line in await self.poll_lines()]

    def arm(self) -> None:
        """Start the silence window NOW — call the moment the pod reaches RUNNING.

        IDEMPOTENT: only the FIRST call arms (and rebases the window); later calls are
        no-ops, so a driver loop can call it every running poll without ever masking a
        real silence. Until armed, BOTH death signals (`is_dead`, `substrate_silent`)
        return false: a pod sitting Pending / SchedulingGated (waiting to schedule,
        pulling a multi-GB Kata image, or held by an EXTERNAL admission gate like Kueue)
        legitimately ships no chunks, and counting that as silence would false-positive a
        healthy pod as dead. Liveness measures silence-WHILE-RUNNING, not since-created."""
        if self._armed:
            return
        self._armed = True
        self._last_progress = self._last_arrival = self._clock()

    @property
    def seconds_since_progress(self) -> float:
        return self._clock() - self._last_progress

    @property
    def seconds_since_arrival(self) -> float:
        return self._clock() - self._last_arrival

    def is_dead(self) -> bool:
        """WORK-silence (progress window) death — false until armed/finished."""
        if not self._armed or self.finished:
            return False
        return (self._clock() - self._last_progress) > self._dead_after_s

    @property
    def substrate_silent(self) -> bool:
        """SUBSTRATE-silence death — no chunk has ARRIVED within the death window. The
        ONLY kill signal the worker acts on (heartbeats keep arrival fresh while alive).
        False until armed: pre-RUNNING quiet is legitimate, not a dead substrate."""
        return self._armed and self.seconds_since_arrival > self._dead_after_s
